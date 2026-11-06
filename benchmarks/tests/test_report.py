"""
Tests for benchmarks.report — Markdown/CSV rendering of a run.
"""

from benchmarks.report import _human_bytes, csv_cell, markdown_table, render, render_levels_csv


# -- fixtures --------------------------------------------------------------------------

def _level(**kw):
    base = dict(topology="linear", axis="depth", level=1, verdict="PASS", criteria=[],
                makespan_median_s=1.0, makespan_max_s=1.0, worst_p95_ms=10, errors=0,
                status_5xx=0, completed=2, peak_cpu_pct={"db": 50.0})
    base.update(kw)
    return base


# -- render ----------------------------------------------------------------------------

def test_render_has_sections_and_breaking_point():
    summary = {"run_id": "R", "tier": "smoke", "environment": {}, "levels_run": 2,
               "breaking_points": [{"topology": "linear", "axis": "depth", "level": 2,
                                    "criteria": ["no_progress"]}]}
    md = render(summary, [_level(),
                          _level(level=2, verdict="FAIL", criteria=["no_progress"])])
    assert "Breaking points" in md
    assert "Approach to failure" in md
    assert "no_progress" in md


def test_render_shows_aggregate_job_cpu_and_disk_write():
    summary = {"run_id": "R", "tier": "full", "environment": {}, "levels_run": 1,
               "breaking_points": []}
    lv = _level(peak_cpu_pct={"db": 70.0}, peak_job_cpu_pct=250.0,
                peak_write_bytes={"db": 2048})
    md = render(summary, [lv])
    assert "plugin-jobs peak 250.0%" in md              # jobs collapsed to one number
    assert "Peak disk write" in md
    assert "2.0 KiB" in md                              # bytes rendered human-readable


def test_peak_io_note_skips_zero_write_services():
    summary = {"run_id": "R", "tier": "full", "environment": {}, "levels_run": 1,
               "breaking_points": []}
    
    # every service wrote nothing -> no disk-write line at all (the fslink common case)
    md = render(summary, [_level(peak_write_bytes={"db": 0, "chris": 0})])
    assert "Peak disk write" not in md
    
    # mixed -> only the non-zero writer is rendered (zeros filtered out)
    md2 = render(summary, [_level(peak_write_bytes={"db": 1048576, "chris": 0})])
    assert "Peak disk write" in md2 and "db 1.0 MiB" in md2
    assert "chris" not in md2                           # zero-write service omitted


def test_human_bytes_unit_boundaries():
    assert _human_bytes(0) == "0 B"
    assert _human_bytes(512) == "512 B"
    assert _human_bytes(2048) == "2.0 KiB"
    assert _human_bytes(5 * 1024 * 1024) == "5.0 MiB"
    assert _human_bytes(3 * 1024 ** 3) == "3.0 GiB"


def test_render_flags_degraded_basic_auth():
    summary = {"run_id": "R", "tier": "smoke", "levels_run": 0, "breaking_points": [],
               "environment": {"api_auth_mode": "basic"}}
    md = render(summary, [])
    assert "degraded run" in md                       # basic auth must be loud
    summary["environment"]["api_auth_mode"] = "token"
    assert "degraded run" not in render(summary, [])


# -- markdown_table & csv --------------------------------------------------------------

def test_markdown_table_shape_dashes_and_escaping():
    lines = markdown_table(["A", "B"], [[1, None], ["x|y", "p\nq"]])
    assert lines[0] == "| A | B |"
    assert lines[1] == "|---|---|"
    assert lines[2] == "| 1 | — |"                       # None renders as em dash
    assert lines[3] == "| x\\|y | p q |"                 # pipes escaped, newlines spaced


def test_render_workload_plugins_table():
    digest = "sha256:b956e35ee69c249677489b2bf1815a578c129c478bbdc4bff8c4c75535cd3e7f"
    summary = {"run_id": "R", "tier": "smoke", "levels_run": 0, "breaking_points": [],
               "environment": {"workload_plugins": {
                   "fs": {"name": "dbg-bigfiles", "version": "1.0.0", "id": 1,
                          "dock_image": f"fnndsc/dbg-bigfiles@{digest}", "matches": 1,
                          "image": {"image_id": "sha256:1c45", "architecture": "amd64",
                                    "repo_digests": [f"fnndsc/dbg-bigfiles@{digest}"]}},
                   "ts": {"name": "pl-topologicalcopy", "version": "1.0.13", "id": 6,
                          "dock_image": "fnndsc/pl-topologicalcopy", "matches": 1}},
                   "workload_plugins_pin": {"path": "benchmarks/chrisomatic/benchmark.yml",
                                            "sha256": "a73940e8bad12144d6b4"}}}
    md = render(summary, [])
    fs_row = (f"| fs | dbg-bigfiles | 1.0.0 | fnndsc/dbg-bigfiles@{digest} "
              "| sha256:b956e35ee69c | 1 |")
    # a run from before the image records: an em dash
    ts_row = "| ts | pl-topologicalcopy | 1.0.13 | fnndsc/pl-topologicalcopy | — | 1 |"
    pin = "Pinned by `benchmarks/chrisomatic/benchmark.yml` (sha256 a73940e8bad1)."

    assert "### Workload plugins" in md  # nosec B101 - pytest assertion
    assert fs_row in md  # nosec B101 - pytest assertion
    assert ts_row in md  # nosec B101 - pytest assertion
    assert pin in md  # nosec B101 - pytest assertion


