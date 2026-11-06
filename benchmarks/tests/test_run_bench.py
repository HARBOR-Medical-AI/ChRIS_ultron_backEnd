"""
Tests for benchmarks.run_bench — scenario params, tier filtering, level rollup, plugin resolution.
"""

import json
from types import SimpleNamespace

import pytest

from benchmarks import run_bench
from benchmarks.config import PluginPin, PluginPins, load_pins, load_tier
from benchmarks.metrics import MetricSink
from benchmarks.models import (Classification, LevelResult, PluginRole, PollResult,
                               QueueSample, ResourceSample, SamplerSummary,
                               ScenarioParams, ScenarioResult, StatusTimeline, Verdict)
from benchmarks.results import ResultsStore
from benchmarks.chris_api import ChrisApiError, HttpResult
from benchmarks.run_bench import (RUN_STATS_SETTLE_S, BenchmarkRunner, _filter_tier,
                                  _level_row, _override_baseline, apply_build_errors,
                                  build_workload_manifest, effective_poll_cadence,
                                  make_scenario_params, pin_mismatch_message,
                                  pin_mismatches, resolve_plugin)
from benchmarks.stats_sampler import sampler_record


# -- scenario params -------------------------------------------------------------------

def test_make_scenario_params_overrides_only_one_axis():
    tier = load_tier("smoke")
    p = make_scenario_params(tier.baseline, 2.0, "linear", "depth", 8, 3)
    assert p.depth == 8 and p.axis == "depth" and p.level == 8 and p.repeat_index == 3
    assert p.feeds == tier.baseline.feeds            # other dims unchanged
    assert p.file_count == tier.baseline.file_count
    assert p.poll_interval == 2.0


def test_make_scenario_params_file_count_axis():
    tier = load_tier("smoke")
    p = make_scenario_params(tier.baseline, 1.0, "linear", "file_count", 100, 0)
    assert p.file_count == 100 and p.depth == tier.baseline.depth


# -- tier filtering --------------------------------------------------------------------

def test_filter_tier_topology_and_cap_override():
    f = _filter_tier(load_tier("full"), topology="linear", axis=None, cap=4, repeat=1)
    assert set(f.topology_axes) == {"linear"}
    assert all(cap == 4 for axes in f.topology_axes.values() for cap in axes.values())
    assert f.repeat == 1


def test_filter_tier_axis_drops_topologies_without_it():
    f = _filter_tier(load_tier("full"), topology=None, axis="layers", cap=None,
                     repeat=None)
    assert set(f.topology_axes) == {"diamond"}        # only diamond has a 'layers' axis


def test_filter_tier_rejects_unknown_topology_and_axis():
    with pytest.raises(ValueError, match="topology 'bogus'"):
        _filter_tier(load_tier("full"), topology="bogus", axis=None, cap=None,
                     repeat=None)
    with pytest.raises(ValueError, match="axis 'bogus'"):
        _filter_tier(load_tier("full"), topology=None, axis="bogus", cap=None,
                     repeat=None)


def test_override_baseline_replaces_only_given_fields():
    tier = load_tier("smoke")
    t2 = _override_baseline(tier, file_size="1MiB", sleep_length=None)
    assert t2.baseline.file_size == "1MiB"
    assert t2.baseline.sleep_length == tier.baseline.sleep_length
    assert _override_baseline(tier, None, None) is tier

    t3 = _override_baseline(tier, None, None, file_count=100)
    assert t3.baseline.file_count == 100               # the marquee preset override
    assert t3.baseline.file_size == tier.baseline.file_size


def test_file_count_override_yields_to_the_escalated_axis():
    # --file-count sets the baseline, but when file_count IS the escalated axis the
    # per-level value must still win; the override only holds for other axes.
    tier = _override_baseline(load_tier("smoke"), None, None, file_count=50)
    escalated = make_scenario_params(tier.baseline, 1.0, "linear", "file_count", 100, 0)
    assert escalated.file_count == 100                  # level wins over the override
    held = make_scenario_params(tier.baseline, 1.0, "linear", "depth", 4, 0)
    assert held.file_count == 50                        # override held while escalating depth


# -- build errors & cadence ------------------------------------------------------------

