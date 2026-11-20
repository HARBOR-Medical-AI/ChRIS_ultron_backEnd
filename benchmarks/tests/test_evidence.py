"""
Tests for the evidence of lost status updates and duplicate job submissions
(HARBOR-planning #152): the instances' parents and feeds, the cancellations only a lost
update explains, the status-queue bound, and what the services' logs show.
"""

import itertools
from types import SimpleNamespace

from benchmarks import run_bench
from benchmarks.compare import check_comparability
from benchmarks.docker_client import DockerClient
from benchmarks.executor import build_feed
from benchmarks.metrics import cancel_evidence
from benchmarks.models import (Classification, InstanceRef, LevelResult, PluginRole,
                              ScenarioParams, ScenarioResult, StatusTimeline, Verdict)
from benchmarks.poller import poll_to_completion
from benchmarks.report import render_levels_csv, render
from benchmarks.run_bench import (_level_row, apply_orphan_cancels, conflicting_jobs,
                                  log_evidence, status_queue_bound)
from benchmarks.tests.test_run_bench import _scenario_runner
from benchmarks.topologies import (build_fanout_fanin, build_layered_diamond, build_linear,
                                   max_width)


def _timeline(iid, final, seen, parents=(), error_code=""):
    tl = StatusTimeline(iid, f"n{iid}", PluginRole.DS, parent_ids=tuple(parents))
    for t, status in enumerate(seen):
        tl.observe(status, float(t))
    tl.final_status, tl.error_code = final, error_code
    return tl


def _params(topology="linear", feeds=1, depth=3, branches=4, layers=2, merges=1):
    return ScenarioParams(topology, "feeds", feeds, 1, "1KiB", depth, branches, layers,
                          merges, feeds, 0, 2.0)


def _result(timelines=(), logs=None, params=None):
    return ScenarioResult(params or _params(), Classification(Verdict.PASS), 1.0, [],
                          list(timelines), {}, {}, {}, 0.0, 1.0, logs=logs or {})


# -- topology width and the status-queue bound -------------------------------------------

def test_the_width_is_the_most_nodes_at_one_depth():
    assert max_width(build_linear(5, 1, "1KiB")) == 1  # nosec B101 - pytest assertion
    fanout = build_fanout_fanin(6, 1, "1KiB", merges=2)
    assert max_width(fanout) == 6  # nosec B101 - pytest assertion
    assert max_width(build_fanout_fanin(2, 1, "1KiB", merges=3)) == 3  # nosec B101 - pytest assertion
    assert max_width(build_layered_diamond(4, 8, 1, "1KiB")) == 4  # nosec B101 - pytest assertion


def test_the_status_queue_bound_is_the_feeds_times_the_width():
    assert status_queue_bound(_params("linear", feeds=64)) == 64  # nosec B101 - pytest assertion
    assert status_queue_bound(_params("diamond", feeds=8, branches=4)) == 32  # nosec B101 - pytest assertion
    assert status_queue_bound(_params("linear", depth=0)) is None  # nosec B101 - pytest assertion


# -- instances know their feed and parents -----------------------------------------------

class _CreateApi:
    def __init__(self):
        self.n = 0

    def create_plugin_instance(self, plugin_id, params):
        self.n += 1
        return {"id": self.n, "feed_id": 7}


def test_built_instances_carry_their_parents_and_timelines_their_feed_too():
    ids = {PluginRole.FS: 1, PluginRole.DS: 2, PluginRole.TS: 3}
    build = build_feed(_CreateApi(), build_fanout_fanin(2, 1, "1KiB"), ids)
    parents = {ref.node_key: ref.parent_ids for ref in build.instances}

    assert parents == {"root": (), "b0": (1,), "b1": (1,), "merge0": (2, 3)}  # nosec B101 - pytest assertion

    api = SimpleNamespace(get_feed_instances=lambda feed_id: [
        {"id": ref.instance_id, "status": "finishedSuccessfully"} for ref in build.instances])
    poll = poll_to_completion(api, [7], build.instances, poll_every=0.001,
                              scenario_timeout=5, no_progress_timeout=5)
    merge = next(tl for tl in poll.timelines if tl.node_key == "merge0")

    assert (merge.feed_id, merge.parent_ids) == (7, (2, 3))  # nosec B101 - pytest assertion


