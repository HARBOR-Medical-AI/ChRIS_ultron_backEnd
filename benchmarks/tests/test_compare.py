"""
Tests for benchmarks.compare — run comparability, deltas, trends, and the CI gate.
"""

from pathlib import Path

from benchmarks.compare import (DeltaPolicy, breaking_point_shifts, check_comparability,
                                classify_delta, compare_levels, compare_runs,
                                flag_counts, has_regressions, main, render_csv,
                                render_markdown, trend_tables)
from benchmarks.models import RunData
from benchmarks.results import ResultsStore

POLICY = DeltaPolicy()      # 20% relative, 2s makespan floor, 50ms latency floor


# -- fixtures ----------------------------------------------------------------------------

def _env(**over):
    env = {
        "matrix": {"workload_sha256": "abc", "thresholds": {"scenario_timeout_s": 600},
                   "repeat": 1},
        "storage_mode": "fslink",
        "api_auth_mode": "token",
        "envelope": {"CUBE_UVICORN_WORKERS": "4"},
        "host": {"ncpu": 16, "mem_total_bytes": 1024, "operating_system": "Ubuntu",
                 "architecture": "x86_64"},
        "git": {"commit": "aaa", "dirty": "false"},
        "noise_floor": {"health_p50_ms": 3.0},
    }
    env.update(over)
    return env


def _lvl(topology="linear", axis="depth", level=1, verdict="PASS",
         makespan=10.0, p95=100.0):
    return {"topology": topology, "axis": axis, "level": level, "verdict": verdict,
            "makespan_median_s": makespan, "worst_p95_ms": p95}


def _run(run_id, levels, breaking_points=(), env=None):
    return RunData(run_id=run_id, path=Path("/runs") / run_id,
                   environment=env if env is not None else _env(),
                   summary={"run_id": run_id, "breaking_points": list(breaking_points)},
                   levels=levels)


# -- comparability -----------------------------------------------------------------------

def test_identical_environments_are_comparable():
    c = check_comparability(_env(), _env())
    assert c.status == "comparable" and c.diffs == ()


def test_workload_hash_mismatch_is_blocker():
    other = _env(matrix={"workload_sha256": "zzz", "thresholds": {}, "repeat": 1})
    c = check_comparability(_env(), other)
    assert c.status == "not_comparable"
    assert any(d.field == "matrix.workload_sha256" and d.severity == "blocker"
               for d in c.diffs)


def test_auth_mode_mismatch_is_blocker():
    c = check_comparability(_env(), _env(api_auth_mode="basic"))
    assert c.status == "not_comparable"


def test_missing_fingerprint_is_warning_not_crash():
    old = _env()
    del old["matrix"]                       # run predates the workload fingerprint
    c = check_comparability(old, _env())
    sha = next(d for d in c.diffs if d.field == "matrix.workload_sha256")
    assert sha.severity == "warning"
    assert c.status in ("caution", "not_comparable")    # thresholds/repeat also differ


def test_envelope_and_noise_floor_are_cautions():
    other = _env(envelope={"CUBE_UVICORN_WORKERS": "8"},
                 noise_floor={"health_p50_ms": 97.0})
    c = check_comparability(_env(), other)
    assert c.status == "caution"
    fields = {d.field for d in c.diffs}
    assert "envelope.CUBE_UVICORN_WORKERS" in fields
    assert "noise_floor.health_p50_ms" in fields


def test_commit_difference_is_informational():
    c = check_comparability(_env(), _env(git={"commit": "bbb", "dirty": "false"}))
    assert c.status == "comparable"         # comparing commits is the point
    assert any(d.field == "git.commit" and d.severity == "info" for d in c.diffs)


def _pg(**settings):
    return {"settings": {"shared_buffers": "128MB", "TimeZone": "Etc/UTC", **settings},
            "altered": [], "table_options": {"core_chrisfile": []}}


def test_postgres_setting_difference_is_a_caution():
    c = check_comparability(_env(postgres=_pg()),
                            _env(postgres=_pg(shared_buffers="1GB", TimeZone="UTC")))
    by_field = {d.field: d for d in c.diffs}

    assert c.status == "caution"  # nosec B101 - pytest assertion
    assert by_field["postgres.shared_buffers"].candidate == "1GB"  # nosec B101 - pytest assertion
    assert by_field["postgres.TimeZone"].severity == "warning"  # nosec B101 - pytest assertion