def test_apply_build_errors_escalates_to_fail():
    clean = Classification(Verdict.PASS)
    failed = apply_build_errors(clean, ["HTTP 400: bad"])
    assert failed.verdict is Verdict.FAIL
    assert "build_error:1" in failed.criteria
    
    # already-FAIL classifications keep their original criteria
    hard = Classification(Verdict.FAIL, ("no_progress",))
    assert apply_build_errors(hard, ["x"]) is hard
    assert apply_build_errors(clean, []) is clean


def test_effective_poll_cadence_scales_with_feeds():
    assert effective_poll_cadence(1.0, 1) == 1.0          # baseline cadence
    assert effective_poll_cadence(1.0, 64) == 3.2         # bounded observer load
    assert effective_poll_cadence(1.0, 1000) == 5.0       # capped
    assert effective_poll_cadence(2.0, 1) == 2.0          # never faster than base


# -- level rollup ----------------------------------------------------------------------

def _scenario(verdict, makespan, p95, cpu, n_completed, out_bytes):
    p = ScenarioParams("linear", "depth", 1, 1, "1KiB", 1, 1, 1, 1, 1, 0, 1.0, 0)
    tls = []

    for i in range(n_completed):
        tl = StatusTimeline(i, "k", PluginRole.DS)
        tl.final_status = "finishedSuccessfully"
        tls.append(tl)

    return ScenarioResult(
        p, Classification(verdict), makespan, [], tls,
        {"by_class": {"create": {"p95_ms": p95}}, "errors": 0, "status_5xx": 0},
        {"db": {"cpu_pct_peak": cpu}},
        {"registered_output_bytes": out_bytes}, 0.0, 1.0)


def test_level_row_aggregates_repeats():
    reps = [_scenario(Verdict.PASS, 5.0, 100, 40, 2, 1000),
            _scenario(Verdict.PASS, 7.0, 300, 80, 2, 2000)]
    row = _level_row(LevelResult("linear", "depth", 4, Verdict.PASS, (), reps))
    assert row["makespan_median_s"] == 6.0            # median(5, 7)
    assert row["makespan_min_s"] == 5.0               # cross-run spread estimate
    assert row["worst_p95_ms"] == 300                 # worst across repeats
    assert row["peak_cpu_pct"]["db"] == 80
    assert row["completed"] == 4                       # 2 per repeat x 2 repeats
    assert row["registered_output_bytes"] == 2000      # max across repeats


def test_level_row_excludes_job_containers_and_keeps_service_io():
    p = ScenarioParams("linear", "depth", 1, 1, "1KiB", 1, 1, 1, 1, 1, 0, 1.0, 0)
    res = {
        "db": {"kind": "service", "cpu_pct_peak": 70.0, "blkio_write_bytes": 2048},
        "chris-jid-1": {"kind": "job", "cpu_pct_peak": 250.0, "blkio_write_bytes": 999},
    }
    sr = ScenarioResult(p, Classification(Verdict.PASS), 1.0, [], [],
                        {"by_class": {}, "errors": 0, "status_5xx": 0}, res,
                        {"registered_output_bytes": 0}, 0.0, 1.0)
    row = _level_row(LevelResult("linear", "depth", 1, Verdict.PASS, (), [sr]))
    assert row["peak_cpu_pct"] == {"db": 70.0}         # ephemeral job container excluded
    assert row["peak_job_cpu_pct"] == 250.0            # surfaced as one aggregate
    assert row["peak_write_bytes"] == {"db": 2048}     # service disk write only


def _with_counters(db_counters):
    sr = _scenario(Verdict.PASS, 5.0, 100, 40, 1, 0)
    sr.db_counters = db_counters
    return sr


def test_level_row_rolls_up_db_counters_as_the_worst_repeat_per_window():
    reps = [_with_counters({"workload": {"num_requested": 0, "wal_bytes": 900},
                            "cleanup": {"temp_bytes": 70}}),
            _with_counters({"workload": {"num_requested": 2, "wal_bytes": 500},
                            "cleanup": None})]
    row = _level_row(LevelResult("diamond", "layers", 8, Verdict.PASS, (), reps))

    assert row["db_counters"] == {  # nosec B101 - pytest assertion
        "workload": {"num_requested": 2, "wal_bytes": 900},
        "cleanup": {"temp_bytes": 70}}


def test_level_row_surfaces_a_counter_reset_instead_of_a_number():
    reps = [_with_counters({"workload": {"num_requested": 0}}),
            _with_counters({"workload": {"reset": True}})]
    row = _level_row(LevelResult("diamond", "layers", 8, Verdict.PASS, (), reps))

    assert row["db_counters"] == {"workload": {"reset": True}}  # nosec B101 - pytest assertion


