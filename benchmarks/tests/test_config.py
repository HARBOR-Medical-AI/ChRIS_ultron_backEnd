"""
Tests for benchmarks.config — matrix/tier loading, the workload fingerprint and the
workload-plugin pin.
"""

import hashlib
import re
from dataclasses import replace

import pytest
import yaml

from benchmarks.config import (DEFAULT_PINS, PluginPin, load_pins, load_tier, step_for,
                               tier_fingerprint)


# -- tier loading ----------------------------------------------------------------------

def test_load_tier_smoke():
    t = load_tier("smoke")
    assert t.name == "smoke"
    assert "linear" in t.topology_axes
    assert t.repeat >= 1
    assert t.baseline.file_size == "1KiB"


def test_load_tier_missing_raises():
    with pytest.raises(KeyError):
        load_tier("nonexistent-tier")


def test_load_tier_rejects_non_escalatable_axis(tmp_path):
    p = tmp_path / "m.yml"
    p.write_text(
        "tiers:\n"
        "  bad:\n"
        "    baseline: {file_count: 1, file_size: 1KiB, depth: 1, branches: 1, "
        "layers: 1, merges: 1, feeds: 1, sleep_length: 0}\n"
        "    thresholds: {healthcheck_timeout_s: 1, api_request_timeout_s: 1, "
        "api_p95_latency_ms: 1, no_progress_timeout_s: 1, scenario_timeout_s: 1}\n"
        "    topologies:\n"
        "      linear: {axes: {bogus: 4}}\n"
    )
    with pytest.raises(ValueError):
        load_tier("bad", p)


def test_step_for_decade_only_for_file_count():
    assert step_for("file_count") == 10
    assert step_for("depth") == 2
    assert step_for("feeds") == 2
    assert step_for("branches") == 2


def test_tiers_define_quiesce_timeout():
    for name in ("smoke", "default", "full", "aging-grow", "aging-probe"):
        assert load_tier(name).thresholds.quiesce_timeout_s > 0


def test_aging_tiers_shape():
    grow, probe = load_tier("aging-grow"), load_tier("aging-probe")
    assert grow.topology_axes == {"linear": {"feeds": 4}}
    assert grow.baseline.file_count == 1000
    assert probe.topology_axes == {"linear": {"depth": 1}}   # fixed 2-instance probe


def test_headline_tier_removed():
    with pytest.raises(KeyError):
        load_tier("headline")


def test_full_linear_feeds_is_the_marquee_and_merges_escalated():
    # headline folded into full: the linear feeds axis reaches the old headline cap,
    # and fanout_fanin now escalates merges (the issue asks to iterate merges too)
    t = load_tier("full")
    assert t.topology_axes["linear"]["feeds"] == 128
    assert t.topology_axes["fanout_fanin"]["merges"] == 8


# -- workload fingerprint --------------------------------------------------------------

def test_tier_fingerprint_is_stable_and_workload_sensitive():
    t = load_tier("smoke")
    fp = tier_fingerprint(t)
    assert fp["workload_sha256"] == tier_fingerprint(load_tier("smoke"))["workload_sha256"]
    heavier = replace(t, baseline=replace(t.baseline, file_count=99))
    assert tier_fingerprint(heavier)["workload_sha256"] != fp["workload_sha256"]


def test_tier_fingerprint_hash_ignores_thresholds_and_repeat():
    t = load_tier("smoke")
    softer = replace(t, repeat=99,
                     thresholds=replace(t.thresholds, scenario_timeout_s=1))
    fp = tier_fingerprint(softer)
    assert fp["workload_sha256"] == tier_fingerprint(t)["workload_sha256"]
    assert fp["repeat"] == 99                          # recorded, just not hashed


def test_tier_fingerprint_canonicalizes_numeric_types():
    # YAML loads sleep_length: 0 as int; the CLI override produces float 0.0 —
    # equal workloads must hash equally regardless of the numeric type
    t = load_tier("smoke")
    as_int = replace(t, baseline=replace(t.baseline, sleep_length=0))
    as_float = replace(t, baseline=replace(t.baseline, sleep_length=0.0))

    assert (tier_fingerprint(as_int)["workload_sha256"]
            == tier_fingerprint(as_float)["workload_sha256"])
    
    # non-integral floats remain distinct workloads
    half = replace(t, baseline=replace(t.baseline, sleep_length=0.5))
    assert (tier_fingerprint(half)["workload_sha256"]
            != tier_fingerprint(as_int)["workload_sha256"])


def test_load_tier_coerces_baseline_types():
    t = load_tier("smoke")
    assert isinstance(t.baseline.sleep_length, float)   # YAML int -> float at the source
    assert isinstance(t.baseline.file_size, str)


# -- workload plugin pin ---------------------------------------------------------------