# -- cancellations only a lost update explains -------------------------------------------

def test_a_child_cancelled_while_waiting_under_parents_that_finished_is_an_orphan():
    parent = _timeline(1, "finishedSuccessfully", ["started", "cancelled", "started"])
    orphan = _timeline(2, "cancelled", ["waiting", "cancelled"], parents=[1])

    evidence = cancel_evidence([parent, orphan])

    assert evidence == {"orphan_cancels": [2], "masked_cancels": [1]}  # nosec B101 - pytest assertion


def test_a_cancel_a_failed_parent_or_the_child_s_own_run_explains_is_no_orphan():
    failed = _timeline(1, "finishedWithError", ["started", "finishedWithError"])
    finished = _timeline(2, "finishedSuccessfully", ["started", "finishedSuccessfully"])
    under_failed = _timeline(3, "cancelled", ["waiting", "cancelled"], parents=[1, 2])
    ran = _timeline(4, "cancelled", ["waiting", "copying", "cancelled"], parents=[2])
    unknown_parent = _timeline(5, "cancelled", ["waiting", "cancelled"], parents=[99])
    root = _timeline(6, "cancelled", ["created", "cancelled"])

    evidence = cancel_evidence([failed, finished, under_failed, ran, unknown_parent, root])

    assert evidence == {"orphan_cancels": [], "masked_cancels": []}  # nosec B101 - pytest assertion


def test_a_child_whose_own_copy_failed_between_two_scans_is_no_orphan():
    finished = _timeline(1, "finishedSuccessfully", ["started", "finishedSuccessfully"])
    failed_copy = _timeline(2, "cancelled", ["waiting", "cancelled"], parents=[1],
                            error_code="CODE18")

    assert cancel_evidence([finished, failed_copy])["orphan_cancels"] == []  # nosec B101 - pytest assertion


def test_a_child_whose_registration_failed_unseen_without_a_code_is_no_orphan():
    finished = _timeline(1, "finishedSuccessfully", ["started", "finishedSuccessfully"])
    ran_unseen = _timeline(2, "cancelled", ["waiting", "cancelled"], parents=[1])
    ran_unseen.submitted = True  # the pfcon responses of its jobs were there

    assert cancel_evidence([finished, ran_unseen])["orphan_cancels"] == []  # nosec B101 - pytest assertion


def test_the_poller_records_whether_a_job_of_an_instance_went_out():
    refs = [InstanceRef(iid, 7, f"n{iid}", PluginRole.DS) for iid in (1, 2, 3)]
    scans = itertools.chain(
        [[{"id": 1, "status": "started", "raw": "eJzLSM3JyQcABiwCFQ=="},
          {"id": 2, "status": "waiting", "raw": ""}, {"id": 3, "status": "waiting"}]],
        itertools.repeat([{"id": 1, "status": "cancelled", "raw": ""},
                          {"id": 2, "status": "cancelled", "raw": ""},
                          {"id": 3, "status": "cancelled"}]))
    api = SimpleNamespace(get_feed_instances=lambda feed_id: next(scans))

    poll = poll_to_completion(api, [7], refs, poll_every=0.001, scenario_timeout=5,
                              no_progress_timeout=5)

    assert {tl.instance_id: tl.submitted for tl in poll.timelines} == {  # nosec B101 - pytest assertion
        1: True, 2: False, 3: False}


def test_a_cancel_undone_then_done_again_is_masked_and_a_relabel_is_not():
    undone = _timeline(1, "cancelled", ["started", "cancelled", "registeringFiles",
                                        "cancelled"])
    relabelled = _timeline(2, "cancelled", ["started", "finishedSuccessfully", "cancelled"])

    assert cancel_evidence([undone, relabelled])["masked_cancels"] == [1]  # nosec B101 - pytest assertion


def test_a_cancel_undone_into_a_status_seen_before_is_masked_too():
    # the lost update of HARBOR-planning #152: a stale write puts back 'started'
    undone = _timeline(1, "cancelled", ["started", "cancelled", "started", "cancelled"])

    assert [s for s, _ in undone.history] == ["started", "cancelled", "started",  # nosec B101 - pytest assertion
                                              "cancelled"]
    assert cancel_evidence([undone])["masked_cancels"] == [1]  # nosec B101 - pytest assertion


