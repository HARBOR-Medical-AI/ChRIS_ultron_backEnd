"""
Tests for benchmarks.pg_waits — the wait-event samples of the database's backends, taken
by one long-lived psql session, and their rollup per window.
"""

import json
import threading
import time
from types import SimpleNamespace

from benchmarks import pg_waits
from benchmarks.pg_stats import HARNESS_MARKER, STATEMENTS_SQL
from benchmarks.pg_waits import (APPLICATION_NAME, TERMINATE_SQL, WAITS_SQL, WaitSampler,
                                 lines, merge_rollups, parse_sample, wait_rollup)


def _sample(at, *backends):
    return {"at": at, "backends": [list(b) for b in backends]}


def _line(sample):
    return json.dumps(sample).encode() + b"\n"


LOCK = ("client backend", "active", "Lock", "transactionid", 3)
IO = ("client backend", "active", "IO", "DataFileRead", 2)
CPU = ("client backend", "active", "CPU", "CPU", 1)
VACUUM = ("autovacuum worker", "active", "Timeout", "VacuumDelay", 1)
OPEN = ("client backend", "idle in transaction", "Client", "ClientRead", 1)
FATAL = b"FATAL:  terminating connection due to administrator command\n"


def test_a_sample_is_a_json_line_with_the_clock_and_the_backends():
    line = json.dumps(_sample(1.5, LOCK))

    assert parse_sample(line) == _sample(1.5, LOCK)  # nosec B101 - pytest assertion
    for other in (FATAL.decode(), "", "0", '{"at" : 1.0', '[1, 2]',
                  '{"at": "x", "backends": []}', '{"at": 1.0, "backends": {}}'):
        assert parse_sample(other) is None  # nosec B101 - pytest assertion


def test_lines_join_a_line_that_spans_two_chunks():
    chunks = [b'{"at": 1', b'}\n{"at"', b': 2}\nFATAL:  x\nlast']

    assert list(lines(chunks)) == [  # nosec B101 - pytest assertion
        '{"at": 1}', '{"at": 2}', 'FATAL:  x', 'last']


def test_the_rollup_of_a_window_sums_each_wait_and_keeps_the_peaks():
    samples = [_sample(9.9, LOCK),                       # before the window
               _sample(10.0, LOCK, IO, CPU),
               _sample(11.0, IO, VACUUM),
               _sample(12.0, OPEN, LOCK, LOCK),
               _sample(13.0, LOCK)]                      # at its end: the next window's

    rollup = wait_rollup(samples, 10.0, 13.0)

    assert rollup == {  # nosec B101 - pytest assertion
        "samples": 3,
        "by_event": {"Lock:transactionid": 9, "IO:DataFileRead": 4, "CPU": 1,
                     "Timeout:VacuumDelay": 1, "idle in transaction": 1},
        "lock_waiters_peak": 6, "backends_peak": 7}
    assert list(rollup["by_event"])[0] == "Lock:transactionid"  # nosec B101 - pytest assertion
    assert wait_rollup([], 0.0, 1.0) == {  # nosec B101 - pytest assertion
        "samples": 0, "by_event": {}, "lock_waiters_peak": 0, "backends_peak": 0}


def test_the_rollups_of_a_levels_repeats_add_up_and_keep_the_largest_peaks():
    first = wait_rollup([_sample(1.0, LOCK), _sample(2.0, IO)], 0.0, 9.0)
    second = wait_rollup([_sample(1.0, IO, IO, CPU)], 0.0, 9.0)

    assert merge_rollups([first, second]) == {  # nosec B101 - pytest assertion
        "samples": 3, "by_event": {"IO:DataFileRead": 6, "Lock:transactionid": 3, "CPU": 1},
        "lock_waiters_peak": 3, "backends_peak": 5}


def test_the_statements_of_the_sampler_stay_out_of_pg_stats():
    for sql in (WAITS_SQL, TERMINATE_SQL):
        assert HARNESS_MARKER in sql  # nosec B101 - pytest assertion
    assert f"NOT ILIKE '%{HARNESS_MARKER}%'" in STATEMENTS_SQL  # nosec B101 - pytest assertion