def test_committed_pin_names_the_workload_plugins_by_digest():
    pins = load_pins()

    assert pins.path == "benchmarks/chrisomatic/benchmark.yml"  # nosec B101 - pytest assertion
    assert set(pins.plugins) == {  # nosec B101 - pytest assertion
        "dbg-bigfiles", "pl-simpledsapp", "pl-topologicalcopy"}
    assert pins.plugins["pl-topologicalcopy"].version == "1.0.13"  # nosec B101 - pytest assertion
    assert all("@sha256:" in p.dock_image  # nosec B101 - pytest assertion
               for p in pins.plugins.values())


def _pulled_refs(text):
    """
    The references `just bench-pull-plugins` reads from a pin, as its sed and tr do: each
    line without its comment and trailing space, what follows `dock_image:`, unquoted.
    """
    refs = []

    for line in text.splitlines():
        line = re.sub(r"\s*#.*$", "", line).rstrip()
        match = re.match(r"^[ -]*dock_image:\s*(.*)$", line)
        if match:
            refs.append(match.group(1).replace('"', "").replace("'", ""))
    return refs


def test_the_pull_recipe_reads_a_reference_past_comments_quotes_and_crlf():
    text = ("    # dock_image: commented/out\n"
            "      dock_image: fnndsc/x@sha256:abc   # 1.0.0\r\n"
            '    - dock_image: "alpine:3.20"\n')
    expected = ["fnndsc/x@sha256:abc", "alpine:3.20"]

    assert _pulled_refs(text) == expected  # nosec B101 - pytest assertion


def test_committed_pin_keeps_to_what_chrisomatic_and_the_pull_recipe_read():
    # BaseLoader reads more than chrisomatic's StrictYAML does (flow style, anchors, any
    # key), and `just bench-pull-plugins` reads the references line by line
    text = DEFAULT_PINS.read_text()
    code = [line.split("#", 1)[0] for line in text.splitlines()]
    doc = yaml.load(text, Loader=yaml.BaseLoader)  # nosec B506 - BaseLoader: strings only
    allowed = {"name", "version", "dock_image", "public_repo", "compute_resource"}
    pinned = [p.dock_image for p in load_pins().plugins.values()]
    flow = [line for line in code if set(line) & set("{}[]&*")]

    assert flow == []  # nosec B101 - pytest assertion
    assert all(set(entry) <= allowed  # nosec B101 - pytest assertion
               for entry in doc["cube"]["plugins"])
    assert doc["on"]["public_store"] == ""  # nosec B101 - pytest assertion
    assert _pulled_refs(text) == pinned  # nosec B101 - pytest assertion


def test_load_pins_keeps_versions_as_strings_and_skips_bare_entries(tmp_path):
    p = tmp_path / "pin.yml"
    p.write_text("on:\n  cube_url: http://x/api/v1/\ncube:\n  plugins:\n"
                 "    - fnndsc/pl-other\n"
                 "    - name: pl-x\n      version: 1.10\n      dock_image: fnndsc/pl-x:1.10\n")
    pins = load_pins(p)

    assert pins.plugins == {  # nosec B101 - pytest assertion
        "pl-x": PluginPin("pl-x", "1.10", "fnndsc/pl-x:1.10")}
    digest = hashlib.sha256(p.read_bytes()).hexdigest()

    # outside the repository, the path reads as given
    assert pins.path == str(p)  # nosec B101 - pytest assertion
    assert pins.sha256 == digest  # nosec B101 - pytest assertion
    assert pins.record() == {"path": str(p), "sha256": digest}  # nosec B101 - pytest assertion


def test_load_pins_takes_a_file_name_in_the_pin_directory_as_bench_start_does():
    assert load_pins("benchmark.yml") == load_pins()  # nosec B101 - pytest assertion


def test_load_pins_rejects_incomplete_and_duplicate_entries(tmp_path):
    p = tmp_path / "pin.yml"
    p.write_text("cube:\n  plugins:\n    - name: pl-x\n      dock_image: fnndsc/pl-x\n")

    with pytest.raises(ValueError, match="no version"):
        load_pins(p)

    p.write_text("cube:\n  plugins:\n"
                 "    - name: pl-x\n      version: '1'\n      dock_image: a\n"
                 "    - name: pl-x\n      version: '2'\n      dock_image: b\n")

    with pytest.raises(ValueError, match="pinned twice"):
        load_pins(p)


def test_load_pins_rejects_a_file_that_is_not_a_chrisomatic_spec(tmp_path):
    p = tmp_path / "pin.yml"

    for text in ("- a list\n", "cube: plugins\n", "cube:\n  plugins: dbg-bigfiles\n", ""):
        p.write_text(text)
        with pytest.raises(ValueError, match="no cube.plugins list"):
            load_pins(p)