def test_a_timeline_without_its_history_is_read_in_first_seen_order():
    older = StatusTimeline(1, "n1", PluginRole.DS,
                           first_seen={"started": 1.0, "cancelled": 2.0,
                                       "registeringFiles": 3.0},
                           final_status="cancelled")

    assert cancel_evidence([older])["masked_cancels"] == [1]  # nosec B101 - pytest assertion


def test_the_poller_keeps_the_error_code_of_the_last_read():
    refs = [InstanceRef(5, 7, "n", PluginRole.DS)]
    api = SimpleNamespace(get_feed_instances=lambda feed_id: [
        {"id": 5, "status": "cancelled", "error_code": "CODE01"}])
    poll = poll_to_completion(api, [7], refs, poll_every=0.001, scenario_timeout=5,
                              no_progress_timeout=5)

    assert poll.timelines[0].error_code == "CODE01"  # nosec B101 - pytest assertion


def test_an_orphan_cancel_is_a_hard_failure_named_for_its_cause():
    clean = Classification(Verdict.PASS)
    failed = apply_orphan_cancels(Classification(Verdict.FAIL, ("cancelled:inst-2",)), [2])

    assert apply_orphan_cancels(clean, []) is clean  # nosec B101 - pytest assertion
    assert failed.criteria == ("cancelled:inst-2", "orphan_cancel:1")  # nosec B101 - pytest assertion
    assert apply_orphan_cancels(clean, [5, 6]).verdict is Verdict.FAIL  # nosec B101 - pytest assertion


# -- the services' logs ------------------------------------------------------------------

class _LoggingContainer:
    def __init__(self, service, chunks):
        self.labels = {"com.docker.compose.service": service}
        self.name = service
        self.chunks = chunks
        self.calls = []

    def logs(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.chunks, Exception):
            raise self.chunks
        return iter(self.chunks)


def test_log_volume_streams_without_following_and_counts_lines_across_chunks():
    container = _LoggingContainer("pfcon", [b"a 409 already in", b" use\nok\nalready",
                                            b" in use"])
    size, matches = DockerClient.log_volume(None, container, 12.5, b"already in use")

    assert (size, matches) == (38, [b"a 409 already in use", b"already in use"])  # nosec B101 - pytest assertion
    assert container.calls == [{"stream": True, "follow": False, "since": 12.5}]  # nosec B101 - pytest assertion


def _docker(containers):
    client = DockerClient.__new__(DockerClient)
    client._docker = object()
    client.listings = []
    client.service_containers = lambda **kwargs: client.listings.append(kwargs) or containers
    return client


def _conflict(name):
    # as pfcon logs Docker's refusal, the quotes escaped inside its exception's text
    return (b'docker.errors.APIError: 409 Client Error: Conflict ("Conflict. The container '
            b'name \\"/' + name + b'\\" is already in use by container \\"abc\\"")\n')


def test_log_evidence_counts_each_worker_s_bytes_and_pfcon_s_conflicts():
    containers = [_LoggingContainer("worker-mains", [b"x" * 10, b"y\n"]),
                  _LoggingContainer("worker-periodic", OSError("gone")),
                  _LoggingContainer("chris", [b"not measured\n"]),
                  _LoggingContainer("pfcon", [_conflict(b"chris-jid-1"),
                                              _conflict(b"chris-jid-1-delete"),
                                              b"fine\n"])]
    client = _docker(containers)

    evidence = log_evidence(client, 3.0, ("worker-mains", "worker-periodic", "worker-heavy"))

    assert evidence == {"worker_log_bytes": {"worker-mains": 12},  # nosec B101 - pytest assertion
                        "worker_logs_unread": ["worker-heavy", "worker-periodic"],
                        "pfcon_conflicts": 1,
                        "pfcon_conflicts_by_job": {"plugin": 1, "delete": 1}}
    assert client.listings == [{"all_states": True}]  # nosec B101 - pytest assertion


