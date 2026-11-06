"""
Tests for benchmarks.metrics — RED/USE/makespan rollups and percentiles.
"""

from benchmarks.metrics import (counter_increase, instance_makespan, max_concurrent_active,
                                percentile, phase_durations, red_rollup, resource_rollup,
                                timelines_rollup)
from benchmarks.models import (PluginRole, RequestRecord, ResourceSample, StatusTimeline)


# -- percentile ------------------------------------------------------------------------

def test_percentile_basic():
    assert percentile([], 95) == 0.0
    assert percentile([5], 95) == 5
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([1, 2, 3, 4], 100) == 4


# -- RED rollup ------------------------------------------------------------------------

def _req(cls, ms, ok=True, code=200, t=0.0):
    return RequestRecord(endpoint_class=cls, method="POST", status_code=code,
                         latency_ms=ms, ok=ok, t=t)


def test_red_rollup_excludes_poll_and_counts_errors():
    records = [
        _req("create", 100, t=0.0),
        _req("create", 300, t=1.0),
        _req("create", 0, ok=False, code=500, t=2.0),
        _req("poll", 9999, t=2.0),          # excluded from workload view
    ]
    red = red_rollup(records)
    assert "poll" not in red["by_class"]
    assert red["by_class"]["create"]["count"] == 3
    assert red["status_5xx"] == 1
    assert red["errors"] == 1
    assert red["total_requests"] == 3


def test_red_rollup_counts_timeouts_as_zero_status():
    red = red_rollup([_req("list", 30000, ok=False, code=0, t=0.0)])
    assert red["timeouts"] == 1


# -- timelines & makespan --------------------------------------------------------------

def _timeline(inst, **first_seen):
    tl = StatusTimeline(instance_id=inst, node_key=f"n{inst}", role=PluginRole.DS)
    for status, t in first_seen.items():
        tl.observe(status, t)
    return tl


def test_phase_durations_and_makespan():
    tl = _timeline(1, created=0, waiting=1, scheduled=2, started=3,
                   registeringFiles=5, finishedSuccessfully=8)
    durations = phase_durations(tl)
    assert durations["created"] == 1
    assert durations["started"] == 2          # started(3) -> registeringFiles(5)
    assert durations["registeringFiles"] == 3  # -> terminal(8)
    assert instance_makespan(tl) == 8


def test_observe_records_first_seen_only():
    tl = _timeline(1)
    tl.observe("started", 3.0)
    tl.observe("started", 9.0)                  # later sighting ignored for first_seen
    assert tl.first_seen["started"] == 3.0
    assert tl.final_status == "started"


def test_timelines_rollup_counts_and_concurrency():
    tls = [
        _timeline(1, created=0, started=1, finishedSuccessfully=4),
        _timeline(2, created=0, started=1, finishedWithError=3),
    ]
    roll = timelines_rollup(tls)
    assert roll["instances"] == 2
    assert roll["final_status_counts"]["finishedSuccessfully"] == 1
    assert roll["final_status_counts"]["finishedWithError"] == 1
    assert roll["completed"] == 2


def test_max_concurrent_active():
    # both active over [0,4] and [0,3] -> peak concurrency 2
    tls = [
        _timeline(1, created=0.0, finishedSuccessfully=4.0),
        _timeline(2, created=0.0, finishedSuccessfully=3.0),
    ]
    assert max_concurrent_active(tls) == 2


# -- resources -------------------------------------------------------------------------

def test_resource_rollup_peaks():
    samples = [
        ResourceSample(t=0, container="db", kind="service", cpu_pct=10, mem_bytes=100,
                       mem_limit_bytes=1000),
        ResourceSample(t=1, container="db", kind="service", cpu_pct=80, mem_bytes=500,
                       mem_limit_bytes=1000),
    ]
    roll = resource_rollup(samples)
    assert roll["db"]["cpu_pct_peak"] == 80
    assert roll["db"]["mem_bytes_peak"] == 500
    assert roll["db"]["samples"] == 2