def test_unrecorded_postgres_settings_give_a_single_caution():
    # a run from before the settings were recorded: one caution, not one per setting
    c = check_comparability(_env(), _env(postgres=_pg()))
    pg = [d for d in c.diffs if d.field.startswith("postgres")]

    assert c.status == "caution"  # nosec B101 - pytest assertion
    assert [(d.field, d.baseline, d.candidate) for d in pg] == [  # nosec B101 - pytest assertion
        ("postgres", "not recorded", "recorded")]


def test_table_options_and_alter_system_overrides_are_cautions():
    altered = dict(_pg(), altered=["work_mem"],
                   table_options={"core_chrisfile": ["autovacuum_vacuum_scale_factor=0.02"]})
    fields = {d.field for d in check_comparability(_env(postgres=_pg()),
                                                   _env(postgres=altered)).diffs}

    assert "postgres.altered" in fields  # nosec B101 - pytest assertion
    assert "postgres.table_options.core_chrisfile" in fields  # nosec B101 - pytest assertion


def _limits(shm):
    return {"db": {"cpus": 2.0, "mem_limit": 4294967296, "memswap_limit": None,
                   "shm_size": shm}}


def test_container_limit_difference_is_a_caution():
    c = check_comparability(_env(limits=_limits(67108864)),
                            _env(limits=_limits(1073741824)))
    shm = [d for d in c.diffs if d.field == "limits.db.shm_size"]

    assert c.status == "caution"  # nosec B101 - pytest assertion
    changes = [(d.baseline, d.candidate) for d in shm]
    assert changes == [(67108864, 1073741824)]  # nosec B101 - pytest assertion


def test_unrecorded_limits_give_a_single_caution():
    fields = [d.field for d in check_comparability(_env(), _env(limits=_limits(1))).diffs]

    assert fields == ["limits"]  # nosec B101 - pytest assertion


def test_db_broker_and_cache_image_differences_are_informational():
    for service in ("db", "chris-broker", "chris-cache"):
        c = check_comparability(_env(images={service: {"image_id": "sha256:a"}}),
                                _env(images={service: {"image_id": "sha256:b"}}))

        assert c.status == "comparable"  # nosec B101 - pytest assertion
        severities = [d.severity for d in c.diffs if d.field == f"images.{service}"]
        assert severities == ["info"]  # nosec B101 - pytest assertion


def test_broker_image_missing_from_an_older_run_is_informational():
    c = check_comparability(_env(), _env(images={"chris-broker": {"image_id": "sha256:b"}}))

    diffs = [(d.field, d.severity) for d in c.diffs]

    assert c.status == "comparable"  # nosec B101 - pytest assertion
    assert diffs == [("images.chris-broker", "info")]  # nosec B101 - pytest assertion


def test_a_run_from_before_the_rename_compares_under_the_current_names():
    # runs archived before the rename recorded the broker's limits under dragonflydb
    limits = {"cpus": 2.0, "mem_limit": 1073741824, "memswap_limit": None, "shm_size": None}
    old = _env(limits={"dragonflydb": limits, "db": limits})
    new = _env(limits={"chris-broker": {**limits, "cpus": 1.0}, "db": limits})
    diffs = {d.field: (d.baseline, d.candidate, d.severity)
             for d in check_comparability(old, new).diffs}
    expected = {"limits.chris-broker.cpus": (2.0, 1.0, "warning")}

    assert diffs == expected  # nosec B101 - pytest assertion

    # in either order: a run made later from an older checkout records the old name too
    mirrored = {d.field: (d.candidate, d.baseline, d.severity)
                for d in check_comparability(new, old).diffs}
    assert mirrored == expected  # nosec B101 - pytest assertion


def test_a_later_run_that_records_an_old_name_shows_it():
    # a container left over from before the rename ran beside the renamed one
    limits = {"cpus": 2.0, "mem_limit": 1073741824}
    new = _env(limits={"chris-broker": limits})
    leftover = _env(limits={"chris-broker": limits, "dragonflydb": limits})
    fields = sorted(d.field for d in check_comparability(new, leftover).diffs)

    assert fields == ["limits.dragonflydb.cpus",  # nosec B101 - pytest assertion
                      "limits.dragonflydb.mem_limit"]