def test_level_row_keeps_the_container_counters_of_a_reset_repeat():
    reps = [_with_counters({"workload": {"num_requested": 0, "cgroup_oom_kill": 0}}),
            _with_counters({"workload": {"reset": True, "cgroup_oom_kill": 1}})]
    row = _level_row(LevelResult("diamond", "layers", 8, Verdict.PASS, (), reps))

    workload = row["db_counters"]["workload"]
    assert workload == {"reset": True, "cgroup_oom_kill": 1}  # nosec B101 - pytest assertion


def _resources_scenario(resources, sampler):
    p = ScenarioParams("linear", "depth", 1, 1, "1KiB", 1, 1, 1, 1, 1, 0, 1.0, 0)
    return ScenarioResult(p, Classification(Verdict.PASS), 1.0, [], [],
                          {"by_class": {}, "errors": 0, "status_5xx": 0}, resources,
                          {"registered_output_bytes": 0}, 0.0, 1.0, sampler=sampler)


def test_level_row_leaves_out_what_was_not_measured_and_rolls_up_the_sampler():
    first = _resources_scenario(
        {"db": {"kind": "service", "cpu_pct_peak": None, "blkio_write_bytes": 10,
                "mem_bytes_peak": 300, "mem_anon_peak": 200, "cpu_throttled_s": 0.5},
         "chris": {"kind": "service", "cpu_pct_peak": 30.0, "mem_bytes_peak": 100},
         "chris-jid-1": {"kind": "job", "cpu_pct_peak": None}},
        SamplerSummary(interval_s=1.0, queue_interval_s=0.5, ticks=10, late_ticks=1,
                       max_tick_s=0.4, reads=30, jobs_seen=2, queue_ticks=20))
    second = _resources_scenario(
        {"chris": {"kind": "service", "cpu_pct_peak": 20.0, "mem_bytes_peak": 150}},
        SamplerSummary(interval_s=1.0, queue_interval_s=0.5, ticks=5, max_tick_s=0.2,
                       reads=10, failed_reads=1, queue_ticks=10))
    row = _level_row(LevelResult("linear", "depth", 1, Verdict.PASS, (), [first, second]))

    # a service with no CPU window has no CPU peak, and no job had one either
    assert row["peak_cpu_pct"] == {"chris": 30.0}  # nosec B101 - pytest assertion
    assert row["peak_job_cpu_pct"] is None  # nosec B101 - pytest assertion
    assert row["peak_mem_bytes"] == {"db": 300, "chris": 150}  # nosec B101 - pytest assertion
    assert row["peak_mem_anon_bytes"] == {"db": 200}  # nosec B101 - pytest assertion
    assert row["cpu_throttled_s"] == {"db": 0.5}  # nosec B101 - pytest assertion
    assert row["peak_write_bytes"] == {"db": 10}  # nosec B101 - pytest assertion
    assert row["sampler"] == {  # nosec B101 - pytest assertion
        "interval_s": 1.0, "queue_interval_s": 0.5, "max_tick_s": 0.4, "ticks": 15,
        "late_ticks": 1, "reads": 40, "failed_reads": 1, "vanished": 0,
        "list_failures": 0, "jobs_seen": 2, "queue_ticks": 30, "queue_late_ticks": 0,
        "queue_failures": 0, "busy_skips": 0}


def test_level_row_of_scenarios_without_a_sampler_summary_has_an_empty_block():
    row = _level_row(LevelResult("linear", "depth", 1, Verdict.PASS, (),
                                 [_scenario(Verdict.PASS, 1.0, 10, 5, 1, 0)]))

    assert row["sampler"] == {}  # nosec B101 - pytest assertion


def test_level_row_without_db_counters_has_an_empty_block():
    row = _level_row(LevelResult("linear", "depth", 1, Verdict.PASS, (),
                                 [_scenario(Verdict.PASS, 1.0, 10, 5, 1, 0)]))

    assert row["db_counters"] == {}  # nosec B101 - pytest assertion


# -- Postgres counter windows and run totals ------------------------------------------------

IN_WINDOW = ResourceSample(t=1.0, container="db", kind="service", cpu_pct=40.0,
                           mem_bytes=1, mem_limit_bytes=2)
