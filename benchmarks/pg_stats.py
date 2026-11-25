"""
Per-scenario ``pg_stat_statements`` snapshots (best-effort).

Turns "db CPU at 210%" into named queries: the runner resets the statement stats at
scenario start and snapshots every statement recorded since, most total execution time
first, persisting them under ``scenarios/<id>/pg_stats.json``. Keeping all of them, not
just the expensive ones, is what lets a run show that a cheap statement no longer runs.

Runs ``psql`` *inside* the db container via the Docker socket (no client-side
postgres dependency); credentials come from the container's own ``POSTGRES_*`` env.
Requires ``shared_preload_libraries=pg_stat_statements`` — ``postgres/cube.conf`` sets it
on both compose stacks — and degrades to unavailable (with the reason recorded once)
otherwise.

``connection_states`` uses the same access for ``pg_stat_activity``: how many connections
each client holds and in which state, which tells idle pooled connections apart from ones
left ``idle in transaction``.

``server_settings`` and ``server_counters`` use it too: the settings the server runs with,
which the environment manifest records, and its cumulative counters (checkpoints, WAL,
temp files, writes by process type, autovacuum runs), which the runner snapshots around
each scenario, together with the db container's cgroup counters, and turns into per-window
deltas with ``counter_windows``.
"""

from __future__ import annotations

import json
from typing import Iterable, Optional

from .docker_client import CONTAINER_START_KEY, DockerClient


#: the tables whose storage parameters and autovacuum runs are recorded: the two that
#: registering and deleting feeds churn by millions of rows
TRACKED_TABLES = ("core_chrisfile", "core_chrisfolder")

#: snapshot keys that name a statistics epoch: when one differs between two snapshots,
#: Postgres reset its counters in between (a crash restart, or ``pg_stat_reset_shared``)
STATS_EPOCH_KEYS = ("checkpointer_reset", "wal_reset")

#: the server's start time: it changes when the server, and with it the db container,
#: restarts, which also restarts the container's cgroup counters
SERVER_START_KEY = "server_start"

#: the db container's cgroup counters (``DockerClient.cgroup_counters``): a Postgres crash
#: restart keeps the container, so they keep counting across it
CGROUP_PREFIX = "cgroup_"

#: per-process-type write counters (``io_write_bytes:<object>:<process_type>``, spaces as
#: underscores), present in a snapshot only once they are non-zero, so one missing from
#: the earlier snapshot counts from 0; any other counter missing from either snapshot is
#: left out of the delta
IO_PREFIX = "io_write_bytes:"

#: a delta's markers for Postgres counters it cannot give: ``reset`` (its statistics were
#: reset in the window) and ``unreadable`` (psql failed at one end of the window)
UNKNOWN_FLAGS = ("reset", "unreadable")

#: per statement: calls, time, rows, the temp blocks it wrote (a sort or hash that spilled
#: past work_mem) and the WAL it generated, full-page images included
STATEMENT_COLUMNS = ("calls", "total_ms", "mean_ms", "rows", "temp_blks_written",
                     "wal_bytes", "wal_fpi", "query")

#: a name in the text of the statements a harness sampler runs (a column alias), which
#: ``pg_stats.json`` leaves out, as it leaves out the statements that read it
HARNESS_MARKER = "bench_harness"

STATEMENTS_SQL = (
    "SELECT calls, round(total_exec_time)::bigint AS total_ms, "
    "round(mean_exec_time::numeric, 2) AS mean_ms, rows, temp_blks_written, "
    "wal_bytes, wal_fpi, "
    "left(regexp_replace(query, '\\s+', ' ', 'g'), 300) AS query "
    "FROM pg_stat_statements "
    "WHERE query NOT ILIKE '%pg_stat_statements%' AND query NOT ILIKE '%bench_harness%' "
    "ORDER BY total_exec_time DESC"
)

CONNECTION_STATES_SQL = (
    "SELECT coalesce(host(client_addr), 'local'), coalesce(state, 'unknown'), count(*) "
    "FROM pg_stat_activity "
    "WHERE datname = current_database() AND pid <> pg_backend_pid() "
    "GROUP BY 1, 2 ORDER BY 1, 2"
)


def parse_psql_rows(output: str, columns: tuple[str, ...]) -> list[dict]:
    """
    Parse ``psql -At -F<tab>`` output into row dicts (numeric where possible).
    """
    rows = []

    for line in output.splitlines():
        parts = line.split("\t")

        if len(parts) != len(columns):
            continue

        row = {}
        for name, value in zip(columns, parts):
            try:
                row[name] = int(value)
            except ValueError:
                try:
                    row[name] = float(value)
                except ValueError:
                    row[name] = value

        rows.append(row)
    return rows


