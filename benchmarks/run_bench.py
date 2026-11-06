"""Composition root: wire the adapters and pure logic into a runnable benchmark.

This is the only module that knows about every other one. It builds the API client and
Docker client, resolves plugin ids, defines ``run_scenario`` (execute -> sample -> poll
-> aggregate -> classify -> persist -> clean up) and drives the escalation engine,
persisting artifacts and rendering the report.

Run inside the benchmark container:  ``python -m benchmarks.run_bench --tier smoke``
"""

from __future__ import annotations

import argparse
import contextlib
import os
import statistics
import time
from collections import Counter
from dataclasses import fields, replace
from pathlib import Path

from . import classifier, environment, escalation, metrics, report, topologies
from .broker import BrokerClient
from .chris_api import ChrisApi, ChrisApiError
from .config import PluginPins, Tier, load_pins, load_tier, step_for, tier_fingerprint
from .docker_client import DockerClient
from .pg_stats import (SERVER_START_KEY, PgStatStatements, counter_delta, counter_windows,
                       max_deltas, server_counters, server_counters_error)
from .executor import build_feeds
from .metrics import MetricSink
from .models import (BreakingPoint, Classification, LevelResult, PluginRole, SamplerSummary,
                     ScenarioParams, ScenarioResult, Verdict)
from .poller import ProgressProbe, poll_to_completion
from .recovery import recover_after_failure, wait_for_health, wait_for_quiescence
from .results import ResultsStore, default_results_root
from .stats_sampler import StatsSampler, sampler_record
from .units import parse_size


PLUGIN_NAMES = {PluginRole.FS: "dbg-bigfiles",
                PluginRole.DS: "pl-simpledsapp",
                PluginRole.TS: "pl-topologicalcopy"}

#: the resource sampler's tick, which is also its CPU window: one second, as in Docker's
#: own figure (a two-second window would average away the 2 s Celery beat's bursts)
STATS_INTERVAL_S = 1.0
#: a quarter of the 2 s Celery beat's period: a queue the beat fills shows within half a
#: second, at one of four fixed phases of each beat
QUEUE_INTERVAL_S = 0.5
SAMPLER_WORKERS = 8
#: the sampler's own Docker client gives up a call after this rather than docker-py's 60 s:
#: a tick waits for its reads only 0.8 of a tick, but a hung listing holds it
SAMPLER_DOCKER_TIMEOUT_S = 10.0

#: idle Postgres backends publish their statistics within ~10 s, so the run's last counter
#: snapshot waits this long to include them
RUN_STATS_SETTLE_S = 11.0

RESTART_SERVICES = ("chris", "worker-mains", "worker-periodic", "celery-scheduler")