SAMPLER = {"method": "one-shot reads, CPU between consecutive readings", "interval_s": 1.0,
           "queue_interval_s": 0.5, "workers": 8}


def test_runs_sampled_differently_are_a_caution():
    # an archived run records no sampler: one caution, as for any block it lacks
    rows = [(d.field, d.baseline, d.candidate, d.severity)
            for d in check_comparability(_env(), _env(sampler=SAMPLER)).diffs]
    assert rows == [  # nosec B101 - pytest assertion
        ("sampler", "not recorded", "recorded", "warning")]

    slower = _env(sampler={**SAMPLER, "interval_s": 2.0})
    rows = [(d.field, d.baseline, d.candidate, d.severity)
            for d in check_comparability(_env(sampler=SAMPLER), slower).diffs]
    assert rows == [("sampler.interval_s", 1.0, 2.0, "warning")]  # nosec B101 - pytest assertion
    assert not check_comparability(  # nosec B101 - pytest assertion
        _env(sampler=SAMPLER), _env(sampler=SAMPLER)).diffs


# -- delta classification ------------------------------------------------------------------

def test_sub_floor_change_is_unchanged_despite_large_percent():
    # 0.5s -> 0.9s is +80% but under the 2s makespan floor (poll-cadence noise)
    assert classify_delta("makespan_median_s", 0.5, 0.9, POLICY).flag == "UNCHANGED"


def test_change_over_both_thresholds_is_flagged():
    d = classify_delta("makespan_median_s", 10.0, 15.0, POLICY)
    assert d.flag == "REGRESSED" and d.delta == 5.0 and d.pct == 50.0
    assert classify_delta("makespan_median_s", 15.0, 10.0, POLICY).flag == "IMPROVED"


def test_large_absolute_but_small_percent_is_unchanged():
    # +4s on a 100s makespan clears the floor but not the 20% threshold
    assert classify_delta("makespan_median_s", 100.0, 104.0, POLICY).flag == "UNCHANGED"


def test_zero_baseline_uses_floor_only():
    d = classify_delta("worst_p95_ms", 0.0, 80.0, POLICY)
    assert d.flag == "REGRESSED" and d.pct is None


def test_missing_values_flag_as_only_one_run():
    # one-sided values must not masquerade as UNCHANGED (CSV consumers filter on flags)
    assert classify_delta("worst_p95_ms", None, 80.0, POLICY).flag == "—"
    assert classify_delta("makespan_median_s", 5.0, None, POLICY).flag == "—"


# -- level joins & verdicts ------------------------------------------------------------------

def test_verdict_flip_flags_regardless_of_metrics():
    base = [_lvl(verdict="PASS")]
    cand = [_lvl(verdict="FAIL")]
    deltas = compare_levels(base, cand, POLICY)
    assert deltas[0].flag == "REGRESSED"
    deltas = compare_levels(cand, base, POLICY)
    assert deltas[0].flag == "IMPROVED"


def test_verdict_improvement_dominates_metric_regression():
    # pinned behavior: FAIL->PASS makes the level IMPROVED even though makespan
    # regressed past both thresholds — broken->working dominates, and the gate
    # must not fire (the metric rows still show the regression to the reader)
    base = [_lvl(verdict="FAIL", makespan=10.0)]
    cand = [_lvl(verdict="PASS", makespan=50.0)]
    deltas = compare_levels(base, cand, POLICY)
    assert deltas[0].metrics[0].flag == "REGRESSED"       # the metric itself
    assert deltas[0].flag == "IMPROVED"                   # the level
    assert not has_regressions(compare_runs(_run("A", base), _run("B", cand)))


def test_asymmetric_ladders_flag_as_only_one_run():
    base = [_lvl(level=1), _lvl(level=2), _lvl(level=4)]
    cand = [_lvl(level=1), _lvl(level=2)]     # candidate broke earlier
    deltas = compare_levels(base, cand, POLICY)
    assert [d.level for d in deltas] == [1, 2, 4]
    assert deltas[-1].flag == "—"
    assert flag_counts(deltas)["—"] == 1


# -- breaking-point shifts --------------------------------------------------------------------

def _bp(topology="linear", axis="depth", level=8):
    return {"topology": topology, "axis": axis, "level": level, "criteria": ["x"]}


