"""
Docker one-shot stats frames for the sampler's tests, shaped as Docker 29 returns them.
"""

from datetime import datetime, timezone


def iso(t: float) -> str:
    """
    Epoch seconds as Docker writes a timestamp, to the nanosecond.
    """
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"


def frame(t: float, cpu_ns: float, *, online: int = 2, system: bool = True,
          throttled_ns: float | None = None, usage: int = 100, anon: int | None = 50,
          read: int = 0, written: int = 0, pids: int = 3) -> dict:
    """
    A one-shot frame read at ``t`` of a container that has used ``cpu_ns`` of CPU since
    it started; the host's CPU time runs at ``online`` CPUs, and a one-shot precpu has
    no host CPU time.
    """
    return {
        "read": iso(t),
        "cpu_stats": {"cpu_usage": {"total_usage": int(cpu_ns)},
                      "system_cpu_usage": int(t * 1e9 * online) if system else None,
                      "online_cpus": online,
                      "throttling_data": {"throttled_time": throttled_ns}},
        "precpu_stats": {"cpu_usage": {"total_usage": 0}},
        "memory_stats": {"usage": usage, "limit": 1000, "stats": {"anon": anon}},
        "blkio_stats": {"io_service_bytes_recursive": [{"op": "read", "value": read},
                                                       {"op": "write", "value": written}]},
        "pids_stats": {"current": pids},
    }