class BenchmarkRunner:
    def __init__(self, *, url: str, username: str, password: str, tier: Tier,
                 pins: PluginPins, store: ResultsStore, cube_poll_interval: float,
                 project: str | None = None, cleanup: bool = True,
                 restart_on_fail: bool = False, api_timeout: float = 30.0,
                 startup_health_timeout: float = 60.0):
        self.tier = tier
        self.store = store
        self.cube_poll_interval = cube_poll_interval
        self.cleanup = cleanup
        self.restart_on_fail = restart_on_fail
        self._sink = MetricSink()

        self.api = ChrisApi(url, username, password, timeout=api_timeout,
                            observer=self._record_request)
       
        # the base compose healthcheck is a no-op, so gate on real readiness ourselves
        if not wait_for_health(self.api, startup_health_timeout):
            raise RuntimeError(
                f"CUBE not reachable at {url} after {startup_health_timeout:.0f}s")
        
        self.docker = DockerClient(project=project, exclude_services=("benchmark",))
        self.sampler_docker = DockerClient(project=project, exclude_services=("benchmark",),
                                           timeout=SAMPLER_DOCKER_TIMEOUT_S)
        
        broker_host, _, broker_port = os.environ.get(
            "BENCH_BROKER", "chris-broker:6379").partition(":")
        
        self.broker = BrokerClient(broker_host, int(broker_port or 6379))
        self.pg_stats = PgStatStatements(self.docker)
        
        self.pins = pins
        self.plugins = {role: resolve_plugin(self.api, name)
                        for role, name in PLUGIN_NAMES.items()}
        self.plugin_images = {role: self.docker.image_info(p["dock_image"])
                              for role, p in self.plugins.items()}

        # a run against anything but the pinned registrations measures another workload
        problems = pin_mismatches(self.plugins, pins, self.plugin_images,
                                  docker_available=self.docker.available)
        if problems:
            raise RuntimeError(pin_mismatch_message(pins, problems))

        self.plugin_ids = {role: p["id"] for role, p in self.plugins.items()}

    def _record_request(self, record) -> None:
        self._sink.record_request(record)

    # -- escalation glue -------------------------------------------------------------

    def make_params(self, topology: str, axis: str, level: int,
                    repeat_index: int) -> ScenarioParams:
        return make_scenario_params(self.tier.baseline, self.cube_poll_interval,
                                    topology, axis, level, repeat_index)

    def run_scenario(self, params: ScenarioParams) -> ScenarioResult:
        self._sink = MetricSink()                      # isolate this scenario's events
        topo = topologies.build(params.topology, params)
        before_counts = self._global_counts()
        poll_every = effective_poll_cadence(self.tier.poll_every_s, params.feeds)
        self.pg_stats.reset()                          # per-scenario query attribution
        db_start = self._db_snapshot()
        started = time.time()

        with StatsSampler(self.sampler_docker, self._sink, interval=STATS_INTERVAL_S,
                          broker=self.broker, queue_interval=QUEUE_INTERVAL_S,
                          workers=SAMPLER_WORKERS) as sampler:
            builds = build_feeds(self.api, topo, params.feeds, self.plugin_ids)
            feeds = [b.feed for b in builds if b.feed is not None]
            instances = [ref for b in builds for ref in b.instances]
            build_errors = [b.error for b in builds if b.error]
           
            poll = poll_to_completion(
                self.api, [f.feed_id for f in feeds], instances,
                poll_every=poll_every,
                scenario_timeout=self.tier.thresholds.scenario_timeout_s,
                no_progress_timeout=self.tier.thresholds.no_progress_timeout_s,
                progress_probe=ProgressProbe(self.docker))

        ended = time.time()
        # the workload window's samples, which the rollups and the persisted files share
        samples = (self._sink.resources(), self._sink.queues())
        db_workload_end = self._db_snapshot()          # the resource samples' window
        db_deltas = self._db_deltas(before_counts, feeds)   # before cleanup deletes them
        red = metrics.red_rollup(self._sink.requests())
        resources = metrics.resource_rollup(samples[0])
        queues = metrics.queue_rollup(samples[1])
        
        classification = apply_build_errors(
            classifier.classify(poll, red, self.tier.thresholds), build_errors)

        result = ScenarioResult(
            params=params, classification=classification, makespan_s=poll.makespan_s,
            feeds=feeds, timelines=poll.timelines, red=red, resources=resources,
            db_deltas=db_deltas, started_at=started, ended_at=ended, queues=queues,
            notes=build_errors, sampler=sampler.summary())

        db_cleanup_end = None
        if self.cleanup:
            self._cleanup_and_quiesce(feeds, before_counts, result.notes)
            db_cleanup_end = self._db_snapshot()

        result.db_counters = counter_windows(db_start, db_workload_end, db_cleanup_end)

        # persist after cleanup so delete/quiesce traffic reaches api_requests.jsonl
        # and cleanup anomalies land in the scenario's notes
        self._persist_scenario(result, poll_every, *samples)
        return result

    # -- persistence -----------------------------------------------------------------

    def _persist_scenario(self, result: ScenarioResult, poll_every: float,
                          resources: list, queues: list) -> None:
        sid = result.params.scenario_id()
        phases = metrics.timelines_rollup(result.timelines)
        requests = self._sink.requests()
        self.store.scenario_dir(sid)
        
        self.store.write_json(f"scenarios/{sid}/scenario.json", {
            "params": result.params, "verdict": result.classification.verdict,
            "criteria": list(result.classification.criteria),
            "makespan_s": result.makespan_s, "red": result.red,
            "resources": result.resources, "phases": phases,
            "db_deltas": result.db_deltas,
            "feeds": [f.feed_id for f in result.feeds], "notes": result.notes,
            "queues": result.queues, "db_counters": result.db_counters,
            "observer": {            # the harness's own measurement traffic, made visible
                "poll_requests": sum(1 for r in requests if r.endpoint_class == "poll"),
                "poll_every_s": poll_every,
                "sampler": result.sampler,
            },
            "duration_s": round(result.ended_at - result.started_at, 2)})
        
        pg_top = self.pg_stats.snapshot()
        if pg_top:
            self.store.write_json(f"scenarios/{sid}/pg_stats.json", pg_top)
        
        self.store.append_jsonl("api_requests.jsonl",
                                ({"scenario": sid, **r.__dict__} for r in requests))
        
        self.store.append_jsonl("docker_stats.jsonl",
                                ({"scenario": sid, **s.__dict__} for s in resources))
        
        self.store.append_jsonl("queue_depths.jsonl",
                                ({"scenario": sid, **q.__dict__} for q in queues))
        
        self.store.append_jsonl("status_samples.jsonl", (
            {"scenario": sid, "instance_id": tl.instance_id, "node": tl.node_key,
             "role": tl.role.value if tl.role else None,
             "first_seen": tl.first_seen, "final_status": tl.final_status}
            for tl in result.timelines))

    def _on_level(self, level: LevelResult) -> None:
        self.store.append_jsonl("levels.jsonl", [_level_row(level)])

    def _on_fail(self, level: LevelResult) -> None:
        # repeats short-circuit on FAIL, so the failing repeat is always the last one;
        # its scenario dir is where scenario.json already lives
        sid = (level.repeats[-1].params.scenario_id() if level.repeats
               else f"{level.topology}_{level.axis}-{level.level}")
        
        out_dir = self.store.scenario_dir(sid)
        
        summary = recover_after_failure(
            self.docker, self.api, out_dir, services=RESTART_SERVICES,
            restart=self.restart_on_fail,
            health_timeout=self.tier.thresholds.healthcheck_timeout_s)
        
        self.store.write_json(f"scenarios/{sid}/failure.json",
                              {"criteria": list(level.criteria), "recovery": summary})

    def _cleanup_and_quiesce(self, feeds, before_counts: dict, notes: list) -> None:
        """
        Delete the scenario's feeds and wait for CUBE to settle back to the
        pre-scenario baseline. Feed deletion is asynchronous, so returning as soon as
        the DELETEs are accepted would let deletion churn bleed into the next level's
        measurements; anomalies are recorded in the scenario's notes.
        """
        failed = 0
        for feed in feeds:
            try:
                if not self.api.delete_feed(feed.feed_id):
                    failed += 1
            except Exception:                          # noqa: BLE001 - best effort
                failed += 1

        if failed:
            notes.append(f"cleanup_delete_failed:{failed}")

        if feeds and not wait_for_quiescence(self._global_counts, before_counts,
                                             self.tier.thresholds.quiesce_timeout_s):
            notes.append(
                f"quiesce_incomplete_after_{self.tier.thresholds.quiesce_timeout_s}s")

    def _db_snapshot(self) -> dict:
        """
        The Postgres server's cumulative counters plus the db container's cgroup counters
        (bytes written, swap, OOM kills), so a window's delta also covers the writes
        Postgres makes after its statements end (checkpoints, background writes). Each
        half is read on its own, even when the other fails (the container's counters
        while Postgres is in crash recovery, say), and a failure only leaves its keys
        out: instrumentation must never end a scenario or lose a run's summary.
        """
        snapshot: dict = {}

        with contextlib.suppress(Exception):
            snapshot.update(server_counters(self.docker))

        with contextlib.suppress(Exception):
            snapshot.update(self.docker.cgroup_counters("db"))
        return snapshot

    def _counters_error(self) -> str:
        """
        Why the Postgres counters cannot be read, from psql itself. The query runs
        again for this, so a failure that has cleared since reads "they read empty".
        """
        try:
            return server_counters_error(self.docker) or "they read empty"
        except Exception as exc:                       # noqa: BLE001 - best effort
            return repr(exc)

    def _global_counts(self) -> dict:
        return {"feeds": self.api.feed_count(), "instances": self.api.instance_count(),
                "files": self.api.file_count()}

    def _db_deltas(self, before: dict, feeds) -> dict:
        """
        Feed/instance/file count deltas plus the absolute totals after the scenario
        (read before cleanup deletes the feeds). The absolutes are the x-axis for
        state-aging analysis: probe latency as a function of accumulated DB rows.
        """
        after = self._global_counts()
        output_bytes = 0

        for feed in feeds:
            for inst in self.api.get_feed_instances(feed.feed_id):
                output_bytes += int(inst.get("size") or 0)

        return {
            "feeds_delta": after["feeds"] - before["feeds"],
            "instances_delta": after["instances"] - before["instances"],
            "files_delta": after["files"] - before["files"],
            "feeds_total": after["feeds"],
            "instances_total": after["instances"],
            "files_total": after["files"],
            "registered_output_bytes": output_bytes,
        }

    # -- top-level run ---------------------------------------------------------------

    def run(self) -> dict:
        env = environment.collect_environment(self.docker)
        self._sink = MetricSink()  # keep noise-floor probes out of scenarios

        if not env.get("postgres"):
            print("[bench] WARNING: could not read the Postgres settings; this run's "
                  "environment.json records none", flush=True)
        
        env["noise_floor"] = environment.measure_noise_floor(self.api)
        
        # 'basic' here means per-request password hashing inflated every number — the
        # report surfaces it so a degraded run can't masquerade as a clean one
        env["api_auth_mode"] = self.api.auth_mode
        
        # effective-workload identity: bench-compare uses this to decide whether two
        # runs are comparable at all
        env["matrix"] = tier_fingerprint(self.tier)
        
        # how the resources and queues were sampled: bench-compare cautions when two runs
        # were sampled differently
        env["sampler"] = sampler_record(
            STATS_INTERVAL_S, QUEUE_INTERVAL_S if self.broker.available else None,
            SAMPLER_WORKERS)

        env["attribution"] = {
            "resource_sampling": True if self.sampler_docker.available
            else self.sampler_docker.unavailable_reason,
            "queue_sampling": True if self.broker.available
            else self.broker.unavailable_reason,
            "pg_stat_statements": True if self.pg_stats.available
            else self.pg_stats.unavailable_reason,
        }

        # the workload plugins the run used, the images they resolved to, and the pin they
        # were checked against
        env["workload_plugins"] = build_workload_manifest(self.plugins, self.plugin_images)
        env["workload_plugins_pin"] = self.pins.record()

        # Postgres counters around the whole run, which the per-scenario windows add up
        # to but for a few seconds of statistics at their edges
        db_start = self._db_snapshot()
        env["attribution"]["db_counters"] = (
            True if SERVER_START_KEY in db_start
            else f"Postgres counters unreadable: {self._counters_error()}")

        self.store.write_json("environment.json", env)

        levels, breaking = escalation.run(
            self.tier.topology_axes, self.tier.repeat, self.run_scenario,
            self.make_params, on_level=self._on_level, on_fail=self._on_fail,
            step_for=step_for)

        if db_start:
            time.sleep(RUN_STATS_SETTLE_S)
        db_end = self._db_snapshot()

        summary = {
            "run_id": self.store.run_id, "tier": self.tier.name,
            "environment": env, "levels_run": len(levels),
            "breaking_points": [_bp_row(bp) for bp in breaking],
            "verdict_counts": dict(Counter(lv.verdict.value for lv in levels)),
            "db_counters": {"run": counter_delta(db_start, db_end),
                            "snapshots": {"start": db_start, "end": db_end}},
        }

        self.store.write_json("summary.json", summary)
        level_rows = self.store.read_jsonl("levels.jsonl")
        self.store.write_text("report.md", report.render(summary, level_rows))
        self.store.write_text("levels.csv", report.render_levels_csv(level_rows))
        return summary