def test_breaking_point_moving_down_is_regression():
    base = _run("A", [_lvl(level=1)], breaking_points=[_bp(level=8)])
    cand = _run("B", [_lvl(level=1)], breaking_points=[_bp(level=4)])
    shifts = breaking_point_shifts(base, cand)
    assert shifts[0].direction == "regressed"


def test_breaking_point_appearing_and_vanishing():
    clean = _run("A", [_lvl(level=1)])
    broken = _run("B", [_lvl(level=1)], breaking_points=[_bp(level=4)])
    assert breaking_point_shifts(clean, broken)[0].direction == "regressed"
    assert breaking_point_shifts(broken, clean)[0].direction == "improved"
    assert breaking_point_shifts(clean, clean)[0].direction == "unchanged"


def test_breaking_point_moving_up_is_improvement():
    base = _run("A", [_lvl(level=1)], breaking_points=[_bp(level=4)])
    cand = _run("B", [_lvl(level=1)], breaking_points=[_bp(level=16)])
    assert breaking_point_shifts(base, cand)[0].direction == "improved"


def test_has_regressions_gate():
    base = _run("A", [_lvl(verdict="PASS")])
    cand = _run("B", [_lvl(verdict="FAIL")])
    assert has_regressions(compare_runs(base, cand))
    assert not has_regressions(compare_runs(base, base))


def test_gate_fires_on_breaking_point_shift_alone():
    # all levels unchanged, but the wall moved down -> still a regression
    base = _run("A", [_lvl(level=1)], breaking_points=[_bp(level=8)])
    cand = _run("B", [_lvl(level=1)], breaking_points=[_bp(level=4)])
    comparison = compare_runs(base, cand)
    assert flag_counts(comparison.level_deltas)["REGRESSED"] == 0
    assert has_regressions(comparison)


# -- rendering ---------------------------------------------------------------------------

def test_render_markdown_headline_and_banner():
    base = _run("A", [_lvl(makespan=10.0)], breaking_points=[_bp(level=8)])
    cand = _run("B", [_lvl(makespan=15.0)], breaking_points=[_bp(level=4)],
                env=_env(api_auth_mode="basic"))
    md = render_markdown(compare_runs(base, cand))
    assert "NOT COMPARABLE" in md
    assert "REGRESSED" in md
    assert "Breaking-point shifts" in md
    assert "linear — depth" in md


def test_self_compare_is_all_unchanged():
    run = _run("A", [_lvl(level=1), _lvl(level=2)])
    comparison = compare_runs(run, run)
    assert comparison.comparability.status == "comparable"
    assert flag_counts(comparison.level_deltas)["UNCHANGED"] == 2
    assert not has_regressions(comparison)


def test_render_csv_is_tidy():
    run_a = _run("A", [_lvl()])
    run_b = _run("B", [_lvl(makespan=15.0)])
    csv = render_csv(compare_runs(run_a, run_b))
    lines = csv.splitlines()
    assert lines[0].startswith("topology,axis,level,metric,baseline,candidate")
    assert len(lines) == 3                  # header + one row per compared metric


def test_render_csv_marks_one_sided_levels():
    # an asymmetric ladder must not surface as UNCHANGED in the tidy CSV
    base = _run("A", [_lvl(level=1), _lvl(level=2)])
    cand = _run("B", [_lvl(level=1)])
    csv = render_csv(compare_runs(base, cand))
    level2_rows = [l for l in csv.splitlines() if l.startswith("linear,depth,2,")]
    assert level2_rows and all(",—," in row for row in level2_rows)
    assert not any("UNCHANGED" in row for row in level2_rows)


def test_trend_tables_track_metric_across_runs():
    runs = [_run("A", [_lvl(level=1, makespan=10), _lvl(level=2, makespan=20)]),
            _run("B", [_lvl(level=1, makespan=11)]),
            _run("C", [_lvl(level=1, makespan=12), _lvl(level=2, makespan=24)],
                 breaking_points=[_bp(level=4)])]
    tables = trend_tables(runs, "makespan_median_s")
    (axis_key, headers, rows), = tables
    assert axis_key == ("linear", "depth")
    assert headers == ["Level", "A", "B", "C"]
    assert rows[0] == [1, 10, 11, 12]
    assert rows[1] == [2, 20, None, 24]     # level missing in run B
    assert rows[-1] == ["breaking point", None, None, 4]