def test_render_shortens_each_digest_and_names_an_image_without_any():
    def plugin(digests):
        return {"name": "pl-x", "version": "1", "id": 1, "dock_image": "pl-x", "matches": 1,
                "image": {"image_id": "sha256:aa", "repo_digests": digests}}

    two = ["a/pl-x@sha256:" + "1" * 64, "b/pl-x@sha256:" + "0" * 64]
    summary = {"run_id": "R", "tier": "smoke", "levels_run": 0, "breaking_points": [],
               "environment": {"workload_plugins": {"fs": plugin(two), "ds": plugin([])}}}
    md = render(summary, [])
    digests = "sha256:000000000000, sha256:111111111111"

    assert f"| pl-x | {digests} | 1 |" in md  # nosec B101 - pytest assertion
    # a local build on overlay2
    assert "| pl-x | none | 1 |" in md  # nosec B101 - pytest assertion


def test_render_workload_plugins_of_a_run_before_the_pin():
    summary = {"run_id": "R", "tier": "smoke", "levels_run": 0, "breaking_points": [],
               "environment": {"workload_plugins": {
                   "fs": {"name": "dbg-bigfiles", "version": "1.0.0", "id": 1,
                          "dock_image": "localhost/dbg-bigfiles", "matches": 1}}}}

    assert "Not pinned: resolved by name." in render(summary, [])  # nosec B101 - pytest assertion


def test_render_omits_workload_plugins_when_absent():
    summary = {"run_id": "R", "tier": "smoke", "levels_run": 0, "breaking_points": [],
               "environment": {}}
    assert "Workload plugins" not in render(summary, [])     # back-compat for old runs


def test_render_key_postgres_settings_overrides_and_table_options():
    summary = {"run_id": "R", "tier": "smoke", "levels_run": 0, "breaking_points": [],
               "environment": {"postgres": {
                   "settings": {"work_mem": "32MB", "TimeZone": "UTC", "DateStyle": "ISO"},
                   "key_settings": ["TimeZone", "work_mem"],
                   "altered": ["work_mem"],
                   "table_options": {"core_chrisfile": ["autovacuum_vacuum_scale_factor=0.02"],
                                     "core_chrisfolder": []}}}}
    md = render(summary, [])

    heading = "### PostgreSQL (key settings; environment.json has all)"
    assert heading in md  # nosec B101 - pytest assertion
    assert "| work_mem | 32MB |" in md  # nosec B101 - pytest assertion
    # recorded, but not a key setting
    assert "DateStyle" not in md  # nosec B101 - pytest assertion
    assert "`ALTER SYSTEM` (postgresql.auto.conf): work_mem" in md  # nosec B101 - pytest assertion
    assert "`core_chrisfolder` storage parameters: defaults" in md  # nosec B101 - pytest assertion


def test_render_omits_postgres_settings_and_limits_for_older_runs():
    summary = {"run_id": "R", "tier": "smoke", "levels_run": 0, "breaking_points": [],
               "environment": {}}
    md = render(summary, [])

    assert "PostgreSQL" not in md  # nosec B101 - pytest assertion
    assert "Container limits" not in md  # nosec B101 - pytest assertion


def test_render_container_limits():
    summary = {"run_id": "R", "tier": "smoke", "levels_run": 0, "breaking_points": [],
               "environment": {"limits": {"db": {
                   "cpus": 2.0, "mem_limit": 4 * 1024 ** 3, "memswap_limit": -1,
                   "shm_size": 1024 ** 3}}}}

    row = "| db | 2.0 | 4.0 GiB | unlimited | 1.0 GiB |"
    assert row in render(summary, [])  # nosec B101 - pytest assertion