# -- pure helpers ----------------------------------------------------------------------

def apply_build_errors(classification: Classification, build_errors: list) -> Classification:
    """
    A DAG that could not be fully created (e.g. a 4xx) is a hard failure for the
    level, even if everything that *was* created completed cleanly.
    """
    if build_errors and classification.verdict is not Verdict.FAIL:
        return Classification(
            Verdict.FAIL,
            classification.criteria + (f"build_error:{len(build_errors)}",))
    return classification


def effective_poll_cadence(base: float, feeds: int) -> float:
    """
    Observation cadence for a scenario. The poller issues one search per feed per
    cycle, so a fixed cadence makes the harness's own traffic grow linearly with the
    feeds axis; slowing the cadence as feeds escalate keeps observer load roughly
    bounded (~20 req/s) at the cost of coarser phase timing — which is already
    cadence-limited by design.
    """
    return max(base, min(5.0, feeds * 0.05))


def make_scenario_params(baseline, poll_interval: float, topology: str, axis: str,
                         level: int, repeat_index: int) -> ScenarioParams:
    """
    OFAT: take the tier baseline and override exactly the escalated axis with level.
    """
    b = baseline.as_dict()
    b[axis] = level

    return ScenarioParams(
        topology=topology, axis=axis, level=level, file_count=b["file_count"],
        file_size=b["file_size"], depth=b["depth"], branches=b["branches"],
        layers=b["layers"], merges=b["merges"], feeds=b["feeds"],
        sleep_length=b["sleep_length"], poll_interval=poll_interval,
        repeat_index=repeat_index)