def test_the_query_leaves_its_own_session_and_idle_backends_out():
    for text in ("pid <> pg_backend_pid()", "state IS DISTINCT FROM 'idle'",
                 "wait_event_type IS DISTINCT FROM 'Activity'"):
        assert text in WAITS_SQL  # nosec B101 - pytest assertion
    for text in (f"application_name = '{APPLICATION_NAME}'", "pid <> pg_backend_pid()"):
        assert text in TERMINATE_SQL  # nosec B101 - pytest assertion


class _Docker:
    """
    Stands in for DockerClient: a db container whose psql session prints ``chunks``, then
    (``watches``) prints psql's last words once a ``pg_terminate_backend`` ends it, or
    (``hangs``) never ends; ``refuse`` and ``unreachable`` are what starting the session
    and ending it raise.
    """

    def __init__(self, chunks, *, watches=True, hangs=False, refuse=None,
                 unreachable=None):
        self.available = True
        self.chunks, self.watches, self.hangs = chunks, watches, hangs
        self.refuse, self.unreachable = refuse, unreachable
        self.ended, self.release = threading.Event(), threading.Event()
        self.execs, self.streams = [], []

    def service_env(self, service):
        return {"POSTGRES_USER": "cube", "POSTGRES_DB": "cube_db"}

    def exec_in_service(self, service, cmd):
        if self.unreachable:
            raise self.unreachable
        self.execs.append((service, cmd))
        if TERMINATE_SQL in cmd:
            self.ended.set()
        return 0, "1"

    def exec_stream(self, service, cmd):
        if self.refuse:
            raise self.refuse
        self.streams.append((service, cmd))
        return self._output()

    def _output(self):
        yield from self.chunks
        if self.hangs:
            self.release.wait(5)
            yield _line(_sample(99.0, LOCK))
        elif self.watches:
            self.ended.wait(5)
            yield FATAL


def _wait_for(condition):
    for _ in range(500):
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met")


def test_the_sampler_runs_one_session_and_ends_it_when_it_stops(monkeypatch):
    first, second = _sample(1.0, LOCK), _sample(2.0, IO)
    raw = _line(first) + _line(second)
    docker = _Docker([b"0\n" + raw[:30], raw[30:]])
    monkeypatch.setattr(pg_waits, "STOP_TIMEOUT_S", 5.0)

    with WaitSampler(docker, service="db", interval=1.0) as sampler:
        _wait_for(lambda: len(sampler._read) == 2)
        began = time.monotonic()

    # the session is ended before its output is waited for, not after a timeout
    assert time.monotonic() - began < 2.0  # nosec B101 - pytest assertion
    assert sampler.samples == [first, second]  # nosec B101 - pytest assertion
    assert sampler.error is None  # nosec B101 - pytest assertion
    (service, cmd), = docker.streams
    assert cmd[:2] == ["sh", "-c"] and service == "db"  # nosec B101 - pytest assertion
    assert f"PGAPPNAME={APPLICATION_NAME}" in cmd[2]  # nosec B101 - pytest assertion
    # psql's stdin: other sampling sessions ended, then the query under \watch
    script = f"{TERMINATE_SQL};\n{WAITS_SQL} \\watch 1\n"
    assert cmd[4:] == [script, "cube", "cube_db"]  # nosec B101 - pytest assertion
    # one exec at the start (the session), one at the end (its termination)
    assert [cmd for _, cmd in docker.execs] == [  # nosec B101 - pytest assertion
        ["psql", "-U", "cube", "-d", "cube_db", "-At", "-F", "\t", "-c", TERMINATE_SQL]]


def test_a_session_that_ended_before_the_scenario_says_when_and_why():
    docker = _Docker([_line(_sample(1.0, LOCK)),
                      b"server closed the connection unexpectedly\n"], watches=False)
    sampler = WaitSampler(docker).start()
    _wait_for(lambda: sampler._ended is not None)
    time.sleep(1.1)

    sampler.stop()

    assert len(sampler.samples) == 1  # nosec B101 - pytest assertion
    assert sampler.error == (  # nosec B101 - pytest assertion
        "the session ended 1 s before the sampling stopped: "
        "server closed the connection unexpectedly")