AFTER_WINDOW = ResourceSample(t=9.0, container="chris", kind="service", cpu_pct=90.0,
                              mem_bytes=1, mem_limit_bytes=2)


class _Sampler:
    """
    Stands in for StatsSampler, recording when the resource samples start and stop, the
    Docker client it is given, and one sample, at its exit.
    """

    def __init__(self, events):
        self.events = events
        self.docker = None

    def __call__(self, docker, sink, *args, **kwargs):
        self.docker, self.sink = docker, sink
        return self

    def __enter__(self):
        self.events.append("samples start")
        return self

    def __exit__(self, *exc):
        self.sink.record_resource(IN_WINDOW)
        self.events.append("samples stop")

    def summary(self):
        return SamplerSummary(interval_s=1.0, queue_interval_s=0.5, ticks=3)


def _scenario_runner(monkeypatch, tmp_path, *, cleanup):
    """
    A runner whose adapters are fakes that record, in order, what ``run_scenario`` does.
    """
    events = []
    snapshots = iter([{"at": 0.0, "server_start": "s", "wal_bytes": 0},
                      {"at": 5.0, "server_start": "s", "wal_bytes": 100},
                      {"at": 8.0, "server_start": "s", "wal_bytes": 130}])

    def snapshot():
        events.append("snapshot")
        return next(snapshots)

    poll = PollResult(timelines=[], makespan_s=1.0, terminal=True, hard_failures=(),
                      no_progress=False, timed_out=False, final_status_counts={})

    sampler = _Sampler(events)
    monkeypatch.setattr(run_bench, "StatsSampler", sampler)
    monkeypatch.setattr(run_bench, "build_feeds", lambda *a, **k: [])
    monkeypatch.setattr(run_bench, "poll_to_completion", lambda *a, **k: poll)
    monkeypatch.setattr(run_bench, "ProgressProbe", lambda docker: None)

    runner = BenchmarkRunner.__new__(BenchmarkRunner)   # no API or Docker connection
    runner.tier = load_tier("smoke")
    runner.cleanup = cleanup
    runner.store = ResultsStore(tmp_path)
    runner.docker = runner.api = runner.broker = None
    runner.sampler_docker = SimpleNamespace(name="the sampler's client")
    runner._sink = MetricSink()
    runner.plugin_ids = {}
    runner.pg_stats = SimpleNamespace(reset=lambda: events.append("pg_stats reset"))
    runner._global_counts = lambda: {}
    runner._db_snapshot = snapshot
    runner._db_deltas = lambda *a: events.append("db deltas") or {}
    runner._cleanup_and_quiesce = lambda *a: (events.append("cleanup"),
                                              runner._sink.record_resource(AFTER_WINDOW))
    runner._persist_scenario = lambda *a: events.append("persist") or events.append(a)
    runner.fake_sampler = sampler
    return runner, events


def test_scenario_counter_windows_match_the_resource_samples_and_cleanup(monkeypatch,
                                                                         tmp_path):
    runner, events = _scenario_runner(monkeypatch, tmp_path, cleanup=True)
    result = runner.run_scenario(make_scenario_params(runner.tier.baseline, 2.0, "linear",
                                                      "depth", 1, 0))

    expected = ["pg_stats reset", "snapshot", "samples start", "samples stop",
                "snapshot", "db deltas", "cleanup", "snapshot", "persist"]
    assert [e for e in events if isinstance(e, str)] == expected  # nosec B101 - pytest assertion
    workload, cleanup = result.db_counters["workload"], result.db_counters["cleanup"]
    assert workload == {"wal_bytes": 100, "seconds": 5.0}  # nosec B101 - pytest assertion
    assert cleanup == {"wal_bytes": 30, "seconds": 3.0}  # nosec B101 - pytest assertion


def test_a_scenario_rolls_up_and_persists_the_samples_of_its_workload_window(monkeypatch,
                                                                            tmp_path):
    runner, events = _scenario_runner(monkeypatch, tmp_path, cleanup=True)
    result = runner.run_scenario(make_scenario_params(runner.tier.baseline, 2.0, "linear",
                                                      "depth", 1, 0))
    _, _, resources, queues = events[-1]

    # the cleanup's late sample is in neither the rollup nor the persisted files
    assert set(result.resources) == {"db"}  # nosec B101 - pytest assertion
    assert (resources, queues) == ([IN_WINDOW], [])  # nosec B101 - pytest assertion
    assert result.sampler == runner.fake_sampler.summary()  # nosec B101 - pytest assertion
    assert runner.fake_sampler.docker is runner.sampler_docker  # nosec B101 - pytest assertion


