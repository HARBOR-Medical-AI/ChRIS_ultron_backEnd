"""Low-level wrapper over the Docker SDK (against the mounted socket).

One small adapter reused by the stats sampler, the recovery routine, and environment
collection — the same access pattern the ``justfile`` ``reap-plugin-instances`` recipe
uses (``docker.from_env()`` + label filters). Engine-agnostic: a podman socket exposing
the Docker API works too.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from datetime import datetime

import docker
from docker.errors import APIError, ImageNotFound, NotFound

from .models import ResourceSample


#: pfcon labels every plugin-job container with this (see docker-compose.yml JOB_LABELS)
JOB_LABEL = "org.chrisproject.miniChRIS=plugininstance"

#: run inside a container, it reads the container's own cgroup v2 files (each container
#: gets a private cgroup namespace): ``io.stat`` is what ``docker stats`` reports as block
#: I/O, and the swap and OOM-kill counters are ones the stats API leaves out
CGROUP_COUNTERS_CMD = [
    "sh", "-c",
    "cat /sys/fs/cgroup/io.stat; "
    "grep -E '^(pswpin|pswpout) ' /sys/fs/cgroup/memory.stat; "
    "grep '^oom_kill ' /sys/fs/cgroup/memory.events",
]

_CGROUP_KEYS = ("pswpin", "pswpout", "oom_kill")

#: the cgroup counters' identity: when it differs between two readings the container
#: restarted in between and its counters started again from zero
CONTAINER_START_KEY = "cgroup_container_started_at"


@dataclass(frozen=True)
class ContainerRef:
    """
    What a resource sample needs to know about a container: its id, the name its samples
    go under (the compose service, or the container's own name for a plugin job), its
    kind, and when it started (epoch seconds; ``None`` when unknown).
    """
    id: str
    name: str
    kind: str  # "service" | "job"
    started_at: float | None


@dataclass(frozen=True)
class StatsRead:
    """
    The outcome of one stats read: the frame, or a container that vanished between the
    listing and the read (removed, or exited: its frame has the zero read time), or the
    error of a read that failed.
    """
    frame: dict | None = None
    vanished: bool = False
    error: str | None = None


class DockerClient:
    """
    Docker access for sampling/recovery/environment. Degrades to a no-op if the
    socket is unavailable (e.g. a non-root container on Docker Desktop) so the rest of
    the harness still runs — resource sampling and recovery are best-effort by design.
    ``timeout`` bounds each call (docker-py's default is 60 s).
    """

    def __init__(self, project: str | None = None, *, job_label: str = JOB_LABEL,
                 exclude_services: tuple[str, ...] = (), timeout: float | None = None):
        self.project = project
        self.job_label = job_label
        self.exclude_services = exclude_services
        self._docker = None
        self.unavailable_reason: str | None = None

        try:
            client = docker.from_env(**({"timeout": timeout} if timeout else {}))
            client.ping()
            self._docker = client
        except Exception as exc:  # noqa: BLE001 - no engine, or one that does not answer
            self.unavailable_reason = str(exc)

    @property
    def available(self) -> bool:
        return self._docker is not None

    # -- container discovery ---------------------------------------------------------

    def _service_filter(self) -> dict:
        """
        The stack's containers: the compose project's, or any compose service's without one.
        """
        if self.project:
            return {"label": f"com.docker.compose.project={self.project}"}
        return {"label": "com.docker.compose.service"}

    def service_containers(self) -> list:
        if not self._docker:
            return []

        # a container removed while the listing inspects it is left out, not an error
        listed = self._docker.containers.list(filters=self._service_filter(),
                                              ignore_removed=True)
        return [c for c in listed
                if c.labels.get("com.docker.compose.service") not in self.exclude_services]

    def job_containers(self, all_states: bool = False) -> list:
        if not self._docker:
            return []
        return self._docker.containers.list(all=all_states, ignore_removed=True,
                                            filters={"label": self.job_label})

    def find_service(self, service: str) -> object | None:
        for c in self.service_containers():
            if c.labels.get("com.docker.compose.service") == service:
                return c
        return None

    # -- sampling --------------------------------------------------------------------

    def read_stats(self, container_id: str) -> StatsRead:
        """
        One one-shot stats read (a few milliseconds, where a plain read waits 1-2 s for
        Docker's collector); never raises.
        """
        try:
            frame = self._docker.api.stats(container_id, stream=False, one_shot=True)
        except NotFound:
            return StatsRead(vanished=True)
        except APIError as exc:
            # Podman answers 409 for a container that is no longer running, where Docker
            # gives a frame with the zero read time
            if exc.status_code == 409:
                return StatsRead(vanished=True)
            return StatsRead(error=str(exc))
        except Exception as exc:                       # noqa: BLE001 - counted, not raised
            return StatsRead(error=str(exc))

        if _rfc3339(frame.get("read")) is None:        # it exited after the listing
            return StatsRead(vanished=True)
        return StatsRead(frame=frame)

    def sampled_containers(self) -> list[ContainerRef]:
        """
        The running service and plugin-job containers to sample, each once though one that
        carries both kinds of label is in both listings. The listings inspect nothing (the
        listing above inspects each container in turn, which waits on any that is being
        started or removed), so ``started_at`` is unknown: ``started_at()`` reads it.
        """
        if not self._docker:
            return []

        refs = {}
        for listed in (self._docker.api.containers(filters=self._service_filter()),
                       self._docker.api.containers(filters={"label": self.job_label})):
            for c in listed:
                labels = c.get("Labels") or {}
                service = labels.get("com.docker.compose.service")
                # a job is named for itself, even from an image that carries compose labels
                job = self.job_label.split("=")[0] in labels

                if not job and service in self.exclude_services:
                    continue

                name = (c.get("Names") or ["/" + c["Id"][:12]])[0].lstrip("/")
                refs[c["Id"]] = ContainerRef(id=c["Id"], name=service if service and not job
                                             else name, kind="job" if job else "service",
                                             started_at=None)
        return list(refs.values())

    def started_at(self, container_id: str) -> float | None:
        """
        When a container started (epoch seconds), from an inspect; ``None`` when the
        inspect fails or it has not started.
        """
        try:
            state = self._docker.api.inspect_container(container_id).get("State") or {}
        except Exception:                              # noqa: BLE001 - best effort
            return None
        return _rfc3339(state.get("StartedAt"))

    # -- ops -------------------------------------------------------------------------

    def logs(self, container, tail: int | str = 200, since: float | None = None) -> str:
        """
        The container's last ``tail`` log lines (``"all"`` for every one), only those
        written after the epoch time ``since`` when given.
        """
        try:
            return container.logs(tail=tail, since=since).decode("utf-8", "replace")
        except Exception as exc:                       # noqa: BLE001 - best effort
            return f"<could not read logs: {exc}>"

    def reap_jobs(self) -> int:
        removed = 0
        for c in self.job_containers(all_states=True):
            with contextlib.suppress(Exception):       # best effort
                c.remove(force=True)
                removed += 1
        return removed

    def exec_in_service(self, service: str, cmd: list) -> tuple[int, str]:
        """
        Run a command inside a service container; (-1, reason) when unavailable.
        """
        container = self.find_service(service)

        if container is None:
            return -1, f"service {service!r} not running"
        return _exec(container, cmd)

    def cgroup_counters(self, service: str) -> dict:
        """
        Counters of a service container's cgroup since it started:
        ``cgroup_write_bytes`` (bytes written to disk), ``cgroup_pswpout`` and
        ``cgroup_pswpin`` (pages swapped out and in) and ``cgroup_oom_kill``, with the
        container's start time under ``CONTAINER_START_KEY``. Only the counters that
        could be read are present, so ``{}`` when none could.
        """
        container = self.find_service(service)

        if container is None:
            return {}

        code, out = _exec(container, CGROUP_COUNTERS_CMD)
        counters = {} if code == -1 else parse_cgroup_counters(out)

        if counters:
            counters[CONTAINER_START_KEY] = (container.attrs.get("State") or {}).get(
                "StartedAt")
        return counters

    def service_limits(self) -> dict:
        """
        The resource limits the engine applies to each running service container:
        ``cpus``, ``mem_limit`` and ``memswap_limit`` (bytes; -1 = unlimited swap) and
        ``shm_size`` (bytes), each ``None`` when unset.
        """
        limits = {}

        for container in self.service_containers():
            host = container.attrs.get("HostConfig", {}) or {}
            service = container.labels.get("com.docker.compose.service", container.name)

            limits[service] = {"cpus": _cpus(host),
                               "mem_limit": host.get("Memory") or None,
                               "memswap_limit": host.get("MemorySwap") or None,
                               "shm_size": host.get("ShmSize") or None}
        return limits

    def service_env(self, service: str) -> dict:
        """
        Environment variables of a service container (e.g. POSTGRES_USER).
        """
        container = self.find_service(service)
        if container is None:
            return {}

        env = container.attrs.get("Config", {}).get("Env", []) or []
        return dict(item.split("=", 1) for item in env if "=" in item)

    def restart_service(self, service: str) -> bool:
        c = self.find_service(service)
        if c is None:
            return False
        try:
            c.restart()
            return True
        except Exception:                              # noqa: BLE001
            return False

    def info(self) -> dict:
        try:
            return self._docker.info()
        except Exception:                              # noqa: BLE001
            return {}

    def version(self) -> dict:
        try:
            return self._docker.version()
        except Exception:                              # noqa: BLE001
            return {}

    def image_info(self, ref: str | None) -> dict | None:
        """
        The local image an image reference resolves to (``image_record``); ``None`` when
        it is not present or Docker is unavailable, ``{"error": ...}`` when Docker
        could not tell.
        """
        if not self._docker or not ref:
            return None

        try:
            return image_record(self._docker.images.get(ref))
        except ImageNotFound:
            return None
        except Exception as exc:                       # noqa: BLE001 - reported, not raised
            return {"error": str(exc)}


def image_record(image) -> dict:
    """
    What identifies a local image: its id, the digests it is known by (sorted) and its
    architecture. The id of one image differs between image stores (overlay2,
    containerd); the digest of a pulled image does not. On overlay2 only a pulled image
    has digests; on containerd a local build has one of its own.
    """
    attrs = getattr(image, "attrs", None) or {}
    return {"image_id": image.id, "repo_digests": sorted(attrs.get("RepoDigests") or []),
            "architecture": attrs.get("Architecture")}


#: the shortest window between two readings that gives a CPU figure: the closing tick of a
#: scenario comes anywhere after a regular one, and its shorter window would read a burst of
#: a service on the 2 s beat as a larger figure than the peaks' 1 s windows do
MIN_CPU_WINDOW_S = 0.75
#: the shortest window since a container's start that gives one: a plugin job lives a
#: second or two, and is read once or twice
MIN_START_WINDOW_S = 0.25


def resource_sample(ref: ContainerRef, frame: dict, previous: dict | None, *,
                    since: float | None = None, last: dict | None = None) -> ResourceSample:
    """
    The sample of a one-shot stats ``frame``. A one-shot frame has no usable precpu, so
    its CPU is over the time since ``previous``, an earlier frame of the container from
    the same sampler (the last one whose window gave a figure, so that no CPU goes
    unreported). Without one, a container that started after ``since`` (when sampling
    began, epoch seconds) gets its CPU since its start, all that a plugin job living a
    second or two gets, and any other container none; nor does a window shorter than
    ``MIN_CPU_WINDOW_S`` (``MIN_START_WINDOW_S`` since a start). The throttling is over
    the time since ``last``, the container's frame just before this one (``previous``
    when not given), so that two readings never count the same throttling.
    """
    cpu, window, _ = _cpu_since(frame, previous, ref.started_at, since)
    mem = frame.get("memory_stats") or {}
    stats = mem.get("stats") or {}
    anon = stats.get("anon", stats.get("rss"))      # cgroup v2, v1
    read, written = _blkio(frame)

    return ResourceSample(
        t=_rfc3339(frame.get("read")), container=ref.name, kind=ref.kind, cpu_pct=cpu,
        mem_bytes=float(mem.get("usage", 0) or 0),
        mem_limit_bytes=float(mem.get("limit", 0) or 0),
        blkio_read_bytes=read, blkio_write_bytes=written,
        pids=int((frame.get("pids_stats") or {}).get("current", 0) or 0),
        cpu_window_s=window, cpu_total_ns=_cpu_total(frame),
        cpu_throttled_s=_throttled_s(frame, last if last is not None else previous),
        mem_anon_bytes=float(anon) if anon is not None else None,
        started_at=ref.started_at)


def short_window(frame: dict, previous: dict | None, started_at: float | None,
                 since: float | None) -> bool:
    """
    Whether ``frame`` gives no CPU figure only because its window is too short: the
    reading after it should then be over the same window's start, ``previous``.
    """
    return _cpu_since(frame, previous, started_at, since)[2]


def counters_went_back(frame: dict, previous: dict) -> bool:
    """
    Whether a container's CPU counter fell between two of its frames: it restarted in
    between, which keeps its id, and its counters started again from zero.
    """
    total, total0 = _cpu_total(frame), _cpu_total(previous)
    return total is not None and total0 is not None and total < total0


def _cpu_since(frame: dict, previous: dict | None, started_at: float | None,
               since: float | None) -> tuple[float | None, float | None, bool]:
    """
    CPU % and its window in seconds, as ``resource_sample`` says, or ``(None, None)``;
    and whether that window was too short for a figure.
    """
    t, total = _rfc3339(frame.get("read")), _cpu_total(frame)

    if t is None or total is None:
        return None, None, False

    if previous is not None:
        t0, total0 = _rfc3339(previous.get("read")), _cpu_total(previous)

        # counters that went backwards: a restart, which the sampler meets by inspecting
        # the start time again
        if t0 is None or total0 is None or t <= t0 or total < total0:
            return None, None, False
        began, used, shortest = t0, total - total0, MIN_CPU_WINDOW_S
    elif started_at is not None and since is not None and since <= started_at < t:
        began, used, shortest = started_at, total, MIN_START_WINDOW_S
    else:
        return None, None, False

    window = t - began
    if window < shortest:
        return None, None, True

    # over the daemon's nanosecond clock: Docker's own formula divides by the host's CPU
    # time, which /proc/stat counts in 10 ms ticks, too coarse for a short window
    return round(used / (window * 1e9) * 100.0, 2), round(window, 3), False


def _cpu_total(frame: dict) -> int | None:
    total = ((frame.get("cpu_stats") or {}).get("cpu_usage") or {}).get("total_usage")
    return int(total) if total is not None else None


def _throttled_s(frame: dict, previous: dict | None) -> float | None:
    """
    Seconds the container's CPU limit held it back since ``previous``.
    """
    def throttled(f):
        return ((f.get("cpu_stats") or {}).get("throttling_data") or {}).get(
            "throttled_time")

    if previous is None:
        return None

    now, before = throttled(frame), throttled(previous)
    if now is None or before is None or now < before:
        return None
    return round((now - before) / 1e9, 3)


_RFC3339 = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$")


def _rfc3339(value: str | None) -> float | None:
    """
    One of Docker's timestamps (RFC 3339 with nanoseconds) as epoch seconds; ``None``
    for the zero time, which Docker gives a container that is not running, or for
    anything else.
    """
    match = _RFC3339.match(value or "")

    if not match or value.startswith("0001-01-01"):
        return None

    whole, fraction, zone = match.groups()
    seconds = datetime.fromisoformat(whole + ("+00:00" if zone == "Z" else zone))
    return seconds.timestamp() + float(f"0.{fraction or 0}")


def _exec(container, cmd: list) -> tuple[int, str]:
    try:
        code, output = container.exec_run(cmd)
        return code, output.decode("utf-8", "replace")
    except Exception as exc:                           # noqa: BLE001 - best effort
        return -1, str(exc)


def _cpus(host: dict) -> float | None:
    """
    A container's CPU limit: Docker sets ``NanoCpus``, while Podman can report the
    same limit as a CFS quota over its period.
    """
    if host.get("NanoCpus"):
        return host["NanoCpus"] / 1e9

    quota, period = host.get("CpuQuota") or 0, host.get("CpuPeriod") or 0
    return quota / period if quota > 0 and period > 0 else None


def parse_cgroup_counters(text: str) -> dict:
    """
    Parse ``CGROUP_COUNTERS_CMD`` output: ``io.stat`` lines (``<major:minor> wbytes=N
    ...``, summed over devices) and ``<key> <value>`` lines from ``memory.stat`` and
    ``memory.events``; anything else is ignored.
    """
    counters: dict[str, int] = {}
    written = None

    for line in text.splitlines():
        fields = line.split()

        if fields and ":" in fields[0]:                # io.stat: one line per device
            for item in fields[1:]:
                key, _, value = item.partition("=")

                if key == "wbytes" and value.isdigit():
                    written = (written or 0) + int(value)

        elif len(fields) == 2 and fields[0] in _CGROUP_KEYS and fields[1].isdigit():
            counters[f"cgroup_{fields[0]}"] = int(fields[1])

    if written is not None:
        counters["cgroup_write_bytes"] = written
    return counters


def _blkio(stats: dict) -> tuple[float, float]:
    read = write = 0.0
    
    for entry in stats.get("blkio_stats", {}).get("io_service_bytes_recursive") or []:
        op = entry.get("op", "").lower()
        
        if op == "read":
            read += entry.get("value", 0)
        elif op == "write":
            write += entry.get("value", 0)
    return read, write