def test_trend_tables_follow_a_dotted_metric_into_nested_values():
    runs = [_run(rid, [dict(_lvl(level=8), peak_write_bytes={"db": written})])
            for rid, written in (("A", 300), ("B", 200), ("C", 100))]
    (_, _, rows), = trend_tables(runs, "peak_write_bytes.db")

    assert rows[0] == [8, 300, 200, 100]  # nosec B101 - pytest assertion


def test_trend_tables_find_a_renamed_service_under_its_old_name_in_older_runs_only():
    later = _env(images={"chris-broker": {"image_id": "sha256:a"}})
    runs = [_run("A", [dict(_lvl(level=8), peak_cpu_pct={"dragonflydb": 7.5})]),
            # a zero peak, and those of a container left over from before the rename
            _run("B", [dict(_lvl(level=8), peak_cpu_pct={"chris-broker": 0.0,
                                                         "dragonflydb": 9.9})], env=later),
            _run("C", [dict(_lvl(level=8), peak_cpu_pct={"dragonflydb": 9.9})], env=later)]
    (_, _, rows), = trend_tables(runs, "peak_cpu_pct.chris-broker")

    assert rows[0] == [8, 7.5, 0.0, None]  # nosec B101 - pytest assertion


# -- CLI ---------------------------------------------------------------------------------

def _write_run(root, run_id, levels, breaking_points=(), env=None):
    store = ResultsStore(root, run_id=run_id)
    store.write_json("summary.json",
                     {"run_id": run_id, "breaking_points": list(breaking_points)})
    store.write_json("environment.json", env if env is not None else _env())
    store.append_jsonl("levels.jsonl", levels)
    return store.run_dir


def test_main_writes_artifacts_and_exit_codes(tmp_path):
    a = _write_run(tmp_path, "A", [_lvl(makespan=10.0)])
    b = _write_run(tmp_path, "B", [_lvl(makespan=15.0)])
    out = tmp_path / "cmp"

    assert main([str(a), str(b), "--out", str(out)]) == 0
    assert (out / "compare.md").exists() and (out / "compare.csv").exists()
    assert "REGRESSED" in (out / "compare.md").read_text()

    # the gate: regressed makespan -> exit 1
    assert main([str(a), str(b), "--out", str(out), "--fail-on-regression"]) == 1
    # self-compare stays clean
    assert main([str(a), str(a), "--out", str(out), "--fail-on-regression"]) == 0


def test_main_policy_flags_can_silence_a_regression(tmp_path):
    a = _write_run(tmp_path, "A", [_lvl(makespan=10.0)])
    b = _write_run(tmp_path, "B", [_lvl(makespan=15.0)])
    out = tmp_path / "cmp"
    code = main([str(a), str(b), "--out", str(out), "--fail-on-regression",
                 "--regress-pct", "60"])
    assert code == 0                        # +50% is below the raised threshold


def test_gate_fails_on_not_comparable_runs(tmp_path):
    # an incomparable pair must not gate green, even with clean deltas
    a = _write_run(tmp_path, "A", [_lvl()])
    b = _write_run(tmp_path, "B", [_lvl()], env=_env(api_auth_mode="basic"))
    out = tmp_path / "cmp"
    assert main([str(a), str(b), "--out", str(out)]) == 0                # no gate: informational
    assert main([str(a), str(b), "--out", str(out), "--fail-on-regression"]) == 1


def test_main_unknown_run_exits_2(tmp_path):
    a = _write_run(tmp_path, "A", [_lvl()])
    assert main([str(a), str(tmp_path / "no-such-run")]) == 2


def test_main_renders_trend_for_three_runs(tmp_path):
    paths = [_write_run(tmp_path, rid, [_lvl(makespan=10.0 + i)])
             for i, rid in enumerate(("A", "B", "C"))]
    out = tmp_path / "cmp"
    assert main([str(p) for p in paths] + ["--out", str(out)]) == 0
    md = (out / "compare.md").read_text()
    assert "## Trend — makespan_median_s across 3 runs" in md
    assert "| Level | A | B | C |" in md