def _level_row(level: LevelResult) -> dict:
    reps = level.repeats
    makespans = [r.makespan_s for r in reps if r.makespan_s is not None]
    p95s = [_worst_p95(r.red) for r in reps]
    # service containers only; a reading without a CPU window has no peak to give
    peaks: dict[str, dict] = {"cpu": {}, "write": {}, "mem": {}, "anon": {}, "throttled": {}}
    peak_job_cpu = None                          # one aggregate over job containers
    peak_queue: dict[str, int] = {}
    final = Counter()

    for r in reps:
        for svc, m in r.resources.items():
            if m.get("kind") == "job":          # collapse ephemeral chris-jid-* jobs
                peak_job_cpu = _larger(peak_job_cpu, m.get("cpu_pct_peak"))
                continue

            for peak, key in (("cpu", "cpu_pct_peak"), ("write", "blkio_write_bytes"),
                              ("mem", "mem_bytes_peak"), ("anon", "mem_anon_peak"),
                              ("throttled", "cpu_throttled_s")):
                if m.get(key) is not None:
                    peaks[peak][svc] = _larger(peaks[peak].get(svc), m[key])

        for q, m in r.queues.items():
            peak_queue[q] = max(peak_queue.get(q, 0), m.get("depth_peak", 0))
        
        for tl in r.timelines:
            if tl.final_status:
                final[tl.final_status] += 1
    
    return {
        "topology": level.topology, "axis": level.axis, "level": level.level,
        "verdict": level.verdict.value, "criteria": list(level.criteria),
        "repeats": len(reps),
        "makespan_median_s": round(statistics.median(makespans), 2) if makespans else None,
        "makespan_min_s": round(min(makespans), 2) if makespans else None,
        "makespan_max_s": round(max(makespans), 2) if makespans else None,
        "worst_p95_ms": max(p95s) if p95s else 0.0,
        "errors": sum(r.red.get("errors", 0) for r in reps),
        "status_5xx": sum(r.red.get("status_5xx", 0) for r in reps),
        "completed": final.get("finishedSuccessfully", 0),
        "final_status_counts": dict(final),
        "peak_cpu_pct": {k: round(v, 1) for k, v in peaks["cpu"].items()},
        "peak_job_cpu_pct": round(peak_job_cpu, 1) if peak_job_cpu is not None else None,
        "peak_write_bytes": peaks["write"],
        "peak_mem_bytes": peaks["mem"],
        "peak_mem_anon_bytes": peaks["anon"],
        "cpu_throttled_s": peaks["throttled"],
        "peak_queue_depth": peak_queue,
        "registered_output_bytes": max(
            (r.db_deltas.get("registered_output_bytes", 0) for r in reps), default=0),
        "db_counters": _db_counters_rollup(reps),
        "sampler": _sampler_rollup(reps),
    }


