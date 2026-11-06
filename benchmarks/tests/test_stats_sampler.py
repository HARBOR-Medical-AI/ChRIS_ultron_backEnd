"""
Tests for benchmarks.stats_sampler — the fixed-rate resource and queue sampler.
"""

import os
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from benchmarks.docker_client import ContainerRef, DockerClient, StatsRead
from benchmarks.metrics import MetricSink, resource_rollup
from benchmarks.stats_sampler import (METHOD, FixedRate, StatsSampler, next_slot,
                                      sampler_record)
from benchmarks.tests._frames import frame


# -- the fixed-rate loop ---------------------------------------------------------------------

def test_next_slot_runs_on_time_or_skips_the_slots_an_overrun_covered():
    assert next_slot(10.0, 10.3, 1.0) == (11.0, 0)  # nosec B101 - pytest assertion
    assert next_slot(10.0, 11.0, 1.0) == (11.0, 0)  # nosec B101 - pytest assertion
    # a tick due at 10 s that ended at 12.5 s skips the 11 and 12 s slots
    assert next_slot(10.0, 12.5, 1.0) == (13.0, 2)  # nosec B101 - pytest assertion


def test_fixed_rate_ticks_start_to_start():
    starts = []
    ticker = FixedRate(0.1, lambda: starts.append(time.monotonic()) or time.sleep(0.04),
                       name="test-ticker")
    ticker.start()
    time.sleep(0.75)
    ticker.stop()
    gaps = sorted(b - a for a, b in zip(starts, starts[1:]))

    # the 40 ms each tick takes does not stretch the period: the slots run or are skipped
    # (a busy host can hold a tick back) but are never pushed back
    assert 7 <= ticker.ticks + ticker.skipped <= 9  # nosec B101 - pytest assertion
    assert 0.09 <= gaps[len(gaps) // 2] <= 0.115, gaps  # nosec B101 - pytest assertion


def test_fixed_rate_counts_skipped_ticks_and_errors_and_runs_a_final_tick():
    calls = []

    def tick():
        calls.append(time.monotonic())
        if len(calls) == 1:
            time.sleep(0.13)                           # overruns the next two slots
        elif len(calls) == 2:
            raise RuntimeError("a tick that fails does not end the loop")

    ticker = FixedRate(0.05, tick, name="test-ticker")
    ticker.start()
    time.sleep(0.3)
    ticker.stop(final_tick=True)

    assert ticker.skipped >= 2 and ticker.errors == 1  # nosec B101 - pytest assertion
    assert ticker.longest >= 0.13  # nosec B101 - pytest assertion
    assert ticker.ticks == len(calls) >= 4  # nosec B101 - pytest assertion


def test_a_final_tick_given_as_a_callable_is_decided_after_the_running_tick():
    state = {"ready": False}

    def tick():
        time.sleep(0.1)
        state["ready"] = True

    ticker = FixedRate(10.0, tick, name="test-ticker")
    ticker.start()
    time.sleep(0.02)                                   # the first tick is running
    ticker.stop(final_tick=lambda: state["ready"])

    assert ticker.ticks == 2  # nosec B101 - pytest assertion


# -- the sampler's ticks -----------------------------------------------------------------------

class ScriptedDocker:
    """
    A stack whose listings, start times and reads a test scripts: ``listings`` gives each
    tick's containers, each read of a container returns the next of its ``reads``, and
    each takes ``delay`` seconds (``delays`` per container).
    """

    available = True

    def __init__(self, listings, reads, *, started=None, delay=0.0, delays=None):
        self._listings = list(listings)
        self._reads = {cid: list(r) for cid, r in reads.items()}
        self._started = started or {}
        self._delays = delays or {}
        self._delay = delay
        self._lock = threading.Lock()
        self._current = []
        self.inspected = []

    def sampled_containers(self):
        with self._lock:
            if self._listings:
                self._current = self._listings.pop(0)
            return list(self._current)

    def started_at(self, container_id):
        self.inspected.append(container_id)
        return self._started.get(container_id)

    def read_stats(self, container_id):
        time.sleep(self._delays.get(container_id, self._delay))
        with self._lock:
            reads = self._reads[container_id]
            return reads.pop(0) if len(reads) > 1 else reads[0]


def _ticking(docker, sink, since=150.0):
    """
    A sampler whose ticks the test runs itself.
    """
    sampler = StatsSampler(docker, sink)
    sampler._since = since
    sampler._pool = ThreadPoolExecutor(4)
    return sampler


def _ref(cid, name, kind="service"):
    return ContainerRef(cid, name, kind, started_at=None)


DB, BROKEN, JOB = _ref("db1", "db"), _ref("x1", "chris"), _ref("j1", "chris-jid-7", "job")
STARTED = {"db1": 100.0, "x1": 100.0, "j1": 201.0}


def test_each_tick_reads_every_container_and_counts_what_went_wrong():
    docker = ScriptedDocker(
        listings=[[DB, BROKEN], [DB, JOB], [DB, JOB]],
        reads={"db1": [StatsRead(frame(200.0, 5e9)), StatsRead(frame(201.0, 6e9)),
                       StatsRead(vanished=True)],
               "x1": [StatsRead(error="500 Server Error")],
               "j1": [StatsRead(frame(201.5, 0.75e9)), StatsRead(frame(202.5, 1.75e9))]},
        started=STARTED)
    sink = MetricSink()
    sampler = _ticking(docker, sink)

    for _ in range(3):
        sampler._sample_containers()

    by = defaultdict(list)
    for sample in sink.resources():
        by[sample.container].append(sample.cpu_pct)

    # the service's CPU from its second reading on, the job's since it started (0.75 s of
    # CPU in its first 0.5 s), then over the second between its readings
    assert by == {"db": [None, 100.0],  # nosec B101 - pytest assertion
                  "chris-jid-7": [150.0, 100.0]}
    summary = sampler.summary()
    counts = (summary.reads, summary.failed_reads, summary.vanished)
    assert counts == (6, 1, 1)  # nosec B101 - pytest assertion
    assert summary.jobs_seen == 1  # nosec B101 - pytest assertion
    # each container is inspected once, for its start time
    assert sorted(docker.inspected) == ["db1", "j1", "x1"]  # nosec B101 - pytest assertion


class Restarting(ScriptedDocker):
    """
    A stack whose ``db1`` restarts in place (same id) at 200.5: an inspect from then on
    gives the new start time.
    """

    def started_at(self, container_id):
        self.inspected.append(container_id)
        return 200.5 if len(self.inspected) > 1 else 100.0


def test_a_restart_in_place_is_inspected_again_and_not_counted_across():
    docker = Restarting(
        listings=[[DB]],
        reads={"db1": [StatsRead(frame(199.0, 4e9, written=1000)),
                       StatsRead(frame(200.0, 5e9, written=2000)),
                       StatsRead(frame(201.0, 0.4e9, written=2500)),
                       StatsRead(frame(202.0, 1.4e9, written=2600))]})
    sink = MetricSink()
    sampler = _ticking(docker, sink)

    for _ in range(4):
        sampler._sample_containers()

    samples = sink.resources()
    # no figure across the restart: its CPU since the new start is 0.4 s in 0.5 s, then
    # the second after it
    assert [s.cpu_pct for s in samples] == [None, 100.0, 80.0, 100.0]  # nosec B101 - pytest assertion
    assert [s.started_at for s in samples] == [100.0, 100.0, 200.5, 200.5]  # nosec B101 - pytest assertion
    assert docker.inspected == ["db1", "db1"]  # nosec B101 - pytest assertion
    # 1000 before the restart and 100 after it, nothing across it
    rollup = resource_rollup(samples)
    assert rollup["db"]["blkio_write_bytes"] == 1100  # nosec B101 - pytest assertion


def test_a_window_too_short_for_a_figure_leaves_its_cpu_to_the_next_reading():
    # a burst of 1.4 s of CPU between the readings at 201.3 and 202.0, a 0.7 s window
    docker = ScriptedDocker(
        listings=[[DB]], started=STARTED,
        reads={"db1": [StatsRead(frame(200.0, 5e9, throttled_ns=0)),
                       StatsRead(frame(201.3, 5e9, throttled_ns=0)),
                       StatsRead(frame(202.0, 6.4e9, throttled_ns=0.3e9)),
                       StatsRead(frame(203.0, 6.4e9, throttled_ns=0.3e9))]})
    sink = MetricSink()
    sampler = _ticking(docker, sink)

    for _ in range(4):
        sampler._sample_containers()

    samples = sink.resources()
    # the reading after the short window has the burst, over the 1.7 s since 201.3; the
    # throttling is counted once, over the window just before each reading
    assert [s.cpu_pct for s in samples] == [None, 0.0, None, 82.35]  # nosec B101 - pytest assertion
    assert [s.cpu_throttled_s for s in samples] == [None, 0.0, 0.3, 0.0]  # nosec B101 - pytest assertion


def test_a_failed_inspect_is_made_again_at_the_next_tick():
    class Flaky(ScriptedDocker):
        def started_at(self, container_id):
            self.inspected.append(container_id)
            return None if len(self.inspected) == 1 else 100.0

    docker = Flaky(listings=[[DB]], reads={"db1": [StatsRead(frame(200.0, 5e9))]})
    sampler = _ticking(docker, MetricSink())

    for _ in range(3):
        sampler._sample_containers()

    assert docker.inspected == ["db1", "db1"]  # nosec B101 - pytest assertion
    assert sampler._started == {"db1": 100.0}  # nosec B101 - pytest assertion


def test_a_container_gone_from_the_listing_is_forgotten():
    docker = ScriptedDocker(listings=[[DB, JOB], [DB]],
                            reads={"db1": [StatsRead(frame(200.0, 5e9))],
                                   "j1": [StatsRead(frame(201.5, 0.75e9))]},
                            started=STARTED)
    sampler = _ticking(docker, MetricSink())

    sampler._sample_containers()
    sampler._sample_containers()

    assert set(sampler._last) == {"db1"}  # nosec B101 - pytest assertion
    assert set(sampler._started) == {"db1"}  # nosec B101 - pytest assertion


def test_more_containers_than_workers_are_all_read_within_the_tick():
    refs = [_ref(f"c{i}", f"service-{i}") for i in range(6)]
    docker = ScriptedDocker(listings=[refs], delay=0.05,
                            reads={r.id: [StatsRead(frame(200.0, 1e9))] for r in refs})
    sink = MetricSink()

    with StatsSampler(docker, sink, interval=10.0, workers=2) as sampler:
        pass

    # three rounds of two reads in each of the two ticks: queued in the pool, not late
    summary = sampler.summary()
    assert len(sink.resources()) == 12  # nosec B101 - pytest assertion
    assert (summary.reads, summary.failed_reads, summary.busy_skips) == (12, 0, 0)  # nosec B101 - pytest assertion


def test_the_reads_of_a_tick_run_together():
    refs = [_ref(f"c{i}", f"service-{i}") for i in range(8)]
    docker = ScriptedDocker(listings=[refs], delay=0.1,
                            reads={r.id: [StatsRead(frame(200.0, 1e9))] for r in refs})
    sink = MetricSink()

    with StatsSampler(docker, sink, interval=10.0, workers=8) as sampler:
        pass

    # two ticks (the first and the closing one) of eight 0.1 s reads, each well under 0.8 s
    assert sampler.summary().max_tick_s < 0.4  # nosec B101 - pytest assertion
    assert len(sink.resources()) == 16  # nosec B101 - pytest assertion


def test_a_hung_read_holds_neither_the_ticks_nor_the_stop():
    hung = _ref("h1", "pfcon")
    docker = ScriptedDocker(listings=[[DB, hung]], delays={"h1": 3.0},
                            reads={"db1": [StatsRead(frame(200.0, 5e9))],
                                   "h1": [StatsRead(frame(200.0, 5e9))]})
    sink = MetricSink()

    with StatsSampler(docker, sink, interval=0.1) as sampler:
        time.sleep(0.5)
        stopping = time.monotonic()
    stopped_in = time.monotonic() - stopping

    summary = sampler.summary()
    db_reads = [s for s in sink.resources() if s.container == "db"]
    assert len(db_reads) >= 4  # nosec B101 - pytest assertion
    assert summary.failed_reads == 1  # nosec B101 - pytest assertion
    # the ticks it held the container out of are counted too
    assert summary.busy_skips == len(db_reads) - 1  # nosec B101 - pytest assertion
    assert summary.max_tick_s < 0.3  # nosec B101 - pytest assertion
    # the hung read was submitted once, not at every tick, and stop() did not wait for it
    assert stopped_in < 1.0  # nosec B101 - pytest assertion
    hung_reads = [s for s in sink.resources() if s.container == "pfcon"]
    assert not hung_reads  # nosec B101 - pytest assertion


def test_a_failed_listing_is_counted_and_reads_nothing():
    class Unlistable:
        available = True

        def sampled_containers(self):
            raise RuntimeError("the engine went away")

    sampler = _ticking(Unlistable(), MetricSink())
    sampler._sample_containers()

    assert sampler.summary().list_failures == 1  # nosec B101 - pytest assertion
    assert sampler._listed is False  # nosec B101 - pytest assertion


def test_an_unavailable_docker_counts_every_tick_as_a_failed_listing():
    docker = SimpleNamespace(available=False, unavailable_reason="no socket")
    sampler = _ticking(docker, MetricSink())
    sampler._sample_containers()
    sampler._sample_containers()

    assert sampler.summary().list_failures == 2  # nosec B101 - pytest assertion


def test_an_unreadable_frame_is_a_failed_read_and_the_tick_goes_on():
    # a frame the conversion cannot read, listed before a good one
    garbage = StatsRead({"read": "2026-10-26T17:12:35Z", "cpu_stats": "garbage"})
    docker = ScriptedDocker(listings=[[BROKEN, DB]], started=STARTED,
                            reads={"x1": [garbage], "db1": [StatsRead(frame(200.0, 5e9))]})
    sink = MetricSink()

    with StatsSampler(docker, sink, interval=10.0) as sampler:
        pass

    summary = sampler.summary()
    assert [s.container for s in sink.resources()] == ["db", "db"]  # nosec B101 - pytest assertion
    assert (summary.reads, summary.failed_reads, summary.list_failures) == (4, 2, 0)  # nosec B101 - pytest assertion


# -- the sampler's lifetime ------------------------------------------------------------------

class SlowBroker:
    available = True

    def __init__(self, delay=0.0, depths=None):
        self._delay = delay
        self._depths = {"main2": 3} if depths is None else depths

    def depths(self):
        time.sleep(self._delay)
        return dict(self._depths)


def test_queue_depths_keep_their_cadence_while_container_reads_are_slow():
    docker = ScriptedDocker(listings=[[DB]], delay=0.15,
                            reads={"db1": [StatsRead(frame(200.0, 5e9))]})
    sink = MetricSink()

    with StatsSampler(docker, sink, interval=0.05, broker=SlowBroker(),
                      queue_interval=0.02) as sampler:
        time.sleep(0.3)

    summary = sampler.summary()
    assert summary.queue_ticks >= 10  # nosec B101 - pytest assertion
    assert len(sink.queues()) == summary.queue_ticks  # nosec B101 - pytest assertion
    # the reads, slower than a tick, missed it rather than holding the ticks back: waiting
    # for each 0.15 s read would leave about two ticks in the 0.3 s
    assert summary.failed_reads >= 1 and summary.ticks >= 5  # nosec B101 - pytest assertion


def test_stop_in_the_middle_of_both_ticks_waits_for_them_and_nothing_comes_after():
    docker = ScriptedDocker(listings=[[DB]], delay=0.4,
                            reads={"db1": [StatsRead(frame(200.0, 5e9))]})
    sink = MetricSink()
    sampler = StatsSampler(docker, sink, interval=10.0, broker=SlowBroker(delay=0.4),
                           queue_interval=10.0)
    sampler.start()
    time.sleep(0.15)                                   # both first ticks are running
    sampler.stop()
    counts = (len(sink.resources()), len(sink.queues()))
    time.sleep(0.3)

    # the first resource tick and the closing one, and the queue tick that was running
    assert counts == (2, 1)  # nosec B101 - pytest assertion
    assert (len(sink.resources()), len(sink.queues())) == counts  # nosec B101 - pytest assertion
    assert sampler._resources._thread is None  # nosec B101 - pytest assertion
    assert sampler._queues._thread is None  # nosec B101 - pytest assertion
    assert sampler._pool is None  # nosec B101 - pytest assertion


def test_a_stop_during_a_listing_that_succeeds_still_runs_the_closing_tick():
    class SlowListing(ScriptedDocker):
        def sampled_containers(self):
            time.sleep(0.2)
            return super().sampled_containers()

    docker = SlowListing(listings=[[DB]], started=STARTED,
                         reads={"db1": [StatsRead(frame(200.0, 5e9))]})
    sink = MetricSink()
    sampler = StatsSampler(docker, sink, interval=10.0).start()
    time.sleep(0.05)                                   # the first tick is listing
    sampler.stop()

    # the first tick and the closing one, which the listing in flight did not cancel
    assert sampler.summary().ticks == 2  # nosec B101 - pytest assertion
    assert len(sink.resources()) == 2  # nosec B101 - pytest assertion


def test_a_job_born_while_sampling_gets_its_cpu_since_it_started():
    # the job appears on the second tick, having started after the sampling began
    class Born(ScriptedDocker):
        def started_at(self, container_id):
            return self.born

        def read_stats(self, container_id):
            if container_id == "j1":
                return StatsRead(frame(self.born + 0.5, 0.5e9))
            return StatsRead(frame(time.time(), 1e9))

    docker = Born(listings=[[DB], [DB, JOB]], reads={})
    docker.born = time.time() + 0.01
    sink = MetricSink()

    with StatsSampler(docker, sink, interval=0.05):
        time.sleep(0.12)

    job = [s for s in sink.resources() if s.container == "chris-jid-7"]
    assert job[0].cpu_pct == 100.0  # nosec B101 - pytest assertion


def test_failed_queue_reads_are_counted():
    sampler = StatsSampler(ScriptedDocker([[]], {}), MetricSink(),
                           broker=SlowBroker(depths={}))
    sampler._sample_queues()

    assert sampler.summary().queue_failures == 1  # nosec B101 - pytest assertion


def test_an_unavailable_broker_is_not_sampled():
    broker = SimpleNamespace(available=False)
    sampler = StatsSampler(ScriptedDocker([[]], {}), MetricSink(), broker=broker)

    with sampler:
        pass

    summary = sampler.summary()
    assert summary.queue_interval_s is None  # nosec B101 - pytest assertion
    assert summary.queue_ticks == 0  # nosec B101 - pytest assertion


def test_sampler_record_names_the_method_and_its_settings():
    assert sampler_record(1.0, 0.5, 8) == {  # nosec B101 - pytest assertion
        "method": METHOD, "interval_s": 1.0, "queue_interval_s": 0.5, "workers": 8}


# -- against the stack -------------------------------------------------------------------------

def test_the_sampler_reads_every_running_service_each_second():
    """
    Against the running stack's containers; skipped without one (``just bench-test``
    after ``just bench-start`` runs it).
    """
    docker = DockerClient(project=os.environ.get("COMPOSE_PROJECT_NAME") or None,
                          exclude_services=("benchmark",), timeout=10)
    services = {ref.name for ref in docker.sampled_containers() if ref.kind == "service"}

    if "db" not in services:
        pytest.skip("no running stack: start the benchmark stack to run this test")

    sink = MetricSink()
    with StatsSampler(docker, sink) as sampler:
        time.sleep(3.5)

    by = defaultdict(list)
    for sample in sink.resources():
        by[sample.container].append(sample)

    assert services <= set(by)  # nosec B101 - pytest assertion
    # the services only: a plugin job a benchmark runs meanwhile lives a second or two
    for name, samples in ((name, by[name]) for name in services):
        gaps = sorted(b.t - a.t for a, b in zip(samples, samples[1:]))
        windows = [s.cpu_pct is not None for s in samples[1:4]]
        median_gap = gaps[len(gaps) // 2]

        assert len(samples) >= 4, name  # nosec B101 - pytest assertion
        assert samples[0].cpu_pct is None, name  # nosec B101 - pytest assertion
        assert all(windows), name  # nosec B101 - pytest assertion
        assert median_gap == pytest.approx(1.0, abs=0.25), name  # nosec B101 - pytest assertion

    summary = sampler.summary()
    trouble = (summary.late_ticks, summary.failed_reads, summary.list_failures)
    assert trouble == (0, 0, 0)  # nosec B101 - pytest assertion