def test_trend_tables_reach_per_process_type_counters_and_tolerate_scalars():
    io_key = "io_write_bytes:relation:client_backend"
    runs = [_run(rid, [dict(_lvl(), db_counters={"workload": {io_key: n}})])
            for n, rid in ((10, "A"), (20, "B"))]
    (_, _, rows), = trend_tables(runs, f"db_counters.workload.{io_key}")

    assert rows[0] == [1, 10, 20]  # nosec B101 - pytest assertion

    # a path through a scalar finds nothing rather than failing
    (_, _, rows), = trend_tables(runs, "makespan_median_s.x")
    assert rows[0] == [1, None, None]  # nosec B101 - pytest assertion


def test_a_trend_of_a_sampled_metric_says_how_each_run_was_sampled(tmp_path):
    old = _write_run(tmp_path, "A", [dict(_lvl(), peak_cpu_pct={"db": 50.0})])
    new = _write_run(tmp_path, "B", [dict(_lvl(), peak_cpu_pct={"db": 90.0})],
                     env=_env(sampler=SAMPLER))
    out = tmp_path / "cmp"

    assert main([str(old), str(new), "--out", str(out),  # nosec B101 - pytest assertion
                 "--metric", "peak_cpu_pct.db"]) == 0
    note = ("_The runs were sampled differently — A: not recorded (the sparse sampler: "
            "lower bounds); B: one-shot reads, CPU between consecutive readings: every "
            "1.0 s, queues every 0.5 s, 8 workers._")
    assert note in (out / "compare.md").read_text()  # nosec B101 - pytest assertion

    # a metric the sampler does not produce gets no note
    assert main([str(old), str(new), "--out", str(out),  # nosec B101 - pytest assertion
                 "--metric", "makespan_median_s"]) == 0
    md = (out / "compare.md").read_text()
    assert "sampled differently" not in md  # nosec B101 - pytest assertion


def _trend_note(tmp_path, envs, metric, row):
    paths = [_write_run(tmp_path, f"R{i}", [dict(_lvl(), **row)], env=env)
             for i, env in enumerate(envs)]
    out = tmp_path / "cmp"
    main([str(p) for p in paths] + ["--out", str(out), "--metric", metric])
    return [line for line in (out / "compare.md").read_text().splitlines()
            if "sampled differently" in line]


def test_a_trend_says_so_whatever_part_of_the_sampler_differs(tmp_path):
    queues = _env(sampler={**SAMPLER, "queue_interval_s": 2.0})
    workers = _env(sampler={**SAMPLER, "workers": 4})
    rows = {"peak_queue_depth.main2": {"peak_queue_depth": {"main2": 5}},
            "peak_mem_bytes.db": {"peak_mem_bytes": {"db": 100}},
            "peak_job_cpu_pct": {"peak_job_cpu_pct": 50.0}}

    for metric, row in rows.items():
        for other in (queues, workers):
            note = _trend_note(tmp_path / metric / str(id(other)),
                               [_env(sampler=SAMPLER), other], metric, row)
            assert len(note) == 1, (metric, other["sampler"])  # nosec B101 - pytest assertion

    # runs sampled alike get no note
    alike = _trend_note(tmp_path / "alike", [_env(sampler=SAMPLER)] * 2,
                        "peak_queue_depth.main2", rows["peak_queue_depth.main2"])
    assert alike == []  # nosec B101 - pytest assertion


def test_main_finds_a_renamed_services_metric_in_older_runs(tmp_path):
    paths = [_write_run(tmp_path, rid, [dict(_lvl(), peak_cpu_pct={"dragonflydb": cpu})])
             for rid, cpu in (("A", 7.5), ("B", 5.2))]
    out = tmp_path / "cmp"

    assert main([str(p) for p in paths]  # nosec B101 - pytest assertion
                + ["--out", str(out), "--metric", "peak_cpu_pct.chris-broker"]) == 0
    md = (out / "compare.md").read_text()
    assert "| 1 | 7.5 | 5.2 |" in md  # nosec B101 - pytest assertion
    assert "not found" not in md  # nosec B101 - pytest assertion