def test_a_scenario_that_keeps_its_feeds_has_no_cleanup_window(monkeypatch, tmp_path):
    runner, events = _scenario_runner(monkeypatch, tmp_path, cleanup=False)
    result = runner.run_scenario(make_scenario_params(runner.tier.baseline, 2.0, "linear",
                                                      "depth", 1, 0))

    assert events.count("snapshot") == 2  # nosec B101 - pytest assertion
    assert result.db_counters["cleanup"] is None  # nosec B101 - pytest assertion


def test_db_snapshot_survives_failing_reads(monkeypatch):
    class Docker:
        def cgroup_counters(self, service):
            return {"cgroup_write_bytes": 10}

    def broken(docker):
        raise RuntimeError("docker API went away")

    runner = BenchmarkRunner.__new__(BenchmarkRunner)
    runner.docker = Docker()
    monkeypatch.setattr(run_bench, "server_counters", broken)

    # the container half is still read, and nothing escapes into the scenario or run
    assert runner._db_snapshot() == {"cgroup_write_bytes": 10}  # nosec B101 - pytest assertion

    runner.docker.cgroup_counters = broken
    assert runner._db_snapshot() == {}  # nosec B101 - pytest assertion


def _run_runner(monkeypatch, tmp_path, snapshots, sleeps):
    monkeypatch.setattr(run_bench.environment, "collect_environment",
                        lambda docker: {"postgres": {}})
    monkeypatch.setattr(run_bench.environment, "measure_noise_floor", lambda api: {})
    monkeypatch.setattr(run_bench.escalation, "run", lambda *a, **k: ([], []))
    monkeypatch.setattr(run_bench.time, "sleep", sleeps.append)

    runner = BenchmarkRunner.__new__(BenchmarkRunner)
    runner.tier = load_tier("smoke")
    runner.store = ResultsStore(tmp_path)
    runner.api = SimpleNamespace(auth_mode="token")
    runner.broker = SimpleNamespace(available=True)
    runner.pg_stats = SimpleNamespace(available=False,
                                      unavailable_reason="psql: connection refused")
    runner.plugins = {}
    runner.plugin_images = {}
    runner.pins = load_pins()
    runner.docker = None
    runner.sampler_docker = SimpleNamespace(available=True)
    runner._db_snapshot = lambda: snapshots.pop(0)
    monkeypatch.setattr(run_bench, "server_counters_error",
                        lambda docker: "psql: FATAL:  sorry, too many clients already")
    return runner


def test_run_totals_wait_for_the_statistics_to_settle(monkeypatch, tmp_path):
    sleeps = []
    snapshots = [{"at": 0.0, "server_start": "s", "num_requested": 1},
                 {"at": 60.0, "server_start": "s", "num_requested": 1}]
    summary = _run_runner(monkeypatch, tmp_path, snapshots, sleeps).run()

    assert sleeps == [RUN_STATS_SETTLE_S]  # nosec B101 - pytest assertion
    run_delta = summary["db_counters"]["run"]
    assert run_delta == {"num_requested": 0, "seconds": 60.0}  # nosec B101 - pytest assertion
    attribution = summary["environment"]["attribution"]
    assert attribution["db_counters"] is True  # nosec B101 - pytest assertion


def test_run_without_readable_counters_skips_the_wait_and_says_why(monkeypatch, tmp_path,
                                                                   capsys):
    sleeps = []
    summary = _run_runner(monkeypatch, tmp_path, [{}, {}], sleeps).run()

    assert sleeps == []  # nosec B101 - pytest assertion
    assert summary["db_counters"]["run"] is None  # nosec B101 - pytest assertion
    attribution = summary["environment"]["attribution"]
    assert attribution["db_counters"] == (  # nosec B101 - pytest assertion
        "Postgres counters unreadable: psql: FATAL:  sorry, too many clients already")
    # the empty settings block was reported as it happened
    out = capsys.readouterr().out
    assert "could not read the Postgres settings" in out  # nosec B101 - pytest assertion


# -- workload plugins ------------------------------------------------------------------

