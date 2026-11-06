"""
Environment / hardware manifest collection.

Auto-collects what it can (Docker engine info gives the *host* CPU/mem, image ids,
versions and each service container's resource limits), reads the effective settings
from the Postgres server, and reads the envelope knobs + git provenance from environment
variables the ``justfile`` injects. Manual hardware fields are left as explicit
placeholders for the generated report.
"""

from __future__ import annotations

import contextlib
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from .chris_api import ChrisApi
from .docker_client import DockerClient
from .metrics import percentile
from .pg_stats import server_settings


#: envelope knobs recorded verbatim from the environment (set by the bench compose/just)
ENVELOPE_KEYS = (
    "CUBE_CELERY_POLL_INTERVAL", "CUBE_UVICORN_WORKERS", "CUBE_DB_POOL_MIN_SIZE",
    "CUBE_DB_POOL_MAX_SIZE", "CUBE_DB_POOL_TIMEOUT", "CUBE_DB_MAX_CONNECTIONS",
    "CUBE_DB_SYNCHRONOUS_COMMIT", "CUBE_WORKER_MAINS_CONCURRENCY", "PFCON_WORKERS",
    "STORAGE_ENV", "CUBE_DEBUG", "CUBE_CACHE_URL",
)

#: Postgres settings recorded with every run whatever their source: the server version
#: and config file, what governs memory, WAL, checkpoints, IO and the planner, and the
#: instrumentation. Every setting a configuration file or the command line sets is
#: recorded as well, so a new line in the configuration needs no change here.
POSTGRES_SETTINGS = (
    "server_version", "config_file", "data_checksums", "max_connections",
    "shared_buffers", "effective_cache_size", "work_mem", "hash_mem_multiplier",
    "maintenance_work_mem", "max_wal_size", "min_wal_size", "checkpoint_timeout",
    "checkpoint_completion_target", "wal_compression", "wal_buffers", "wal_level",
    "full_page_writes", "synchronous_commit", "random_page_cost",
    "effective_io_concurrency", "io_method", "io_workers",
    "max_parallel_workers_per_gather", "jit", "autovacuum_max_workers",
    "shared_preload_libraries", "track_io_timing", "log_min_duration_statement",
    "log_lock_waits", "TimeZone",
)


def envelope(env: os._Environ | dict) -> dict:
    """
    The envelope knobs as the environment sets them, with any password in a URL masked: the
    manifests are archived in the repository.
    """
    return {k: _without_password(env.get(k)) for k in ENVELOPE_KEYS}


def _without_password(value):
    # RedisCache reads several servers from one LOCATION, split at ; and ,
    if not value or "://" not in value:
        return value
    return "".join(_url_without_password(part) for part in re.split(r"([;,])", value))


def _url_without_password(value):
    parts = urlsplit(value) if "://" in value else None
    if parts is None:
        return value

    netloc = parts.netloc
    if parts.password is not None:
        netloc = netloc.replace(f":{parts.password}@", ":***@", 1)

    # redis-py also takes the password, and the TLS key's, from the query string
    query = re.sub(r"(^|&)((?:ssl_)?password)=[^&]*", r"\1\2=***", parts.query)

    if (netloc, query) == (parts.netloc, parts.query):
        return value
    return urlunsplit(parts._replace(netloc=netloc, query=query))


def postgres_settings(docker: DockerClient) -> dict:
    """
    The Postgres server's effective settings (see ``pg_stats.server_settings``).
    """
    return server_settings(docker, POSTGRES_SETTINGS)


def collect_environment(docker: DockerClient, *, env: os._Environ | dict | None = None,
                        image_services: tuple[str, ...] = ("chris", "pfcon", "db",
                                                           "chris-broker",
                                                           "chris-cache")) -> dict:
    env = os.environ if env is None else env
    info = docker.info()
    version = docker.version()

    images = {}

    for service in image_services:
        container = docker.find_service(service)

        if container is not None:
            images[service] = {"image_id": container.image.id,
                               "tags": container.image.tags}
    return {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "host": {
            "operating_system": info.get("OperatingSystem"),
            "kernel_version": info.get("KernelVersion"),
            "architecture": info.get("Architecture"),
            "ncpu": info.get("NCPU"),
            "mem_total_bytes": info.get("MemTotal"),
            "docker_root_dir": info.get("DockerRootDir"),
            "server_version": info.get("ServerVersion"),
        },
        "docker": {
            "engine_version": version.get("Version"),
            "api_version": version.get("ApiVersion"),
            "platform": (version.get("Platform") or {}).get("Name"),
        },
        "images": images,
        "git": {"commit": env.get("GIT_COMMIT"), "dirty": env.get("GIT_DIRTY")},
        "storage_mode": env.get("STORAGE_ENV", "fslink"),
        "envelope": envelope(env),
        # the CPU/memory knobs as the engine applied them, not as the env vars asked
        "limits": docker.service_limits(),
        "postgres": postgres_settings(docker),
        "noise_floor": None,                       # filled by measure_noise_floor()
        "manual_fields": {
            "storage_device_type_throughput": "TODO",
            "power_thermal_mode": "TODO",
            "other_significant_workloads": "TODO",
        },
    }


def measure_noise_floor(api: ChrisApi, samples: int = 20) -> dict:
    """
    Idle API latency, to quantify the measurement noise floor.
    """
    latencies: list[float] = []

    for _ in range(samples):
        t0 = time.perf_counter()

        with contextlib.suppress(Exception):
            api.health()

        latencies.append((time.perf_counter() - t0) * 1000.0)

    return {
        "health_p50_ms": round(percentile(latencies, 50), 1),
        "health_p95_ms": round(percentile(latencies, 95), 1),
        "samples": samples,
    }