def psql_role(env: dict) -> tuple[str, str]:
    """
    The user and the database psql connects as inside the db container: its own
    ``POSTGRES_USER`` and ``POSTGRES_DB``, given its environment ``env``.
    """
    return env.get("POSTGRES_USER", "chris"), env.get("POSTGRES_DB", "chris_dev")


def psql(docker: DockerClient, sql: str, *, service: str = "db",
         env: Optional[dict] = None) -> tuple[int, str]:
    """
    Run one statement with ``psql -At -F<tab>`` inside the db container, as its own
    ``POSTGRES_USER`` on its ``POSTGRES_DB`` (read from ``env``, else the container).
    """
    user, database = psql_role(docker.service_env(service) if env is None else env)

    return docker.exec_in_service(
        service, ["psql", "-U", user, "-d", database, "-At", "-F", "\t", "-c", sql])


def connection_states(docker: DockerClient, service: str = "db") -> list[dict]:
    """
    Connections to the database by client address and state; [] when psql cannot run.
    """
    code, out = psql(docker, CONNECTION_STATES_SQL, service=service)

    if code != 0:
        return []
    return parse_psql_rows(out, ("client_addr", "state", "count"))


# -- server settings and cumulative counters ---------------------------------------------

#: one JSON document: the value ``SHOW`` prints for every setting; ``configured``, the
#: settings a configuration file or the command line sets; ``altered``, the settings in
#: effect from ``ALTER SYSTEM``; and the storage parameters of every table in ``public``.
#: A constant, like every query here: ``server_settings`` picks what it needs in Python.
SETTINGS_SQL = (
    "SELECT json_build_object("
    "'settings', (SELECT json_object_agg(name, current_setting(name) ORDER BY name) "
    "FROM pg_settings), "
    "'configured', (SELECT json_agg(name ORDER BY name) FROM pg_settings "
    "WHERE source IN ('configuration file', 'command line')), "
    "'altered', (SELECT json_agg(name ORDER BY name) FROM pg_settings "
    "WHERE sourcefile LIKE '%/postgresql.auto.conf'), "
    "'table_options', (SELECT json_object_agg(relname, coalesce(reloptions, '{}') "
    "ORDER BY relname) FROM pg_class "
    "WHERE relkind = 'r' AND relnamespace = 'public'::regnamespace))"
)

#: one JSON document of the server's cumulative counters: checkpoints, WAL, the current
#: database's temp files and sessions, autovacuum and autoanalyze runs per table in
#: ``public`` (``autovacuum_by_table``, which ``server_counters`` sums over the tracked
#: tables), and the bytes each process type wrote or extended per object
#: (``io_write_bytes:<object>:<process_type>``, from ``pg_stat_io``, non-zero ones only,
#: spaces as underscores so the key passes through a shell unquoted).
#: ``wal_init_bytes`` is the part of the WAL writes that only zero-fills new WAL segment
#: files, paid whenever the WAL between checkpoints outgrows the pool of segments kept
#: for reuse (on a fresh volume, and after lighter load or a restart has shrunk the
#: pool). ``at`` is the server clock, ``server_start`` the ``SERVER_START_KEY`` and the
#: ``*_reset`` keys the ``STATS_EPOCH_KEYS``.
COUNTERS_SQL = (
    "SELECT jsonb_build_object("
    "'at', extract(epoch FROM clock_timestamp()), "
    "'server_start', pg_postmaster_start_time(), "
    "'checkpointer_reset', c.stats_reset, 'num_timed', c.num_timed, "
    "'num_requested', c.num_requested, 'num_done', c.num_done, "
    "'buffers_written', c.buffers_written, "
    "'checkpoint_write_ms', c.write_time, 'checkpoint_sync_ms', c.sync_time, "
    "'wal_reset', w.stats_reset, 'wal_records', w.wal_records, 'wal_fpi', w.wal_fpi, "
    "'wal_bytes', w.wal_bytes, 'wal_buffers_full', w.wal_buffers_full, "
    "'wal_init_bytes', (SELECT coalesce(sum(write_bytes), 0) FROM pg_stat_io "
    "WHERE object = 'wal' AND context = 'init'), "
    "'temp_files', d.temp_files, 'temp_bytes', d.temp_bytes, "
    "'sessions', d.sessions, 'xact_commit', d.xact_commit, "
    "'autovacuum_by_table', (SELECT jsonb_object_agg(relname, "
    "jsonb_build_array(autovacuum_count, autoanalyze_count)) FROM pg_stat_user_tables "
    "WHERE schemaname = 'public')"
    ") || coalesce((SELECT jsonb_object_agg("
    "'io_write_bytes:' || replace(object, ' ', '_') || ':' "
    "|| replace(backend_type, ' ', '_'), written) "
    "FROM (SELECT backend_type, object, "
    "sum(coalesce(write_bytes, 0) + coalesce(extend_bytes, 0)) AS written "
    "FROM pg_stat_io GROUP BY 1, 2) io WHERE written > 0), '{}') "
    "FROM pg_stat_checkpointer c, pg_stat_wal w, pg_stat_database d "
    "WHERE d.datname = current_database()"
)