PINS = PluginPins(path="benchmarks/chrisomatic/benchmark.yml", sha256="ab12", plugins={
    "dbg-bigfiles": PluginPin("dbg-bigfiles", "1.0.0", "fnndsc/dbg-bigfiles@sha256:b956"),
    "pl-topologicalcopy": PluginPin("pl-topologicalcopy", "1.0.13",
                                    "fnndsc/pl-topologicalcopy@sha256:35cb"),
})

IMAGE = {"image_id": "sha256:1c45", "repo_digests": ["fnndsc/dbg-bigfiles@sha256:b956"],
         "architecture": "amd64"}


def _registered(name, version, dock_image, matches=1, installed=None):
    return {"id": 1, "name": name, "version": version, "dock_image": dock_image,
            "matches": matches, "installed_versions": installed or [version]}


def _pinned_registrations(pins=PINS):
    roles = {"dbg-bigfiles": PluginRole.FS, "pl-simpledsapp": PluginRole.DS,
             "pl-topologicalcopy": PluginRole.TS}
    return {roles[p.name]: _registered(p.name, p.version, p.dock_image)
            for p in pins.plugins.values()}


def _mismatches(plugins, images=None, docker_available=False):
    return pin_mismatches(plugins, PINS, images or {}, docker_available=docker_available)


def test_build_workload_manifest_carries_versions_and_images_keyed_by_role():
    m = build_workload_manifest(_pinned_registrations(), {PluginRole.FS: IMAGE})
    expected = {"name": "dbg-bigfiles", "version": "1.0.0", "id": 1,
                "dock_image": "fnndsc/dbg-bigfiles@sha256:b956", "matches": 1,
                "image": IMAGE}

    assert m["fs"] == expected  # nosec B101 - pytest assertion
    # Docker could not tell
    assert m["ts"]["image"] is None  # nosec B101 - pytest assertion


def test_the_pinned_registrations_with_their_images_present_match():
    images = dict.fromkeys(_pinned_registrations(), IMAGE)
    problems = _mismatches(_pinned_registrations(), images, docker_available=True)

    assert problems == []  # nosec B101 - pytest assertion


def test_another_registered_version_is_a_mismatch_naming_both():
    # e.g. the ChRIS store's no-op 2.0.0, registered by a dev `just` on the same volumes
    plugins = _pinned_registrations()
    plugins[PluginRole.TS] = _registered("pl-topologicalcopy", "2.0.0",
                                         "docker.io/backplane/true",
                                         matches=2, installed=["2.0.0", "1.0.13"])
    expected = ["pl-topologicalcopy has 2 versions registered (2.0.0, 1.0.13), "
                "not only the pinned 1.0.13"]

    assert _mismatches(plugins) == expected  # nosec B101 - pytest assertion


def test_another_version_or_image_reference_is_a_mismatch():
    plugins = _pinned_registrations()
    plugins[PluginRole.FS] = _registered("dbg-bigfiles", "1.0.1", "localhost/dbg-bigfiles")
    expected = ["dbg-bigfiles 1.0.1 is registered, 1.0.0 is pinned",
                "dbg-bigfiles is registered as localhost/dbg-bigfiles, pinned as "
                "fnndsc/dbg-bigfiles@sha256:b956"]

    assert _mismatches(plugins) == expected  # nosec B101 - pytest assertion


def test_an_absent_image_is_a_mismatch_only_when_docker_can_tell():
    plugins = _pinned_registrations()
    images = {PluginRole.FS: IMAGE}                      # the ts image is not present
    expected = ["pl-topologicalcopy's image fnndsc/pl-topologicalcopy@sha256:35cb is "
                "not present"]

    assert _mismatches(plugins, images, True) == expected  # nosec B101 - pytest assertion
    assert _mismatches(plugins, images, False) == []  # nosec B101 - pytest assertion


def test_an_image_docker_could_not_read_is_a_mismatch_with_its_error():
    images = {PluginRole.FS: IMAGE, PluginRole.TS: {"error": "500 Server Error"}}
    problems = _mismatches(_pinned_registrations(), images, docker_available=True)

    assert len(problems) == 1  # nosec B101 - pytest assertion
    assert "could not be read: 500 Server Error" in problems[0]  # nosec B101 - pytest assertion


def test_an_unpinned_or_unregistered_workload_plugin_is_a_mismatch():
    plugins = _pinned_registrations()
    plugins[PluginRole.DS] = _registered("pl-simpledsapp", "2.1.5", "fnndsc/pl-simpledsapp")
    plugins[PluginRole.FS] = _registered("dbg-bigfiles", None, None, matches=0)
    expected = ["dbg-bigfiles is not registered",
                "pl-simpledsapp is not pinned in benchmarks/chrisomatic/benchmark.yml"]

    assert sorted(_mismatches(plugins)) == expected  # nosec B101 - pytest assertion