def _larger(a, b):
    """
    The larger of two values either of which may be missing (``None``).
    """
    return b if a is None else a if b is None else max(a, b)


def _sampler_rollup(reps) -> dict:
    """
    The repeats' sampler summaries rolled up as each field says (the counts added up, the
    longest tick the longest of them); ``{}`` when none has one.
    """
    summaries = [r.sampler for r in reps if r.sampler is not None]

    if not summaries:
        return {}

    rollups = {"sum": sum, "max": max, "first": lambda values: values[0]}
    return {f.name: rollups[f.metadata["rollup"]]([getattr(s, f.name) for s in summaries])
            for f in fields(SamplerSummary)}


def _db_counters_rollup(reps) -> dict:
    """
    Per window (workload, cleanup), each counter's largest delta across the level's
    repeats, like the other peaks (``pg_stats.max_deltas``).
    """
    out = {}

    for window in ("workload", "cleanup"):
        deltas = [r.db_counters[window] for r in reps
                  if r.db_counters.get(window) is not None]

        if deltas:
            out[window] = max_deltas(deltas)
    return out


def _worst_p95(red: dict) -> float:
    return max((c.get("p95_ms", 0.0) for c in red.get("by_class", {}).values()),
               default=0.0)


def _bp_row(bp: BreakingPoint) -> dict:
    return {"topology": bp.topology, "axis": bp.axis, "level": bp.level,
            "criteria": list(bp.criteria)}