def _psql_json(docker: DockerClient, sql: str, *, service: str,
               env: Optional[dict]) -> tuple[object, Optional[str]]:
    """
    The JSON document a one-value query prints, and ``None``; or ``None`` and why it
    could not be read (psql's own output, trimmed).
    """
    code, out = psql(docker, sql, service=service, env=env)
    lines = [line for line in out.splitlines() if line.strip()]

    if code == 0 and lines:
        try:
            return json.loads(lines[-1]), None   # exec output also carries psql's stderr
        except ValueError:
            pass
    return None, out.strip()[:300] or f"psql exit {code}"


def server_settings(docker: DockerClient, names: Iterable[str], *,
                    tables: Iterable[str] = TRACKED_TABLES, service: str = "db",
                    env: Optional[dict] = None) -> dict:
    """
    What the server runs with. ``settings`` maps every setting to its value as ``SHOW``
    prints it; values stay strings, so archived runs compare as text, and a setting
    missing from one run means a different server version, never a different source.
    ``key_settings`` names the ones a report shows: ``names`` and every setting a
    configuration file or the command line sets. ``altered`` lists the settings in
    effect from ``ALTER SYSTEM`` (``postgresql.auto.conf`` wins over the configuration
    file), and ``table_options`` holds the storage parameters of ``tables``. Returns
    ``{}`` when psql cannot run.
    """
    doc, _ = _psql_json(docker, SETTINGS_SQL, service=service, env=env)

    if not isinstance(doc, dict):
        return {}

    settings = doc.get("settings") or {}
    key_settings = {name for name in names if name in settings}
    key_settings.update(doc.get("configured") or [])
    options = doc.get("table_options") or {}

    return {"settings": settings,
            "key_settings": sorted(key_settings, key=str.lower),
            "altered": doc.get("altered") or [],
            "table_options": {table: options[table] for table in tables
                              if table in options}}


def server_counters(docker: DockerClient, *, tables: Iterable[str] = TRACKED_TABLES,
                    service: str = "db", env: Optional[dict] = None) -> dict:
    """
    One snapshot of the server's cumulative counters (``COUNTERS_SQL``), with
    ``autovacuum_count`` and ``autoanalyze_count`` summed over ``tables``; ``{}`` when
    psql cannot run or the server predates PostgreSQL 18's ``pg_stat_io`` byte columns.
    Backends publish their counters between transactions, up to ~10 s after they go
    idle, so a snapshot can miss a few seconds of short transactions; a long statement
    shows up when it ends.
    """
    doc, _ = _psql_json(docker, COUNTERS_SQL, service=service, env=env)

    if not isinstance(doc, dict):
        return {}

    per_table = doc.pop("autovacuum_by_table", None) or {}
    runs = [per_table[table] for table in tables if table in per_table]

    if runs:
        doc["autovacuum_count"] = sum(run[0] for run in runs)
        doc["autoanalyze_count"] = sum(run[1] for run in runs)
    return doc


