"""
Locust control-plane load test for CUBE — the Layer-1 (control plane / RED) from
STRATEGY §4 built on the ``chris_api`` Collection+JSON seam.

The data-plane harness (``run_bench``) measures orchestration (makespan, DAG execution).
This complements it by driving *concurrent API clients* against the control-plane
endpoints (list feeds/instances/files, plugin search, create instance) and reporting RED
metrics — Rate, Errors, Duration percentiles — via Locust's own statistics. It is the
right tool to find the API saturation knee, error onset and DB connection-pool
exhaustion under client concurrency, which the data plane does not exercise.

Run headless via ``just bench-locust``, e.g.::

    just bench-locust '-u 100 -r 20 -t 3m'      # 100 users, ramp 20/s, 3 minutes

``host`` is taken from ``CUBE_URL`` (set on the benchmark service), so no ``--host`` is
needed. Pure Collection+JSON helpers are reused from ``chris_api`` (the shared seam).

Each step also records whether the API was healthy when it started and whether it
recovered once the load stopped (``recovery.api_recovery``) in
``<csv prefix>_recovery.json`` and exits non-zero otherwise: a step that ran on or
left behind a broken stack does not measure CUBE under load. That file also records the
envelope knobs and the Postgres server's effective settings.
"""

from __future__ import annotations

import json
import logging
import os

from locust import HttpUser, between, events, task
from locust.runners import WorkerRunner

from benchmarks.chris_api import CJ, _items, _template
from benchmarks.docker_client import DockerClient
from benchmarks.environment import envelope, postgres_settings
from benchmarks.recovery import all_ok, api_recovery, probe_api


USERNAME = os.environ.get("CUBE_USERNAME", "chris")
PASSWORD = os.environ.get("CUBE_PASSWORD", "chris1234")

FS_PLUGIN = os.environ.get("BENCH_LOCUST_FS_PLUGIN", "dbg-bigfiles")

#: small fs payload (~10 files of 1 KiB) — keeps the write path cheap; jobs still spawn
FS_PARAMS = {"total": "10240B", "size": "1024B"}

#: the create task spawns real DAGs, so it is opt-in (BENCH_LOCUST_WRITE=1). Default is a
#: clean read-only control-plane saturation sweep that doesn't flood the compute side.
WRITE_ENABLED = os.environ.get("BENCH_LOCUST_WRITE", "").lower() in ("1", "true", "yes")

#: seconds to keep probing the API for recovery after the step (0 disables both checks)
RECOVERY_S = float(os.environ.get("BENCH_LOCUST_RECOVERY_S") or 60)

#: exit code of a step that started on, or left behind, an API that does not answer
UNHEALTHY_EXIT_CODE = 3

_step: dict = {}