def test_pin_mismatch_message_lists_the_problems_and_each_remedy():
    msg = pin_mismatch_message(PINS, ["one", "two"])
    listed = "benchmarks/chrisomatic/benchmark.yml pins:\n  - one\n  - two\n"

    assert listed in msg  # nosec B101 - pytest assertion
    # the pin bench-start was given comes first: a reset would delete the dev data
    assert msg.index("--plugins") < msg.index("bench-nuke")  # nosec B101 - pytest assertion
    assert "'just bench-start benchmark.yml'" in msg  # nosec B101 - pytest assertion
    assert "'just bench-pull-plugins benchmark.yml'" in msg  # nosec B101 - pytest assertion


def test_resolve_plugin_records_a_plugin_cube_does_not_have():
    def missing(name):
        raise ChrisApiError(HttpResult(False, 404, 1.0, None, [], f"not found: {name}"))

    record = resolve_plugin(SimpleNamespace(get_plugin=missing), "dbg-bigfiles")

    assert record["matches"] == 0 and record["id"] is None  # nosec B101 - pytest assertion


def test_resolve_plugin_raises_other_api_errors():
    def broken(name):
        raise ChrisApiError(HttpResult(False, 500, 1.0, None, [], "server error"))

    with pytest.raises(ChrisApiError):
        resolve_plugin(SimpleNamespace(get_plugin=broken), "dbg-bigfiles")


def test_committed_pin_pins_every_workload_plugin():
    names = set(run_bench.PLUGIN_NAMES.values())

    assert names <= set(load_pins().plugins)  # nosec B101 - pytest assertion


def _connect(monkeypatch, tmp_path, registered, images=None):
    """
    A runner over fakes of everything __init__ connects to: ``registered`` (name ->
    get_plugin result; a missing name is a plugin CUBE does not have) as CUBE's workload
    plugins, and ``images`` (reference -> image_info result) as what Docker has, or Docker
    unavailable when None.
    """
    def get_plugin(name):
        if name not in registered:
            raise ChrisApiError(HttpResult(False, 404, 1.0, None, [], "not found"))
        return registered[name]

    docker = SimpleNamespace(available=images is not None,
                             image_info=lambda ref: (images or {}).get(ref))
    monkeypatch.setattr(run_bench, "ChrisApi",
                        lambda *a, **k: SimpleNamespace(get_plugin=get_plugin))
    monkeypatch.setattr(run_bench, "wait_for_health", lambda api, timeout: True)
    monkeypatch.setattr(run_bench, "DockerClient", lambda **k: docker)
    monkeypatch.setattr(run_bench, "BrokerClient", lambda host, port: None)
    monkeypatch.setattr(run_bench, "PgStatStatements", lambda docker: None)

    return BenchmarkRunner(url="http://chris:8000/api/v1/", username="chris",
                           password="chris1234",  # nosec B106 - the dev stack's test user
                           tier=load_tier("smoke"), pins=load_pins(),
                           store=ResultsStore(tmp_path), cube_poll_interval=2.0)


def _pinned_by_name():
    return {p["name"]: p for p in _pinned_registrations(load_pins()).values()}


def test_runner_starts_on_the_pinned_registrations(monkeypatch, tmp_path):
    registered = _pinned_by_name()
    images = {p["dock_image"]: IMAGE for p in registered.values()}
    runner = _connect(monkeypatch, tmp_path, registered, images)
    expected = dict.fromkeys(PluginRole, IMAGE)

    assert set(runner.plugin_ids) == set(PluginRole)  # nosec B101 - pytest assertion
    assert runner.plugin_images == expected  # nosec B101 - pytest assertion


def test_runner_refuses_to_start_on_other_registrations(monkeypatch, tmp_path):
    registered = _pinned_by_name()
    registered["pl-topologicalcopy"] = _registered(
        "pl-topologicalcopy", "2.0.0", "docker.io/backplane/true")

    with pytest.raises(RuntimeError, match="pl-topologicalcopy 2.0.0 is registered"):
        _connect(monkeypatch, tmp_path, registered)


