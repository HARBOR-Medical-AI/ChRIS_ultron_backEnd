"""
Per-scenario wait events of the database's backends (best-effort).

Tells lock waits apart from I/O and CPU: once a second, for the whole scenario (its
workload and its cleanup), the backends of the db container that are not idle are counted
by process type, state and wait event, which gives ``scenarios/<id>/pg_waits.jsonl`` and a
rollup per window in ``scenario.json``. The wait event says what a backend waits on
(``Lock`` for a lock on a row, a transaction or a table, ``LWLock``, ``IO``, ``IPC``,
``Timeout``...); a backend that waits on nothing runs on CPU, counted as ``CPU``.

The samples come from one psql session that runs the query under ``\\watch`` for the whole
scenario rather than from a psql per sample, which would add a new session every second
to the rate HARBOR-planning #162 measures. The session names itself ``APPLICATION_NAME``,
leaves itself out of its samples and, before its first, ends any other of that name (one
that a harness that died left running); the harness ends it with ``pg_terminate_backend``.
Like ``pg_stats``, it runs psql inside the db container through the Docker socket.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from typing import Iterable, Iterator, Optional

from .docker_client import DockerClient
from .pg_stats import psql, psql_role


#: the name the sampling session gives itself, by which it is ended
APPLICATION_NAME = "cube-bench-waits"

#: the columns of each entry of a sample's ``backends``: the backends of one process type
#: in one state waiting on one event, and how many they are
BACKEND_COLUMNS = ("backend_type", "state", "wait_event_type", "wait_event", "count")

#: one JSON document per sample: the server clock (``at``, epoch seconds) and the
#: ``backends`` that are not idle, grouped as ``BACKEND_COLUMNS`` says; ``Activity`` is
#: the wait of a background process with nothing to do, and ``state`` is empty for one.
#: Its column is named ``pg_stats.HARNESS_MARKER``, which keeps it out of pg_stats.json.
WAITS_SQL = (
    "SELECT json_build_object('at', extract(epoch FROM clock_timestamp()), 'backends', "
    "coalesce((SELECT json_agg(json_build_array(backend_type, state, wait_event_type, "
    "wait_event, n) ORDER BY n DESC, backend_type, state, wait_event_type, wait_event) "
    "FROM (SELECT backend_type, coalesce(state, '') AS state, "
    "coalesce(wait_event_type, 'CPU') AS wait_event_type, "
    "coalesce(wait_event, 'CPU') AS wait_event, count(*) AS n "
    "FROM pg_stat_activity WHERE pid <> pg_backend_pid() "
    "AND state IS DISTINCT FROM 'idle' AND wait_event_type IS DISTINCT FROM 'Activity' "
    "GROUP BY 1, 2, 3, 4) b), '[]')) AS bench_harness"
)

#: ends every sampling session but the one that runs it
TERMINATE_SQL = (
    "SELECT count(pg_terminate_backend(pid)) AS bench_harness FROM pg_stat_activity "
    "WHERE application_name = 'cube-bench-waits' AND pid <> pg_backend_pid()"
)

#: psql reads its statements from stdin: psql 18 refuses a ``\\watch`` given with ``-c``
#: after the query ("cannot be used with an empty query")
_SHELL = (f'printf "%s" "$1" | PGAPPNAME={APPLICATION_NAME} '
          'exec psql -X -At -U "$2" -d "$3"')

#: how long ``stop`` waits for the session's output to end once it terminated it
STOP_TIMEOUT_S = 10.0


def parse_sample(line: str) -> Optional[dict]:
    """
    The sample a line of the session's output holds, or None for any other line (psql's
    messages, a line cut short).
    """
    try:
        doc = json.loads(line)
    except ValueError:
        return None

    if (not isinstance(doc, dict) or not isinstance(doc.get("at"), (int, float))
            or not isinstance(doc.get("backends"), list)):
        return None
    return {"at": doc["at"], "backends": doc["backends"]}


def lines(chunks: Iterable[bytes]) -> Iterator[str]:
    """
    The lines of a stream that comes in chunks, a line that spans two chunks whole.
    """
    pending = b""

    for chunk in chunks:
        pending += chunk
        *complete, pending = pending.split(b"\n")
        yield from (line.decode("utf-8", "replace") for line in complete)

    if pending:
        yield pending.decode("utf-8", "replace")


def wait_key(row: dict) -> str:
    """
    What a sample's backends waited on, as a rollup counts it:
    ``<wait_event_type>:<wait_event>``, ``CPU`` for nothing, and the state of a backend
    idle in a transaction (it waits for its client, holding the transaction's locks).
    """
    if row["state"].startswith("idle in transaction"):
        return row["state"]
    if row["wait_event_type"] == "CPU":
        return "CPU"
    return f"{row['wait_event_type']}:{row['wait_event']}"


def wait_rollup(samples: Iterable[dict], start: float, end: float) -> dict:
    """
    The samples taken from ``start`` to ``end`` (epoch seconds, ``end`` excluded) rolled
    up: how many there are, how many backends each wait (``wait_key``) had, summed over
    them (backend-samples: about backend-seconds at one sample a second), most first, and
    the most backends one sample found waiting on a lock (``Lock``) and not idle.
    """
    events: Counter = Counter()
    count = lock_peak = busy_peak = 0

    for sample in samples:
        if not start <= sample["at"] < end:
            continue

        count += 1
        locks = busy = 0

        for backend in sample["backends"]:
            row = dict(zip(BACKEND_COLUMNS, backend))
            events[wait_key(row)] += row["count"]
            locks += row["count"] if row["wait_event_type"] == "Lock" else 0
            busy += row["count"]

        lock_peak, busy_peak = max(lock_peak, locks), max(busy_peak, busy)

    return {"samples": count, "by_event": dict(events.most_common()),
            "lock_waiters_peak": lock_peak, "backends_peak": busy_peak}


def merge_rollups(rollups: Iterable[dict]) -> dict:
    """
    Several rollups of ``wait_rollup`` (a level's repeats) as one: the samples and each
    wait's backend-samples added up, the peaks the largest.
    """
    events: Counter = Counter()
    count = lock_peak = busy_peak = 0

    for rollup in rollups:
        count += rollup["samples"]
        events.update(rollup["by_event"])
        lock_peak = max(lock_peak, rollup["lock_waiters_peak"])
        busy_peak = max(busy_peak, rollup["backends_peak"])

    return {"samples": count, "by_event": dict(events.most_common()),
            "lock_waiters_peak": lock_peak, "backends_peak": busy_peak}


class WaitSampler:
    """
    Samples the wait events while it runs (``start``/``stop``, or a ``with`` block), every
    ``interval`` seconds, in a psql session of its own inside the db container. It does
    nothing when Docker or the container is not there. ``error`` says why when there is
    no sample, when the session ended before ``stop`` (a crash restart of Postgres, a
    restart of the container), or when ``stop`` could not end it.
    """

    def __init__(self, docker: Optional[DockerClient], *, service: str = "db",
                 interval: float = 1.0):
        self.interval = interval
        self.samples: list[dict] = []
        self.error: Optional[str] = None
        self._docker, self._service = docker, service
        self._env: Optional[dict] = None
        self._thread: Optional[threading.Thread] = None
        self._read: list[dict] = []
        self._last_line = ""
        self._ended: Optional[float] = None   # when the session's output ended

    def start(self) -> WaitSampler:
        docker = self._docker

        if docker is None or not docker.available:
            self.error = getattr(docker, "unavailable_reason", None) or "no Docker client"
            return self

        try:
            self._env = docker.service_env(self._service)
            output = docker.exec_stream(self._service, self._command())
        except Exception as exc:                       # noqa: BLE001 - best effort
            self.error = str(exc)[:300] or type(exc).__name__
            return self

        self._thread = threading.Thread(target=self._collect, args=(output,),
                                        name="wait-sampler", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """
        End the session and keep what it sampled; a session whose output does not end
        within ``STOP_TIMEOUT_S`` is left to its thread, whose later samples are dropped.
        Never raises, and stops once however many times it is called.
        """
        if self._thread is None:
            return

        stopping, errors = time.time(), []
        try:
            self._terminate()
        except Exception as exc:                       # noqa: BLE001 - best effort
            errors.append(f"could not end the session: {str(exc)[:200]}")

        thread, self._thread = self._thread, None
        thread.join(STOP_TIMEOUT_S)
        self.samples = list(self._read)
        ended, last = self._ended, self._last_line[:200]

        if thread.is_alive():
            errors.append(f"the session's output did not end within {STOP_TIMEOUT_S:g} s")

        if ended is not None and ended < stopping:
            errors.insert(0, f"the session ended {stopping - ended:.0f} s before the "
                             f"sampling stopped: {last or 'no message'}")
        elif not self.samples:
            errors.insert(0, last or "psql printed no sample")
        self.error = "; ".join(errors) or None

    def __enter__(self) -> WaitSampler:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def record(self, windows: dict[str, Optional[tuple[float, float]]]) -> dict:
        """
        What ``scenario.json`` keeps of the samples: the sampling, and a ``wait_rollup``
        of each window (``name: (start, end)``; ``None`` for a window that did not run).
        """
        return {"interval_s": self.interval, "samples": len(self.samples),
                "error": self.error,
                **{name: None if bounds is None else wait_rollup(self.samples, *bounds)
                   for name, bounds in windows.items()}}

    def _command(self) -> list[str]:
        user, database = psql_role(self._env or {})
        script = f"{TERMINATE_SQL};\n{WAITS_SQL} \\watch {self.interval:g}\n"
        return ["sh", "-c", _SHELL, "sh", script, user, database]

    def _collect(self, output: Iterable[bytes]) -> None:
        try:
            for line in lines(output):
                sample = parse_sample(line)

                if sample is not None:
                    self._read.append(sample)
                elif line.strip() and not line.strip().isdigit():  # not TERMINATE_SQL's
                    self._last_line = line.strip()   # why the session ended, usually
        except Exception as exc:                       # noqa: BLE001 - best effort
            self._last_line = str(exc) or type(exc).__name__
        finally:
            self._ended = time.time()

    def _terminate(self) -> None:
        psql(self._docker, TERMINATE_SQL, service=self._service, env=self._env)