def test_resource_rollup_blkio_delta_and_pids_peak():
    # blkio counters are cumulative since container start -> rollup reports the delta
    samples = [
        ResourceSample(t=0, container="db", kind="service", cpu_pct=1, mem_bytes=1,
                       mem_limit_bytes=10, blkio_read_bytes=1000,
                       blkio_write_bytes=5000, pids=3),
        ResourceSample(t=1, container="db", kind="service", cpu_pct=1, mem_bytes=1,
                       mem_limit_bytes=10, blkio_read_bytes=1500,
                       blkio_write_bytes=9000, pids=7),
    ]
    roll = resource_rollup(samples)
    assert roll["db"]["blkio_read_bytes"] == 500
    assert roll["db"]["blkio_write_bytes"] == 4000
    assert roll["db"]["pids_peak"] == 7


def _db(t, cpu=None, *, written=0, started_at=1.0, anon=None, throttled=None):
    return ResourceSample(t=t, container="db", kind="service", cpu_pct=cpu, mem_bytes=t,
                          mem_limit_bytes=10, blkio_write_bytes=written,
                          started_at=started_at, mem_anon_bytes=anon,
                          cpu_throttled_s=throttled)


def test_resource_rollup_skips_readings_without_a_cpu_window():
    roll = resource_rollup([_db(0), _db(1, 40.0), _db(2, 60.0)])["db"]

    assert (roll["cpu_pct_peak"], roll["cpu_pct_mean"]) == (  # nosec B101 - pytest assertion
        60.0, 50.0)
    assert (roll["cpu_windows"], roll["samples"]) == (2, 3)  # nosec B101 - pytest assertion

    # a container read once has no CPU figure at all, not a 0
    once = resource_rollup([_db(0)])["db"]
    figures = (once["cpu_pct_peak"], once["cpu_pct_mean"], once["cpu_windows"])
    assert figures == (None, None, 0)  # nosec B101 - pytest assertion


def test_a_restart_starts_the_counters_again_without_counting_the_reset():
    # 100 -> 400 bytes written, a restart (a new start time, counters back to 0), 0 -> 50
    samples = [_db(0, written=100), _db(1, written=400),
               _db(2, written=0, started_at=9.0), _db(3, written=50, started_at=9.0)]

    assert counter_increase(samples, "blkio_write_bytes") == 350  # nosec B101 - pytest assertion
    # the rollup puts the readings in time order first
    roll = resource_rollup(list(reversed(samples)))["db"]
    assert roll["blkio_write_bytes"] == 350  # nosec B101 - pytest assertion


def test_a_counter_counts_only_what_it_grew_within_one_start():
    # a restart whose first reading is above the old start's last, and a fall within one
    # start, which a counter that runs from the container's start cannot make
    across = [_db(0, written=100), _db(1, written=400),
              _db(2, written=500, started_at=9.0), _db(3, written=550, started_at=9.0)]
    fall = [_db(0, written=100), _db(1, written=400), _db(2, written=300),
            _db(3, written=350)]

    assert counter_increase(across, "blkio_write_bytes") == 350  # nosec B101 - pytest assertion
    assert counter_increase(fall, "blkio_write_bytes") == 350  # nosec B101 - pytest assertion


def test_resource_rollup_peaks_anon_memory_and_adds_up_throttling():
    roll = resource_rollup([_db(0, anon=70), _db(1, 5.0, anon=90, throttled=0.25),
                            _db(2, 5.0, anon=80, throttled=0.5)])["db"]

    assert (roll["mem_bytes_peak"], roll["mem_anon_peak"]) == (  # nosec B101 - pytest assertion
        2, 90)
    assert roll["cpu_throttled_s"] == 0.75  # nosec B101 - pytest assertion
    unknown = resource_rollup([_db(0)])["db"]
    figures = (unknown["mem_anon_peak"], unknown["cpu_throttled_s"])
    assert figures == (None, None)  # nosec B101 - pytest assertion


def test_timelines_rollup_throughput_and_littles_law():
    # two instances, each in-system for 10s over a 10s span -> 0.2/s, ~2 in flight
    tls = [
        _timeline(1, created=0, finishedSuccessfully=10),
        _timeline(2, created=0, finishedSuccessfully=10),
    ]
    roll = timelines_rollup(tls)
    assert roll["throughput_per_s"] == 0.2
    assert roll["littles_law_avg_inflight"] == 2.0
    assert roll["max_concurrent_active"] == 2