def test_runner_refuses_to_start_when_a_pinned_image_is_absent(monkeypatch, tmp_path):
    with pytest.raises(RuntimeError, match="is not present"):
        _connect(monkeypatch, tmp_path, _pinned_by_name(), images={})


def test_runner_lists_a_plugin_cube_does_not_have_with_the_other_problems(monkeypatch,
                                                                         tmp_path):
    registered = _pinned_by_name()
    del registered["dbg-bigfiles"]
    registered["pl-simpledsapp"] = _registered("pl-simpledsapp", "2.1.4", "x")

    with pytest.raises(RuntimeError) as refused:
        _connect(monkeypatch, tmp_path, registered)
    message = str(refused.value)

    assert "dbg-bigfiles is not registered" in message  # nosec B101 - pytest assertion
    assert "pl-simpledsapp 2.1.4 is registered" in message  # nosec B101 - pytest assertion


def test_run_records_how_it_samples(monkeypatch, tmp_path):
    env = _run_runner(monkeypatch, tmp_path, [{}, {}], []).run()["environment"]

    assert env["sampler"] == sampler_record(  # nosec B101 - pytest assertion
        run_bench.STATS_INTERVAL_S, run_bench.QUEUE_INTERVAL_S, run_bench.SAMPLER_WORKERS)
    assert env["attribution"]["resource_sampling"] is True  # nosec B101 - pytest assertion


def test_a_scenario_persists_the_samples_its_rollups_used(tmp_path):
    runner = BenchmarkRunner.__new__(BenchmarkRunner)
    runner.store = ResultsStore(tmp_path)
    runner._sink = MetricSink()
    runner.pg_stats = SimpleNamespace(snapshot=lambda: None)
    summary = SamplerSummary(interval_s=1.0, queue_interval_s=0.5, ticks=4)
    result = _resources_scenario({}, summary)
    sample = ResourceSample(t=1.0, container="db", kind="service", cpu_pct=None,
                            mem_bytes=1, mem_limit_bytes=2)
    # a sample recorded after the workload window is not persisted
    runner._sink.record_resource(AFTER_WINDOW)

    runner._persist_scenario(result, 2.0, [sample], [QueueSample(1.0, "main2", 3)])

    sid = result.params.scenario_id()
    scenario = json.loads((runner.store.run_dir / f"scenarios/{sid}/scenario.json").read_text())
    assert scenario["observer"]["sampler"]["ticks"] == 4  # nosec B101 - pytest assertion
    rows = runner.store.read_jsonl("docker_stats.jsonl")
    persisted = [(r["container"], r["cpu_pct"]) for r in rows]
    assert persisted == [("db", None)]  # nosec B101 - pytest assertion
    assert len(runner.store.read_jsonl("queue_depths.jsonl")) == 1  # nosec B101 - pytest assertion


def test_run_records_why_the_resources_were_not_sampled(monkeypatch, tmp_path):
    runner = _run_runner(monkeypatch, tmp_path, [{}, {}], [])
    runner.sampler_docker = SimpleNamespace(available=False,
                                            unavailable_reason="permission denied")
    env = runner.run()["environment"]

    reason = env["attribution"]["resource_sampling"]
    assert reason == "permission denied"  # nosec B101 - pytest assertion


def test_run_records_the_plugins_images_and_the_pin(monkeypatch, tmp_path):
    runner = _run_runner(monkeypatch, tmp_path, [{}, {}], [])
    runner.plugins = _pinned_registrations()
    runner.plugin_images = {PluginRole.FS: IMAGE}
    env = runner.run()["environment"]

    assert env["workload_plugins"]["fs"]["image"] == IMAGE  # nosec B101 - pytest assertion
    assert env["workload_plugins_pin"] == runner.pins.record()  # nosec B101 - pytest assertion


def test_main_checks_against_the_pin_it_is_given(monkeypatch, tmp_path):
    seen = {}

    class Runner:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def run(self):
            return {"levels_run": 0, "breaking_points": []}

    pin = tmp_path / "other.yml"
    pin.write_text("cube:\n  plugins:\n"
                   "    - name: pl-x\n      version: '1'\n      dock_image: fnndsc/pl-x\n")
    monkeypatch.setattr(run_bench, "BenchmarkRunner", Runner)
    run_bench.main(["--plugins", str(pin), "--results-dir", str(tmp_path / "results")])

    assert seen["pins"] == load_pins(pin)  # nosec B101 - pytest assertion