def test_render_db_counters_per_level_and_for_the_whole_run():
    workload = {"num_done": 3, "num_timed": 4, "num_requested": 0,
                "wal_bytes": 3 * 1024 ** 3, "wal_fpi": 210000, "temp_bytes": 0,
                "cgroup_write_bytes": 20 * 1024 ** 3, "wal_init_bytes": 1024 ** 3,
                "cgroup_pswpout": 12}
    summary = {"run_id": "R", "tier": "full", "environment": {}, "levels_run": 1,
               "breaking_points": [],
               "db_counters": {"run": dict(workload, num_requested=1)}}
    md = render(summary, [_level(db_counters={"workload": workload,
                                              "cleanup": {"reset": True}})])

    assert "## Postgres over the whole run" in md  # nosec B101 - pytest assertion
    assert "checkpoints 3 done (4 timed / 1 requested)" in md  # nosec B101 - pytest assertion
    note = ("_Postgres at level 1: workload checkpoints 3 done (4 timed / 0 requested), "
            "WAL 3.0 GiB (210,000 full-page images), temp files 0 B, "
            "db container wrote 20.0 GiB, WAL segments zero-filled 1.0 GiB, "
            "swapped out 12 pages; "
            "cleanup Postgres counters n/a (statistics reset")
    assert note in md  # nosec B101 - pytest assertion


def test_render_keeps_the_oom_kill_through_a_postgres_reset():
    # an OOM-killed backend crash-restarts Postgres: its counters are unknown, the
    # container's are not, and the OOM kill is the point
    summary = {"run_id": "R", "tier": "full", "environment": {}, "levels_run": 0,
               "breaking_points": [],
               "db_counters": {"run": {"reset": True, "cgroup_oom_kill": 1,
                                       "cgroup_write_bytes": 1024}}}
    md = render(summary, [])

    assert "Postgres counters n/a (statistics reset" in md  # nosec B101 - pytest assertion
    assert "db container wrote 1.0 KiB, **1 OOM kill(s)**" in md  # nosec B101 - pytest assertion


def test_render_shows_wal_zero_fill_only_when_there_was_some():
    from benchmarks.report import _counters_summary

    line = _counters_summary({"num_done": 1, "cgroup_write_bytes": 2048,
                              "wal_init_bytes": 1024})
    assert "WAL segments zero-filled 1.0 KiB" in line  # nosec B101 - pytest assertion

    # none in this window (a warm pool), or a run that predates the counter
    for delta in ({"num_done": 1, "cgroup_write_bytes": 2048, "wal_init_bytes": 0},
                  {"num_done": 1, "cgroup_write_bytes": 2048}):
        assert "zero-filled" not in _counters_summary(delta)  # nosec B101 - pytest assertion


def test_render_prefers_the_reset_message_when_both_flags_are_set():
    # a level whose repeats saw a reset in one window and Postgres unreadable in another
    rows = [_level(db_counters={"workload": {"reset": True, "unreadable": True,
                                             "cgroup_oom_kill": 1}})]
    summary = {"run_id": "R", "tier": "full", "environment": {}, "levels_run": 1,
               "breaking_points": []}
    md = render(summary, rows)

    assert "statistics reset in between" in md  # nosec B101 - pytest assertion
    assert "unreadable at one end" not in md  # nosec B101 - pytest assertion
    assert "**1 OOM kill(s)**" in md  # nosec B101 - pytest assertion


def test_render_skips_db_counters_when_absent_or_unreadable():
    summary = {"run_id": "R", "tier": "smoke", "environment": {}, "levels_run": 1,
               "breaking_points": []}
    assert "Postgres" not in render(summary, [_level()])  # nosec B101 - pytest assertion

    # nothing could be read at the start or the end of the run: no delta
    unreadable = dict(summary, db_counters={"run": None, "snapshots": {}})
    md = render(unreadable, [_level(db_counters={})])
    assert "Postgres" not in md  # nosec B101 - pytest assertion


def test_render_never_shows_unread_postgres_counters_as_zeros():
    # Postgres was unreadable at one end; only the container's counters are known
    summary = {"run_id": "R", "tier": "full", "environment": {}, "levels_run": 0,
               "breaking_points": [],
               "db_counters": {"run": {"unreadable": True, "cgroup_write_bytes": 2048}}}
    md = render(summary, [])

    unread = "Postgres counters n/a (Postgres unreadable at one end"
    assert unread in md  # nosec B101 - pytest assertion
    assert "temp files" not in md  # nosec B101 - pytest assertion
    assert "db container wrote 2.0 KiB" in md  # nosec B101 - pytest assertion


def test_csv_cell_rfc4180_quoting():
    assert csv_cell("plain") == "plain"
    assert csv_cell("a,b") == '"a,b"'
    assert csv_cell('he said "hi"') == '"he said ""hi"""'      # embedded quotes doubled
    # a criteria list joins with "; " (no comma) -> no quoting needed
    assert csv_cell(["no_progress", "api_5xx:1"]) == "no_progress; api_5xx:1"