def build_workload_manifest(plugins: dict, images: dict) -> dict:
    """
    Per-role workload-plugin provenance for the environment manifest:
    ``{role.value: {name, version, id, dock_image, matches, image}}``. ``matches`` is how
    many installed versions the name resolved to (a run starts only with one), and
    ``image`` the local image the reference resolved to (``docker_client.image_record``;
    ``None`` when Docker could not tell).
    """
    return {role.value: {"name": p["name"], "version": p["version"],
                         "id": p["id"], "dock_image": p["dock_image"],
                         "matches": p["matches"], "image": images.get(role)}
            for role, p in plugins.items()}


def resolve_plugin(api: ChrisApi, name: str) -> dict:
    """
    ``api.get_plugin(name)``, or a record of no match when CUBE has no plugin of that
    name, so that ``pin_mismatches`` reports it with the other problems.
    """
    try:
        return api.get_plugin(name)
    except ChrisApiError as exc:
        if exc.result.status_code != 404:
            raise
        return {"id": None, "name": name, "version": None, "dock_image": None,
                "matches": 0, "installed_versions": []}


def pin_mismatches(plugins: dict, pins: PluginPins, images: dict, *,
                   docker_available: bool) -> list[str]:
    """
    How CUBE's workload-plugin registrations differ from the pin, one line per problem.
    Empty when each plugin has exactly one version, the pinned one under the pinned
    image, and (where Docker can tell) that image is present: otherwise pfcon would pull
    it inside the first measured scenario.
    """
    problems = []

    for role, p in plugins.items():
        name = p["name"]
        pin = pins.plugins.get(name)

        if pin is None:
            problems.append(f"{name} is not pinned in {pins.path}")
            continue

        if p.get("matches", 1) == 0:
            problems.append(f"{name} is not registered")
            continue

        if p.get("matches", 1) != 1:
            versions = ", ".join(str(v) for v in p.get("installed_versions") or [])
            problems.append(f"{name} has {p['matches']} versions registered ({versions}), "
                            f"not only the pinned {pin.version}")
            continue

        if p.get("version") != pin.version:
            problems.append(f"{name} {p.get('version')} is registered, {pin.version} "
                            f"is pinned")

        image = images.get(role)

        if p.get("dock_image") != pin.dock_image:
            problems.append(f"{name} is registered as {p.get('dock_image')}, pinned as "
                            f"{pin.dock_image}")
        elif docker_available and image is None:
            problems.append(f"{name}'s image {pin.dock_image} is not present")
        elif image and "error" in image:
            problems.append(f"{name}'s image {pin.dock_image} could not be read: "
                            f"{image['error']}")
    return problems


def pin_mismatch_message(pins: PluginPins, problems: list[str]) -> str:
    """
    Why the runner refuses to start and what puts the pinned registrations back: the
    pin ``just bench-start`` registered when it was another one, else fresh volumes.
    """
    lines = "\n".join(f"  - {problem}" for problem in problems)
    pin = Path(pins.path).name
    return (f"[bench] the workload plugins in CUBE are not the ones {pins.path} pins:\n"
            f"{lines}\n"
            f"If 'just bench-start' was given another pin, check against the same one: "
            f"'just bench-run --plugins <its file name> ...'. Otherwise register this one "
            f"on fresh volumes: 'just bench-nuke' (it deletes the dev stack's data too: the "
            f"two stacks share their volumes), then 'just bench-start {pin}'. A missing "
            f"image alone: 'just bench-pull-plugins {pin}'.")