def test_the_conflicts_of_every_pfcon_container_add_up_and_unnamed_ones_count():
    containers = [_LoggingContainer("pfcon", [_conflict(b"chris-jid-1")]),
                  _LoggingContainer("pfcon", [b"409 ... already in use, somehow\n",
                                              _conflict(b"chris-jid-2-delete")]),
                  _LoggingContainer("pfcon", [b"fine\n"])]  # one left stopped, say

    evidence = log_evidence(_docker(containers), 1.0, ())

    assert evidence["pfcon_conflicts"] == 2  # nosec B101 - pytest assertion
    assert evidence["pfcon_conflicts_by_job"] == {"plugin": 1, "unknown": 1, "delete": 1}  # nosec B101 - pytest assertion


def test_a_stopped_worker_s_log_adds_to_its_running_one_s():
    containers = [_LoggingContainer("worker-mains", [b"old run\n"]),
                  _LoggingContainer("worker-mains", [b"new\n"])]

    evidence = log_evidence(_docker(containers), 1.0, ("worker-mains",))

    assert evidence["worker_log_bytes"] == {"worker-mains": 12}  # nosec B101 - pytest assertion
    assert evidence["worker_logs_unread"] == []  # nosec B101 - pytest assertion


def test_the_refused_containers_are_told_apart_by_their_job():
    lines = [_conflict(name) for name in (b"chris-jid-7", b"chris-jid-7-copy",
                                          b"chris-jid-7-upload", b"chris-jid-7-delete")]
    lines += [b'name "/chris-jid-8" is already in use', b"already in use, somehow"]

    assert conflicting_jobs(lines) == {"plugin": 2, "copy": 1, "upload": 1,  # nosec B101 - pytest assertion
                                       "delete": 1, "unknown": 1}


def test_log_evidence_without_docker_is_empty_and_pfcon_unread_is_none():
    assert log_evidence(None, 1.0) == {}  # nosec B101 - pytest assertion
    evidence = log_evidence(_docker([_LoggingContainer("pfcon", OSError("x"))]), 1.0)
    assert evidence["pfcon_conflicts"] is None  # nosec B101 - pytest assertion


def test_log_evidence_survives_a_container_listing_that_fails():
    client = _docker([])

    def fails(**kwargs):
        raise RuntimeError("the engine timed out")

    client.service_containers = fails

    assert log_evidence(client, 1.0) == {}  # nosec B101 - pytest assertion


def test_a_scenario_measures_the_logs_since_it_started_after_its_cleanup(monkeypatch,
                                                                        tmp_path):
    runner, events = _scenario_runner(monkeypatch, tmp_path, cleanup=True)
    monkeypatch.setattr(run_bench, "log_evidence",
                        lambda docker, since: events.append(("logs", since)) or {"x": 1})
    monkeypatch.setattr(run_bench.time, "time", lambda: 42.0)
    result = runner.run_scenario(run_bench.make_scenario_params(
        runner.tier.baseline, 2.0, "linear", "depth", 1, 0))
    order = [e[0] if isinstance(e, tuple) and e[0] == "logs" else e for e in events
             if isinstance(e, str) or e[0] == "logs"]

    assert order[-4:] == ["cleanup", "snapshot", "logs", "persist"]  # nosec B101 - pytest assertion
    assert ("logs", 42.0) in events and result.logs == {"x": 1}  # nosec B101 - pytest assertion


def test_a_scenario_reads_the_logs_from_its_own_start(monkeypatch, tmp_path):
    runner, events = _scenario_runner(monkeypatch, tmp_path, cleanup=True)
    clock = itertools.count(100.0)
    monkeypatch.setattr(run_bench.time, "time", lambda: next(clock))
    monkeypatch.setattr(run_bench, "log_evidence",
                        lambda docker, since: events.append(("logs", since)) or {})
    result = runner.run_scenario(run_bench.make_scenario_params(
        runner.tier.baseline, 2.0, "linear", "depth", 1, 0))

    assert result.started_at < result.ended_at  # nosec B101 - pytest assertion
    assert ("logs", result.started_at) in events  # nosec B101 - pytest assertion


# -- the level row and the report --------------------------------------------------------

