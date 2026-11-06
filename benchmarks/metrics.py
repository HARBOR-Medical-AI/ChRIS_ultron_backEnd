"""
Metric aggregation (pure math) and the thread-safe ``MetricSink``.

Adapters (``chris_api``, ``stats_sampler``) emit ``RequestRecord`` / ``ResourceSample``
events into a ``MetricSink``; the pure functions here roll those — plus the poller's
``StatusTimeline``s — into RED, makespan/phase, and USE summaries. No I/O.
"""

from __future__ import annotations

import math
import threading
from collections import defaultdict
from typing import Iterable, Optional

from .models import (PHASE_ORDER, QueueSample, RequestRecord, ResourceSample,
                     StatusTimeline)


# --- statistics helpers ---------------------------------------------------------------

def percentile(values: list[float], p: float) -> float:
    """
    Linear-interpolated percentile (p in [0, 100]). 0.0 for empty input.
    """
    if not values:
        return 0.0
    
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    
    k = (len(xs) - 1) * (p / 100.0)
    lo, hi = math.floor(k), math.ceil(k)

    if lo == hi:
        return xs[int(k)]
    return xs[lo] * (hi - k) + xs[hi] * (k - lo)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


# --- collection point -----------------------------------------------------------------

class MetricSink:
    """
    Thread-safe collection point for measurement events emitted by adapters.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: list[RequestRecord] = []
        self._resources: list[ResourceSample] = []
        self._queues: list[QueueSample] = []

    def record_request(self, r: RequestRecord) -> None:
        with self._lock:
            self._requests.append(r)

    def record_resource(self, s: ResourceSample) -> None:
        with self._lock:
            self._resources.append(s)

    def record_queue(self, s: QueueSample) -> None:
        with self._lock:
            self._queues.append(s)

    def requests(self) -> list[RequestRecord]:
        with self._lock:
            return list(self._requests)

    def resources(self) -> list[ResourceSample]:
        with self._lock:
            return list(self._resources)

    def queues(self) -> list[QueueSample]:
        with self._lock:
            return list(self._queues)


# --- RED (control plane) --------------------------------------------------------------

def red_rollup(records: Iterable[RequestRecord],
               exclude_classes: Iterable[str] = ("poll",)) -> dict:
    """
    Rate/Errors/Duration per endpoint class, plus an overall summary.

    ``poll`` requests (the harness's own observation traffic) are excluded from the
    workload view by default so they don't pollute latency percentiles.
    """
    records = [r for r in records if r.endpoint_class not in set(exclude_classes)]
    by_class: dict[str, list[RequestRecord]] = defaultdict(list)

    for r in records:
        by_class[r.endpoint_class].append(r)

    classes = {}

    for cls, rs in by_class.items():
        lat = [r.latency_ms for r in rs]
        classes[cls] = {
            "count": len(rs),
            "errors": sum(1 for r in rs if not r.ok),
            "status_5xx": sum(1 for r in rs if r.status_code >= 500),
            "timeouts": sum(1 for r in rs if r.status_code == 0),
            "p50_ms": round(percentile(lat, 50), 1),
            "p95_ms": round(percentile(lat, 95), 1),
            "p99_ms": round(percentile(lat, 99), 1),
            "max_ms": round(max(lat), 1) if lat else 0.0,
        }

    span = _timespan(records)
    total = len(records)

    return {
        "by_class": classes,
        "total_requests": total,
        "errors": sum(1 for r in records if not r.ok),
        "status_5xx": sum(1 for r in records if r.status_code >= 500),
        "timeouts": sum(1 for r in records if r.status_code == 0),
        "requests_per_s": round(total / span, 2) if span > 0 else 0.0,
    }


def _timespan(records: list[RequestRecord]) -> float:
    ts = [r.t for r in records if r.t]
    return (max(ts) - min(ts)) if len(ts) >= 2 else 0.0


# --- makespan & phase decomposition (orchestration) -----------------------------------

def phase_durations(timeline: StatusTimeline) -> dict[str, float]:
    """
    Seconds spent in each active phase, from first-seen timestamps.
    """
    seen = timeline.first_seen
    ordered = [(s, seen[s]) for s in PHASE_ORDER if s in seen]

    # include the terminal status as the closing boundary for the last active phase
    terminal_t = terminal_ts(timeline)
    out: dict[str, float] = {}

    for i, (status, t0) in enumerate(ordered):
        t1 = ordered[i + 1][1] if i + 1 < len(ordered) else terminal_t

        if t1 is not None:
            out[status] = max(0.0, t1 - t0)
    return out


def terminal_ts(timeline: StatusTimeline) -> Optional[float]:
    finals = [timeline.first_seen[s] for s in
              ("finishedSuccessfully", "finishedWithError", "cancelled")
              if s in timeline.first_seen]
    return min(finals) if finals else None


def instance_makespan(timeline: StatusTimeline) -> Optional[float]:
    if not timeline.first_seen:
        return None
    start = min(timeline.first_seen.values())
    end = terminal_ts(timeline)
    return (end - start) if end is not None else None


def timelines_rollup(timelines: list[StatusTimeline]) -> dict:
    """
    Per-phase distribution, makespan distribution, final-status counts, and a
    max-concurrency (Little's Law) estimate across instances.
    """
    phase_samples: dict[str, list[float]] = defaultdict(list)
    makespans: list[float] = []
    final_counts: dict[str, int] = defaultdict(int)

    for tl in timelines:
        for phase, dur in phase_durations(tl).items():
            phase_samples[phase].append(dur)

        ms = instance_makespan(tl)
        if ms is not None:
            makespans.append(ms)

        if tl.final_status:
            final_counts[tl.final_status] += 1

    phases = {
        phase: {"mean_s": round(_mean(v), 2),
                "p50_s": round(percentile(v, 50), 2),
                "p95_s": round(percentile(v, 95), 2)}
        for phase, v in phase_samples.items()
    }

    completed = sum(final_counts.get(s, 0) for s in
                    ("finishedSuccessfully", "finishedWithError", "cancelled"))
    
    span = _timelines_span(timelines)
    throughput = round(completed / span, 3) if span > 0 else 0.0
    
    return {
        "instances": len(timelines),
        "phases": phases,
        "makespan_mean_s": round(_mean(makespans), 2),
        "makespan_p95_s": round(percentile(makespans, 95), 2),
        "final_status_counts": dict(final_counts),
        "max_concurrent_active": max_concurrent_active(timelines),
        "completed": completed,
        "throughput_per_s": throughput,
        # Little's Law sanity check: avg in-flight ≈ completion rate × mean time in
        # system; cross-check against max_concurrent_active
        "littles_law_avg_inflight": round(throughput * _mean(makespans), 2),
    }


def _timelines_span(timelines: list[StatusTimeline]) -> float:
    """
    Wall-clock span from the first observed status to the last terminal status.
    """
    starts = [min(tl.first_seen.values()) for tl in timelines if tl.first_seen]
    ends = [e for e in (terminal_ts(tl) for tl in timelines) if e is not None]
    
    if not starts or not ends:
        return 0.0
    return max(ends) - min(starts)


def max_concurrent_active(timelines: list[StatusTimeline]) -> int:
    """
    Peak number of simultaneously-active instances, by sweeping
    (created -> terminal) intervals.
    """
    events: list[tuple[float, int]] = []

    for tl in timelines:
        if not tl.first_seen:
            continue

        start = min(tl.first_seen.values())
        end = terminal_ts(tl)
        events.append((start, +1))
        events.append((end if end is not None else float("inf"), -1))
    
    events.sort()
    cur = peak = 0
    
    for _, delta in events:
        cur += delta
        peak = max(peak, cur)
    return peak


# --- broker queues ----------------------------------------------------------------------

def queue_rollup(samples: list[QueueSample]) -> dict:
    """
    Peak/mean depth per Celery queue — sustained growth means the workers are the
    bottleneck for that queue's task class.
    """
    by_queue: dict[str, list[int]] = defaultdict(list)
    
    for s in samples:
        by_queue[s.queue].append(s.depth)

    return {queue: {"depth_peak": max(depths),
                    "depth_mean": round(_mean(depths), 1),
                    "samples": len(depths)}
            for queue, depths in by_queue.items()}


# --- USE (resources) ------------------------------------------------------------------

def resource_rollup(samples: list[ResourceSample]) -> dict:
    """
    Peak/mean CPU% over the readings that have a CPU window (``None`` without one),
    peak memory with and without the page cache, the block I/O and CPU throttling in the
    scenario's window, and peak pids, per container.
    """
    by_container: dict[str, list[ResourceSample]] = defaultdict(list)

    for s in samples:
        by_container[s.container].append(s)

    out = {}
    for container, ss in by_container.items():
        ss = sorted(ss, key=lambda s: s.t)
        cpu = [s.cpu_pct for s in ss if s.cpu_pct is not None]
        anon = [s.mem_anon_bytes for s in ss if s.mem_anon_bytes is not None]
        throttled = [s.cpu_throttled_s for s in ss if s.cpu_throttled_s is not None]

        out[container] = {
            "kind": ss[0].kind,
            "cpu_pct_peak": round(max(cpu), 1) if cpu else None,
            "cpu_pct_mean": round(_mean(cpu), 1) if cpu else None,
            "cpu_windows": len(cpu),
            "cpu_throttled_s": round(sum(throttled), 3) if throttled else None,
            "mem_bytes_peak": int(max(s.mem_bytes for s in ss)),
            "mem_anon_peak": int(max(anon)) if anon else None,
            # blkio counters are cumulative since the container started
            "blkio_read_bytes": counter_increase(ss, "blkio_read_bytes"),
            "blkio_write_bytes": counter_increase(ss, "blkio_write_bytes"),
            "pids_peak": max((s.pids for s in ss), default=0),
            "samples": len(ss),
        }
    return out


def counter_increase(samples: list[ResourceSample], counter: str) -> int:
    """
    How much a counter that runs from the container's start grew over time-ordered
    ``samples``: the sum of its increases between consecutive readings of one start. A
    pair across a restart (the sampler inspects the start time again after one) is left
    out, so that the counter's climb from zero after it is not counted from the value
    before it; a fall is never counted.
    """
    total = 0.0

    for before, after in zip(samples, samples[1:]):
        grew = getattr(after, counter) - getattr(before, counter)

        if after.started_at == before.started_at and grew > 0:
            total += grew
    return int(total)