class CubeUser(HttpUser):
    """
    One simulated API client: authenticates once via a DRF token, then exercises the
    control-plane endpoints. Reads dominate (the cleanest saturation signal); a low-weight
    create exercises the write path too.
    """

    host = os.environ.get("CUBE_URL", "http://chris:8000/api/v1/")
    wait_time = between(0.1, 0.5)

    def on_start(self) -> None:
        with self.client.post("auth-token/",
                              json={"username": USERNAME, "password": PASSWORD},
                              name="auth-token", catch_response=True) as r:
            if r.status_code // 100 != 2:
                r.failure(f"auth HTTP {r.status_code}")
                self.fs_id = None
                return
            
            token = r.json().get("token")
            if not token:
                r.failure("no token in auth response")
                self.fs_id = None
                return

            r.success()

        self.client.headers.update({"Authorization": f"Token {token}", "Accept": CJ})
        self.fs_id = self._resolve_fs_id()

    def _resolve_fs_id(self):
        with self.client.get("plugins/search/", params={"name_exact": FS_PLUGIN},
                             name="search-plugins", catch_response=True) as r:
            self._check(r)

            try:
                items = _items(r.json())
                return int(items[0]["id"]) if items else None
            except Exception:                      # noqa: BLE001 - best effort
                return None

    # -- read tasks (control-plane RED) ----------------------------------------------

    @task(6)
    def list_feeds(self) -> None:
        self._get("", {"limit": 10}, "list-feeds")

    @task(6)
    def list_instances(self) -> None:
        self._get("plugins/instances/", {"limit": 10}, "list-instances")

    @task(4)
    def list_files(self) -> None:
        self._get("userfiles/", {"limit": 10}, "list-files")

    @task(2)
    def search_plugins(self) -> None:
        self._get("plugins/search/", {"name_exact": FS_PLUGIN}, "search-plugins")

    # -- write task (exercises the create path; spawns a small job) -------------------

    @task(1)
    def create_instance(self) -> None:
        if not WRITE_ENABLED or not getattr(self, "fs_id", None):
            return
        
        body = json.dumps(_template(FS_PARAMS))

        with self.client.post(f"plugins/{self.fs_id}/instances/", data=body,
                              headers={"Content-Type": CJ}, name="create-instance",
                              catch_response=True) as r:
            self._check(r)

    # -- helpers ----------------------------------------------------------------------

    def _get(self, path: str, params: dict, name: str) -> None:
        with self.client.get(path, params=params, name=name, catch_response=True) as r:
            self._check(r)

    @staticmethod
    def _check(r) -> None:
        if r.status_code // 100 == 2:
            r.success()
        else:
            r.failure(f"HTTP {r.status_code}")


# -- step health: was the API healthy before the step, and did it recover after it? ------

def _checks_step_health(environment) -> bool:
    """
    Only one process checks: the local or master runner, not each ``--processes`` worker.
    """
    return RECOVERY_S > 0 and not isinstance(environment.runner, WorkerRunner)


def _postgres_settings(docker) -> dict:
    """
    The Postgres server's effective settings; ``{}`` when they cannot be read, which
    must not decide whether the step recovered.
    """
    try:
        return postgres_settings(docker) if docker is not None else {}
    except Exception:                              # noqa: BLE001 - instrumentation only
        logging.exception("could not read the Postgres settings")
        return {}


def _api_url(environment) -> str:
    # --host replaces CubeUser.host when the runner starts, after test_start has fired
    return environment.host or CubeUser.host


@events.test_start.add_listener
def _probe_before_step(environment, **_kwargs) -> None:
    if _checks_step_health(environment):
        _step["healthy_at_start"] = all_ok(probe_api(_api_url(environment)))


@events.quitting.add_listener
def _record_recovery(environment, **_kwargs) -> None:
    if not _checks_step_health(environment):
        return

    # quitting fires before Locust stops the users itself: after Ctrl-C or SIGTERM they
    # would still be loading the API while it is probed for recovery
    if environment.runner is not None:
        environment.runner.stop()

    result = {"healthy_at_start": _step.get("healthy_at_start")}
    docker = None

    try:
        docker = DockerClient(project=os.environ.get("COMPOSE_PROJECT_NAME") or None,
                              exclude_services=("benchmark",))
        result.update(api_recovery(
            _api_url(environment), docker, window_s=RECOVERY_S,
            stats_interval_s=float(os.environ.get("CUBE_DB_POOL_STATS_INTERVAL") or 0),
            workers=int(os.environ.get("CUBE_UVICORN_WORKERS") or 0) or None))
    except Exception as exc:                       # noqa: BLE001 - must still be recorded
        logging.exception("could not check whether the API recovered")
        result.update({"recovered": False, "seconds": None, "error": repr(exc)})

    result["envelope"] = envelope(os.environ)
    result["postgres"] = _postgres_settings(docker)
    prefix = getattr(environment.parsed_options, "csv_prefix", None)

    if prefix:
        with open(f"{prefix}_recovery.json", "w") as fh:
            json.dump(result, fh, indent=2)

    logging.info("API healthy before the step: %s; recovered after it: %s (%s s)",
                 result["healthy_at_start"], result["recovered"], result["seconds"])

    if not (result["healthy_at_start"] and result["recovered"]):
        environment.process_exit_code = UNHEALTHY_EXIT_CODE