def test_the_level_row_adds_up_the_evidence_of_its_repeats():
    orphaned = [_timeline(1, "finishedSuccessfully", ["started"]),
                _timeline(2, "cancelled", ["waiting", "cancelled"], parents=[1])]
    reps = [_result(orphaned, {"worker_log_bytes": {"worker-mains": 10, "worker-periodic": 1},
                               "pfcon_conflicts": 2,
                               "pfcon_conflicts_by_job": {"plugin": 2, "delete": 1}},
                    _params(feeds=4)),
            _result((), {"worker_log_bytes": {"worker-mains": 30}, "pfcon_conflicts": 0,
                         "pfcon_conflicts_by_job": {"delete": 2}},
                    _params(feeds=4))]

    row = _level_row(LevelResult("linear", "feeds", 4, Verdict.FAIL, (), reps))

    assert row["pfcon_conflicts"] == 2 and row["orphan_cancels"] == 1  # nosec B101 - pytest assertion
    assert row["pfcon_conflicts_by_job"] == {"plugin": 2, "delete": 3}  # nosec B101 - pytest assertion
    assert row["masked_cancels"] == 0 and row["status_queue_bound"] == 4  # nosec B101 - pytest assertion
    # the services of the repeat that logged the most, and their total
    assert row["worker_log_bytes"] == {"worker-mains": 30}  # nosec B101 - pytest assertion
    assert row["worker_log_bytes_total"] == 30  # nosec B101 - pytest assertion


def test_a_level_with_a_repeat_whose_logs_were_not_read_counts_no_conflicts():
    reps = [_result((), {"worker_log_bytes": {"worker-mains": 5}, "pfcon_conflicts": 0}),
            _result((), {"worker_log_bytes": {}, "pfcon_conflicts": None}),
            _result()]

    row = _level_row(LevelResult("linear", "feeds", 1, Verdict.PASS, (), reps))

    assert row["pfcon_conflicts"] is None  # nosec B101 - pytest assertion
    assert row["worker_log_bytes_total"] == 5  # nosec B101 - pytest assertion


def test_a_level_whose_worker_logs_could_not_be_read_says_none():
    unread = log_evidence(_docker([_LoggingContainer("worker-mains", OSError("x"))]), 1.0)
    row = _level_row(LevelResult("linear", "feeds", 1, Verdict.PASS, (),
                                 [_result(logs=unread)]))

    assert row["worker_log_bytes_total"] is None  # nosec B101 - pytest assertion


def test_a_level_with_a_worker_log_unread_in_any_repeat_has_no_total():
    reps = [_result((), {"worker_log_bytes": {"worker-mains": 50}, "worker_logs_unread": []}),
            _result((), {"worker_log_bytes": {"worker-mains": 5},
                         "worker_logs_unread": ["worker-heavy"]})]

    row = _level_row(LevelResult("linear", "feeds", 1, Verdict.PASS, (), reps))

    assert row["worker_log_bytes"] == {"worker-mains": 50}  # nosec B101 - pytest assertion
    assert row["worker_logs_unread"] == ["worker-heavy"]  # nosec B101 - pytest assertion
    assert row["worker_log_bytes_total"] is None  # nosec B101 - pytest assertion


def test_the_level_row_takes_the_status_queue_s_fresh_peak_of_every_repeat():
    def with_queue(fresh):
        result = _result()
        result.queues = {"status": {"depth_peak": 90, "depth_peak_fresh": fresh}}
        return result

    row = _level_row(LevelResult("linear", "feeds", 1, Verdict.PASS, (),
                                 [with_queue(3), with_queue(5)]))
    unknown = _level_row(LevelResult("linear", "feeds", 1, Verdict.PASS, (),
                                     [with_queue(3), _result()]))

    assert row["status_queue_peak_fresh"] == 5  # nosec B101 - pytest assertion
    assert unknown["status_queue_peak_fresh"] is None  # nosec B101 - pytest assertion


def test_a_level_without_log_evidence_says_none_rather_than_zero():
    row = _level_row(LevelResult("linear", "feeds", 1, Verdict.PASS, (), [_result()]))

    assert (row["pfcon_conflicts"], row["worker_log_bytes_total"]) == (None, None)  # nosec B101 - pytest assertion


def _summary():
    return {"run_id": "r", "tier": "full", "environment": {}, "levels_run": 2,
            "breaking_points": []}