# -- CLI -------------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _size_arg(text: str) -> str:
    parse_size(text)  # validate eagerly; the harness keeps the original string
    return text


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="CUBE load & scalability benchmark (data plane)")
    p.add_argument("--tier", default="smoke",
                   help="matrix tier: smoke | default | full | "
                        "aging-grow | aging-probe")
    p.add_argument("--matrix", default=None, help="path to matrix.yml")
    p.add_argument("--plugins", default=None,
                   help="the chrisomatic pin `just bench-start` registered the workload "
                        "plugins from: a file name in benchmarks/chrisomatic/ (as for "
                        "bench-start) or a path; default benchmark.yml")
    p.add_argument("--topology", default=None,
                   help="restrict to one topology (linear|fanout_fanin|diamond)")
    p.add_argument("--axis", default=None, help="restrict to one escalation axis")
    p.add_argument("--repeat", type=int, default=None, help="override tier repeat count")
    p.add_argument("--cap", type=int, default=None, help="override every axis cap")
    p.add_argument("--file-size", type=_size_arg, default=None,
                   help="override baseline file size, e.g. 1MiB (the 2-point size "
                        "sanity check; fslink hides byte-copy cost)")
    p.add_argument("--file-count", type=int, default=None,
                   help="override baseline file count (held while escalating a "
                        "non-file_count axis; e.g. the heavy combined-stress feeds run)")
    p.add_argument("--sleep-length", type=float, default=None,
                   help="override baseline simpledsapp sleepLength in seconds "
                        "(long-active-job workload class)")
    p.add_argument("--poll-interval", type=float,
                   default=_env_float("CUBE_CELERY_POLL_INTERVAL", 2.0),
                   help="CUBE poll interval to record (set via compose env)")
    p.add_argument("--url", default=os.environ.get("CUBE_URL",
                                                    "http://chris:8000/api/v1/"))
    p.add_argument("--username", default=os.environ.get("CUBE_USERNAME", "chris"))
    p.add_argument("--password", default=os.environ.get("CUBE_PASSWORD", "chris1234"))
    p.add_argument("--results-dir", default=str(default_results_root()))
    p.add_argument("--no-cleanup", action="store_true",
                   help="keep created feeds (study cumulative-growth effects)")
    p.add_argument("--restart-on-fail", action="store_true",
                   help="restart services if health does not recover after a failure")
    return p


def _filter_tier(tier: Tier, topology: str | None, axis: str | None,
                 cap: int | None, repeat: int | None) -> Tier:
    topology_axes = tier.topology_axes

    if topology:
        if topology not in topology_axes:
            raise ValueError(f"topology {topology!r} not in tier {tier.name!r} "
                             f"(have: {sorted(topology_axes)})")
        topology_axes = {topology: topology_axes[topology]}
    
    if axis:
        topology_axes = {t: {axis: axes[axis]} for t, axes in topology_axes.items()
                         if axis in axes}
        if not topology_axes:
            raise ValueError(f"axis {axis!r} not in any selected topology of tier "
                             f"{tier.name!r}")
    if cap is not None:
        topology_axes = {t: {a: cap for a in axes} for t, axes in topology_axes.items()}
    
    return Tier(name=tier.name, baseline=tier.baseline, thresholds=tier.thresholds,
                repeat=repeat if repeat is not None else tier.repeat,
                poll_every_s=tier.poll_every_s, topology_axes=topology_axes)


def _override_baseline(tier: Tier, file_size: str | None,
                       sleep_length: float | None,
                       file_count: int | None = None) -> Tier:
    """
    Apply CLI baseline overrides (the file-size sanity check, the long-active-job
    workload class, and the baseline file count) without editing matrix.yml.
    """
    changes = {}

    if file_size is not None:
        changes["file_size"] = file_size

    if sleep_length is not None:
        changes["sleep_length"] = sleep_length

    if file_count is not None:
        changes["file_count"] = file_count

    if not changes:
        return tier
    return replace(tier, baseline=replace(tier.baseline, **changes))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tier = _filter_tier(load_tier(args.tier, args.matrix), args.topology, args.axis,
                        args.cap, args.repeat)
    tier = _override_baseline(tier, args.file_size, args.sleep_length, args.file_count)
    pins = load_pins(args.plugins)
    store = ResultsStore(args.results_dir)
    
    runner = BenchmarkRunner(
        url=args.url, username=args.username, password=args.password, tier=tier,
        pins=pins, store=store, cube_poll_interval=args.poll_interval,
        project=os.environ.get("COMPOSE_PROJECT_NAME"),
        cleanup=not args.no_cleanup, restart_on_fail=args.restart_on_fail)

    print(f"[bench] run {store.run_id} tier={tier.name} -> {store.run_dir}", flush=True)
    summary = runner.run()
    
    print(f"[bench] done: {summary['levels_run']} levels, "
          f"{len(summary['breaking_points'])} breaking point(s)", flush=True)
    
    print(f"[bench] report: {store.run_dir / 'report.md'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