def test_a_session_that_printed_no_sample_says_why():
    refused = ('psql: error: connection to server on socket "/var/run/postgresql/.s.PGSQL.'
               '5432" failed: FATAL:  sorry, too many clients already')
    docker = _Docker([b"\n" + refused.encode() + b"\n"], watches=False)

    with WaitSampler(docker) as sampler:
        _wait_for(lambda: sampler._ended is not None)

    assert sampler.samples == []  # nosec B101 - pytest assertion
    assert sampler.error.endswith(refused)  # nosec B101 - pytest assertion


def test_an_output_that_breaks_keeps_its_samples_and_says_why():
    def broken():
        yield _line(_sample(1.0, IO))
        raise ConnectionResetError("connection reset by the engine")

    docker = _Docker([])
    docker.exec_stream = lambda service, cmd: broken()

    with WaitSampler(docker) as sampler:
        _wait_for(lambda: sampler._ended is not None)

    assert len(sampler.samples) == 1  # nosec B101 - pytest assertion
    assert sampler.error.endswith("connection reset by the engine")  # nosec B101 - pytest assertion


def test_a_stop_that_cannot_end_the_session_keeps_the_samples_and_never_raises():
    docker = _Docker([_line(_sample(1.0, LOCK))], watches=False)
    sampler = WaitSampler(docker).start()
    _wait_for(lambda: sampler._ended is not None)
    docker.unreachable = RuntimeError("Read timed out")

    sampler.stop()
    sampler.stop()   # once

    assert len(sampler.samples) == 1  # nosec B101 - pytest assertion
    unended = "could not end the session: Read timed out"
    assert unended in sampler.error  # nosec B101 - pytest assertion


def test_a_session_whose_output_does_not_end_is_left_and_its_later_samples_dropped(
        monkeypatch):
    docker = _Docker([_line(_sample(1.0, LOCK))], hangs=True)
    monkeypatch.setattr(pg_waits, "STOP_TIMEOUT_S", 0.2)
    sampler = WaitSampler(docker).start()
    _wait_for(lambda: len(sampler._read) == 1)

    sampler.stop()
    docker.release.set()                                  # a sample after the stop
    _wait_for(lambda: sampler._ended is not None)

    assert [s["at"] for s in sampler.samples] == [1.0]  # nosec B101 - pytest assertion
    assert "did not end within 0.2 s" in sampler.error  # nosec B101 - pytest assertion


def test_without_docker_or_the_container_the_sampler_does_nothing_and_says_why():
    no_engine = SimpleNamespace(available=False, unavailable_reason="permission denied")

    for docker, reason in ((None, "no Docker client"), (no_engine, "permission denied"),
                           (_Docker([], refuse=RuntimeError("service 'db' not running")),
                            "service 'db' not running")):
        with WaitSampler(docker) as sampler:
            pass

        assert (sampler.samples, sampler.error) == ([], reason)  # nosec B101 - pytest assertion


def test_the_record_rolls_up_each_window_that_ran():
    docker = _Docker([_line(_sample(1.0, LOCK)) + _line(_sample(5.0, IO))])

    with WaitSampler(docker, interval=0.5) as sampler:
        _wait_for(lambda: len(sampler._read) == 2)
    record = sampler.record({"workload": (0.0, 4.0), "cleanup": (4.0, 6.0), "other": None})

    sampling = (record["interval_s"], record["samples"], record["error"])
    assert sampling == (0.5, 2, None)  # nosec B101 - pytest assertion
    assert record["workload"]["by_event"] == {  # nosec B101 - pytest assertion
        "Lock:transactionid": 3}
    assert record["cleanup"]["by_event"] == {"IO:DataFileRead": 2}  # nosec B101 - pytest assertion
    assert record["other"] is None  # nosec B101 - pytest assertion
    assert docker.streams[0][1][4].endswith("\\watch 0.5\n")  # nosec B101 - pytest assertion