def server_counters_error(docker: DockerClient, *, service: str = "db",
                          env: Optional[dict] = None) -> Optional[str]:
    """
    Why ``server_counters`` reads nothing (psql's output: no such view on a server
    before PostgreSQL 18, too many clients, crash recovery...); ``None`` when it works.
    """
    doc, error = _psql_json(docker, COUNTERS_SQL, service=service, env=env)

    if isinstance(doc, dict):
        return None
    return error or f"unexpected output: {doc!r}"[:300]


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def counter_delta(before: dict, after: dict) -> Optional[dict]:
    """
    ``after - before`` for every numeric counter in both snapshots (an
    ``io_write_bytes:`` counter missing from ``before`` counts from 0), plus the
    ``seconds`` between them; ``None`` when a snapshot is missing.

    Postgres's counters and the db container's cgroup counters reset independently. A
    crash restart, or ``pg_stat_reset_shared``, zeroes Postgres's statistics: the
    ``STATS_EPOCH_KEYS`` change, or a counter goes down, and the delta reads
    ``"reset": True`` instead of Postgres's counters, which are unknown. After a crash
    ``num_requested`` can even read the same, because the end-of-recovery checkpoint is
    counted after the reset. The container keeps running through a crash, so its
    counters, OOM kills included, still have a delta. A server or container restart
    (``SERVER_START_KEY`` or ``CONTAINER_START_KEY`` changes) invalidates both. When
    Postgres could not be read at either end, the delta reads ``"unreadable": True``
    instead of its counters, and the container's counters remain.
    """
    if not before or not after:
        return None

    pg_readable = SERVER_START_KEY in before and SERVER_START_KEY in after
    restarted = any(key in before and key in after and before[key] != after[key]
                    for key in (SERVER_START_KEY, CONTAINER_START_KEY))
    stats_reset = restarted or (
        pg_readable and any(before.get(key) != after.get(key) for key in STATS_EPOCH_KEYS))

    pg: dict = {}
    cgroup: dict = {}

    for key, value in after.items():
        in_cgroup = key.startswith(CGROUP_PREFIX)
        prior = before.get(key, 0 if key.startswith(IO_PREFIX) else None)

        if (key == "at" or not _is_number(value) or not _is_number(prior)
                or not (in_cgroup or pg_readable)):
            continue

        if value < prior:                    # counters only grow until they restart
            if in_cgroup:
                restarted = True
            else:
                stats_reset = True
            continue

        diff = value - prior
        (cgroup if in_cgroup else pg)[key] = round(diff, 1) if isinstance(diff,
                                                                          float) else diff

    if not pg_readable:
        delta = {"unreadable": True}
    elif stats_reset:
        delta = {"reset": True}
    else:
        delta = pg

    if not restarted:
        delta.update(cgroup)

    if _is_number(before.get("at")) and _is_number(after.get("at")):
        delta["seconds"] = round(after["at"] - before["at"], 1)
    return delta


def max_deltas(deltas: Iterable[dict]) -> dict:
    """
    Each counter's largest delta across several windows (a level's repeats), so the
    counters of one result may come from different windows. When Postgres's counters
    are unknown for any window (a statistics reset, or Postgres unreadable), only the
    cgroup counters and ``seconds`` survive, with that window's ``UNKNOWN_FLAGS``.
    """
    deltas = list(deltas)
    merged: dict = {}

    for delta in deltas:
        for key, value in delta.items():
            if key not in UNKNOWN_FLAGS:
                merged[key] = max(merged.get(key, value), value)

    flags = {flag: True for flag in UNKNOWN_FLAGS
             if any(delta.get(flag) for delta in deltas)}

    if flags:
        merged = {**flags, **{key: value for key, value in merged.items()
                              if key.startswith(CGROUP_PREFIX) or key == "seconds"}}
    return merged


def counter_windows(start: dict, workload_end: dict,
                    cleanup_end: Optional[dict]) -> dict:
    """
    A scenario's counter deltas, with the raw ``snapshots``. ``workload`` runs from
    scenario start to the end of the workload, the window of the resource samples.
    ``cleanup`` covers feed deletion and quiescence, and is ``None`` when the scenario
    keeps its feeds.
    """
    return {
        "workload": counter_delta(start, workload_end),
        "cleanup": None if cleanup_end is None else counter_delta(workload_end,
                                                                  cleanup_end),
        "snapshots": {"start": start, "workload_end": workload_end,
                      "cleanup_end": cleanup_end},
    }


class PgStatStatements:
    """
    Reset/snapshot interface over the db container; no-op when unavailable.
    """

    def __init__(self, docker: DockerClient, service: str = "db"):
        self._docker = docker
        self._service = service
        self.unavailable_reason: Optional[str] = None

        self._env = docker.service_env(service)
        
        code, out = self._psql("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        
        if code != 0:
            # most commonly: a db that does not preload pg_stat_statements
            self.unavailable_reason = out.strip()[:300] or f"psql exit {code}"

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None

    def reset(self) -> None:
        if self.available:
            self._psql("SELECT pg_stat_statements_reset()")

    def snapshot(self, limit: Optional[int] = None) -> list[dict]:
        """
        Statements since the last reset, most total execution time first: all of them
        unless ``limit`` is given. Two kinds of statement can still be missing: entries
        evicted by ``pg_stat_statements.max`` (5,000 by default), and statements that
        failed with an error, which ``pg_stat_statements`` never records.
        """
        if not self.available:
            return []

        sql = STATEMENTS_SQL if limit is None else f"{STATEMENTS_SQL} LIMIT {int(limit)}"
        code, out = self._psql(sql)

        if code != 0:
            return []
        return parse_psql_rows(out, STATEMENT_COLUMNS)

    def _psql(self, sql: str) -> tuple[int, str]:
        return psql(self._docker, sql, service=self._service, env=self._env)