def test_render_levels_csv_header_and_rows():
    csv = render_levels_csv([_level(), _level(level=2, verdict="FAIL",
                                              criteria=["a,b"])])
    lines = csv.splitlines()
    assert lines[0].startswith("topology,axis,level,verdict")
    assert len(lines) == 3
    assert '"a,b"' in lines[2]                          # criteria with comma is quoted


# -- sampling ----------------------------------------------------------------------------

SAMPLER = {"method": "one-shot reads, CPU between consecutive readings", "interval_s": 1.0,
           "queue_interval_s": 0.5, "workers": 8}
LEVEL_SAMPLER = {"interval_s": 1.0, "queue_interval_s": 0.5, "ticks": 12, "late_ticks": 0,
                 "max_tick_s": 0.03, "reads": 60, "failed_reads": 0, "vanished": 1,
                 "list_failures": 0, "jobs_seen": 2, "queue_ticks": 24,
                 "queue_late_ticks": 0, "queue_failures": 0}


def _summary(env):
    return {"run_id": "R", "tier": "smoke", "environment": env, "levels_run": 1,
            "breaking_points": []}


def test_render_says_how_each_axis_was_sampled():
    md = render(_summary({"sampler": SAMPLER}),
                [_level(sampler=LEVEL_SAMPLER, final_status_counts={"finishedSuccessfully": 2})])

    note = ("_Sampling at level 1: every 1.0 s, 12 ticks (0 skipped, slowest 0.03 s), 60 "
            "reads (0 failed); 2 plugin-job containers seen for 2 instances; queues every "
            "0.5 s, 24 ticks_")
    row = ("| Resource sampler | one-shot reads, CPU between consecutive readings: every "
           "1.0 s, queues every 0.5 s, 8 workers |")
    assert note in md and row in md  # nosec B101 - pytest assertion
    assert "fell behind" not in md  # nosec B101 - pytest assertion


def test_render_names_the_levels_whose_sampling_fell_behind():
    md = render(_summary({}), [_level(sampler=LEVEL_SAMPLER),
                               _level(level=2, sampler={**LEVEL_SAMPLER, "late_ticks": 3}),
                               _level(level=4, sampler={**LEVEL_SAMPLER, "failed_reads": 1})])

    warning = "**Sampling fell behind or failed at level(s) 2, 4**"
    assert warning in md  # nosec B101 - pytest assertion


def test_render_of_a_run_from_before_the_sampler_summary_has_no_sampling_note():
    md = render(_summary({}), [_level()])

    assert "Sampling at level" not in md  # nosec B101 - pytest assertion
    assert "Resource sampler" not in md  # nosec B101 - pytest assertion


def test_the_sampling_note_describes_the_last_level_of_the_axis():
    md = render(_summary({}), [_level(sampler=LEVEL_SAMPLER),
                               _level(level=2, sampler={**LEVEL_SAMPLER, "ticks": 30,
                                                        "queue_interval_s": None},
                                      final_status_counts={"cancelled": 1,
                                                           "finishedSuccessfully": 3})])

    note = ("_Sampling at level 2: every 1.0 s, 30 ticks (0 skipped, slowest 0.03 s), 60 "
            "reads (0 failed); 2 plugin-job containers seen for 4 instances; queues not "
            "sampled_")
    assert note in md  # nosec B101 - pytest assertion


def test_readings_missed_behind_a_hung_read_are_named_and_flagged():
    md = render(_summary({}), [_level(sampler={**LEVEL_SAMPLER, "failed_reads": 1,
                                               "busy_skips": 9})])

    missed = "60 reads (1 failed, 9 readings missed behind a read still running)"
    assert missed in md  # nosec B101 - pytest assertion
    assert "**Sampling fell behind or failed at level(s) 1**" in md  # nosec B101 - pytest assertion


def test_every_kind_of_sampling_trouble_is_flagged():
    for key in ("late_ticks", "failed_reads", "list_failures", "queue_late_ticks",
                "queue_failures", "busy_skips"):
        md = render(_summary({}), [_level(sampler={**LEVEL_SAMPLER, key: 1})])

        warning = "**Sampling fell behind or failed at level(s) 1**"
        assert warning in md, key  # nosec B101 - pytest assertion


def test_the_peak_cpu_note_without_a_plugin_job_peak():
    md = render(_summary({}), [_level(peak_cpu_pct={"db": 70.0}, peak_job_cpu_pct=None)])

    assert "_Peak CPU at level 1: db 70.0%" in md  # nosec B101 - pytest assertion
    assert "plugin-jobs" not in md  # nosec B101 - pytest assertion