def test_main_renders_a_two_run_trend_for_an_explicit_metric(tmp_path):
    paths = [_write_run(tmp_path, rid, [dict(_lvl(), peak_write_bytes={"db": written})])
             for rid, written in (("A", 300), ("B", 200))]
    out = tmp_path / "cmp"

    assert main([str(p) for p in paths]  # nosec B101 - pytest assertion
                + ["--out", str(out), "--metric", "peak_write_bytes.db"]) == 0
    md = (out / "compare.md").read_text()
    assert "## Trend — peak_write_bytes.db across 2 runs" in md  # nosec B101 - pytest assertion
    assert "| 1 | 300 | 200 |" in md  # nosec B101 - pytest assertion

    # without --metric two runs keep the short report
    assert main([str(p) for p in paths] + ["--out", str(out)]) == 0  # nosec B101 - pytest assertion
    assert "## Trend" not in (out / "compare.md").read_text()  # nosec B101 - pytest assertion


def test_render_markdown_shows_whole_run_counters_side_by_side():
    base = _run("A", [_lvl()])
    base.summary["db_counters"] = {"run": {"num_requested": 17, "wal_bytes": 1000}}
    cand = _run("B", [_lvl()])
    cand.summary["db_counters"] = {"run": {"reset": True, "cgroup_oom_kill": 1}}
    md = render_markdown(compare_runs(base, cand))

    assert "## Postgres over the whole run" in md  # nosec B101 - pytest assertion
    assert "| num_requested | 17 | — | — |" in md  # nosec B101 - pytest assertion
    assert "| reset | — | True | — |" in md  # nosec B101 - pytest assertion
    assert "| cgroup_oom_kill | — | 1 | — |" in md  # nosec B101 - pytest assertion


def test_whole_run_counters_include_wal_zero_fill():
    # a run from before the counter against one with it
    base = _run("A", [_lvl()])
    base.summary["db_counters"] = {"run": {"cgroup_write_bytes": 1000}}
    cand = _run("B", [_lvl()])
    cand.summary["db_counters"] = {"run": {"cgroup_write_bytes": 900,
                                           "wal_init_bytes": 1073741824}}
    md = render_markdown(compare_runs(base, cand))

    assert "| wal_init_bytes | — | 1073741824 | — |" in md  # nosec B101 - pytest assertion
    assert "| cgroup_write_bytes | 1000 | 900 | -10.0% |" in md  # nosec B101 - pytest assertion


def test_render_markdown_omits_run_counters_for_older_runs():
    md = render_markdown(compare_runs(_run("A", [_lvl()]), _run("B", [_lvl()])))

    assert "Postgres over the whole run" not in md  # nosec B101 - pytest assertion


def test_main_trend_accepts_a_dotted_metric(tmp_path):
    paths = [_write_run(tmp_path, rid,
                        [dict(_lvl(), db_counters={"workload": {"num_requested": n}})])
             for n, rid in enumerate(("A", "B", "C"))]
    out = tmp_path / "cmp"

    assert main([str(p) for p in paths]  # nosec B101 - pytest assertion
                + ["--out", str(out), "--metric", "db_counters.workload.num_requested"]) == 0
    md = (out / "compare.md").read_text()
    assert "| 1 | 0 | 1 | 2 |" in md  # nosec B101 - pytest assertion
    assert "not found" not in md  # nosec B101 - pytest assertion


def test_render_markdown_echoes_policy_thresholds():
    md = render_markdown(compare_runs(_run("A", [_lvl()]), _run("B", [_lvl()]),
                                      DeltaPolicy(regress_pct=60.0)))
    assert "Flag thresholds" in md and "60%" in md


# -- workload plugins --------------------------------------------------------------------

DIGEST_REF = "fnndsc/pl-topologicalcopy@sha256:35cb"
REFERENCE, DIGESTS = "workload_plugins.ts.dock_image", "workload_plugins.ts.digests"


def _plugins(version="1.0.13", dock_image=DIGEST_REF, digests=(DIGEST_REF,),
             name="pl-topologicalcopy"):
    image = (None if digests is None
             else {"image_id": "sha256:3329", "repo_digests": list(digests),
                   "architecture": "amd64"})
    return {"ts": {"name": name, "version": version, "id": 3,
                   "dock_image": dock_image, "matches": 1, "image": image}}


def _severities(base, cand, **cand_env):
    c = check_comparability(_env(workload_plugins=base),
                            _env(workload_plugins=cand, **cand_env))
    return c.status, {d.field: d.severity for d in c.diffs}