def test_the_report_names_the_levels_with_evidence_and_the_last_log_volume():
    rows = [{"topology": "linear", "axis": "feeds", "level": 64, "pfcon_conflicts": 3,
             "pfcon_conflicts_by_job": {"plugin": 3, "delete": 2},
             "orphan_cancels": 0, "status_queue_bound": 64,
             "peak_queue_depth": {"status": 700}, "status_queue_peak_fresh": 70,
             "worker_log_bytes_total": 2048, "worker_log_bytes": {"worker-mains": 2048}},
            {"topology": "linear", "axis": "feeds", "level": 128, "masked_cancels": 1,
             # above the bound only with expired checks in the list
             "status_queue_bound": 128, "peak_queue_depth": {"status": 900},
             "status_queue_peak_fresh": 90,
             "worker_log_bytes_total": 5 * 1024 * 1024,
             "worker_log_bytes": {"worker-mains": 5 * 1024 * 1024, "worker-heavy": 1024}}]

    text = render(_summary(), rows)

    assert "refused ('already in use')** at level(s) 64: 3." in text  # nosec B101 - pytest assertion
    assert "delete job was submitted twice at once" in text  # nosec B101 - pytest assertion
    assert "another status** at level(s) 128: 1." in text  # nosec B101 - pytest assertion
    assert "no check in it had expired" in text  # nosec B101 - pytest assertion
    assert "at level(s) 64: 70 > 64." in text and "900" not in text  # nosec B101 - pytest assertion
    assert ("_Celery workers logged 5.0 MiB in a scenario of level 128 (worker-mains "  # nosec B101 - pytest assertion
            "5.0 MiB, worker-heavy 1.0 KiB)_") in text
    assert "children cancelled" not in text  # nosec B101 - pytest assertion


def test_the_report_says_when_the_last_level_s_log_volume_is_unknown():
    rows = [{"topology": "linear", "axis": "feeds", "level": 8,
             "worker_log_bytes_total": None, "worker_log_bytes": {"worker-mains": 1},
             "worker_logs_unread": ["worker-heavy"]}]

    text = render(_summary(), rows)

    assert "log volume at level 8 is unknown: worker-heavy could not be read" in text  # nosec B101 - pytest assertion


def test_a_run_before_these_measurements_shows_no_evidence_lines():
    rows = [{"topology": "linear", "axis": "feeds", "level": 1,
             "peak_queue_depth": {"main2": 5}}]

    text = render(_summary(), rows)

    assert "pfcon refused" not in text and "Celery workers logged" not in text  # nosec B101 - pytest assertion


def test_the_levels_csv_has_the_evidence_columns():
    header = render_levels_csv([]).splitlines()[0].split(",")

    assert header[-6:] == ["pfcon_conflicts", "orphan_cancels", "masked_cancels",  # nosec B101 - pytest assertion
                           "worker_log_bytes_total", "status_queue_bound",
                           "status_queue_peak_fresh"]
    assert InstanceRef(1, 2, "k", PluginRole.FS).parent_ids == ()  # nosec B101 - pytest assertion


# -- the Celery services' command lines ----------------------------------------------------

def test_the_celery_services_command_lines_are_recorded_as_the_engine_ran_them():
    def container(service, cmd):
        return SimpleNamespace(labels={"com.docker.compose.service": service}, name=service,
                               attrs={"Config": {"Cmd": cmd}})

    client = DockerClient.__new__(DockerClient)
    client.service_containers = lambda: [
        container("worker-mains", ["celery", "-A", "core", "worker", "-c", "4"]),
        container("celery-scheduler", ["celery", "-A", "core", "beat"]),
        container("db", ["postgres"])]

    assert client.service_commands() == {  # nosec B101 - pytest assertion
        "worker-mains": ["celery", "-A", "core", "worker", "-c", "4"],
        "celery-scheduler": ["celery", "-A", "core", "beat"]}


def test_another_worker_command_line_is_a_caution():
    def env(concurrency):
        return {"commands": {"worker-heavy": ["celery", "worker", "-c", concurrency]}}

    diffs = check_comparability(env("2"), env("4")).diffs

    assert [(d.field, d.baseline, d.candidate, d.severity) for d in diffs  # nosec B101 - pytest assertion
            if d.field.startswith("commands")] == [
        ("commands.worker-heavy", "celery worker -c 2", "celery worker -c 4", "warning")]
    unrecorded = [d.field for d in check_comparability({}, env("2")).diffs]
    assert "commands" in unrecorded  # nosec B101 - pytest assertion