def test_identical_workload_plugins_are_comparable():
    status, severities = _severities(_plugins(), _plugins())

    assert (status, severities) == ("comparable", {})  # nosec B101 - pytest assertion


def test_another_workload_plugin_name_or_version_is_a_blocker():
    for other, field in ((_plugins(version="2.0.0"), "workload_plugins.ts.version"),
                         (_plugins(name="pl-other"), "workload_plugins.ts.name")):
        status, severities = _severities(_plugins(), other)

        assert status == "not_comparable"  # nosec B101 - pytest assertion
        assert severities == {field: "blocker"}  # nosec B101 - pytest assertion


def test_another_reference_to_the_same_image_is_information():
    # on overlay2 a RepoDigest can carry another repository's name for the same image
    other = _plugins(dock_image="localhost/pl-topologicalcopy",
                     digests=("localhost/pl-topologicalcopy@sha256:35cb",))
    status, severities = _severities(_plugins(), other)

    assert status == "comparable"  # nosec B101 - pytest assertion
    assert severities == {REFERENCE: "info"}  # nosec B101 - pytest assertion


def test_other_image_digests_or_a_local_build_are_a_caution():
    for digests in (("fnndsc/pl-topologicalcopy@sha256:2e1b",), ()):
        status, severities = _severities(_plugins(), _plugins(digests=digests))

        assert status == "caution"  # nosec B101 - pytest assertion
        assert severities == {DIGESTS: "warning"}  # nosec B101 - pytest assertion


def test_two_references_to_other_digests_are_a_caution_without_image_records():
    # neither run could read Docker, but two digests name two images
    other = _plugins(dock_image="fnndsc/pl-topologicalcopy@sha256:2e1b", digests=None)
    status, severities = _severities(_plugins(digests=None), other)

    assert status == "caution"  # nosec B101 - pytest assertion
    assert severities == {REFERENCE: "warning"}  # nosec B101 - pytest assertion


def test_images_recorded_for_some_roles_only_are_a_caution():
    base = {**_plugins(), "fs": {**_plugins()["ts"], "name": "dbg-bigfiles"}}
    cand = {**base, "fs": {**base["fs"], "image": None}}
    status, severities = _severities(base, cand)

    assert status == "caution"  # nosec B101 - pytest assertion
    assert severities == {"workload_plugins.fs.digests": "warning"}  # nosec B101 - pytest assertion


def test_another_pin_with_the_same_plugins_is_information():
    pin = {"path": "benchmarks/chrisomatic/benchmark.yml", "sha256": "ab12"}
    other = {"path": "benchmarks/chrisomatic/laptop.local.yml", "sha256": "cd34"}
    c = check_comparability(_env(workload_plugins=_plugins(), workload_plugins_pin=pin),
                            _env(workload_plugins=_plugins(), workload_plugins_pin=other))
    severities = {d.field: d.severity for d in c.diffs}
    expected = {"workload_plugins_pin.path": "info", "workload_plugins_pin.sha256": "info"}

    assert c.status == "comparable"  # nosec B101 - pytest assertion
    assert severities == expected  # nosec B101 - pytest assertion


def test_a_baseline_without_image_records_gives_a_single_caution():
    # a baseline from before the pin: an untagged reference, no image record and no pin
    baseline = _plugins(dock_image="fnndsc/pl-topologicalcopy", digests=None)
    del baseline["ts"]["image"]
    pin = {"path": "benchmarks/chrisomatic/benchmark.yml", "sha256": "ab12"}
    status, severities = _severities(baseline, _plugins(), workload_plugins_pin=pin)
    expected = {"workload_plugins.ts.dock_image": "info", "workload_plugins.image": "warning",
                "workload_plugins_pin.path": "info", "workload_plugins_pin.sha256": "info"}

    assert status == "caution"  # nosec B101 - pytest assertion
    assert severities == expected  # nosec B101 - pytest assertion


def test_a_run_before_workload_plugins_were_recorded_gives_a_single_caution():
    c = check_comparability(_env(), _env(workload_plugins=_plugins()))
    diffs = [(d.field, d.baseline, d.candidate) for d in c.diffs]
    expected = [("workload_plugins", "not recorded", "recorded")]

    assert diffs == expected  # nosec B101 - pytest assertion
