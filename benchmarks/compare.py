"""
Compare benchmark runs over time (pure logic + rendering + CLI).

Answers two questions about a *baseline* and a *candidate* run:

1. **Are they comparable at all?** Mismatched workload fingerprint, workload plugin
   versions, storage mode or auth mode are *blockers*; envelope, container-limit,
   Postgres-settings, resource-sampler, host, threshold and workload-plugin image
   differences are *cautions*; commit and service image differences are the point of
   comparing. The comparison never refuses — a loudly-labelled report beats an error.
2. **What changed?** Per (topology, axis, level): makespan and worst-p95 deltas
   flagged REGRESSED/IMPROVED only when they exceed BOTH a relative threshold and the
   metric's absolute floor (makespan is quantized by the poll cadence; latency has a
   noise floor), plus verdict flips and per-axis breaking-point shifts — the most
   noise-robust longitudinal signal.

With three or more runs (first = baseline, last = candidate), a per-axis trend table
tracks one metric across all of them.

Run via ``just bench-compare <run> <run> [...]``; run ids resolve against both
``results/`` and the committed ``history/`` archive.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from .models import RunData, describe_sampler
from .report import csv_cell, markdown_table, record_digests
from .results import default_results_root, load_run, resolve_run


BLOCKER, WARNING, INFO = "blocker", "warning", "info"

REGRESSED, IMPROVED, UNCHANGED = "REGRESSED", "IMPROVED", "UNCHANGED"

ONLY_ONE_RUN = "—"          # level present in only one run (explained by a bp shift)

#: level-row metrics that get delta flags (others are diagnostic, not SLO-like)
COMPARED_METRICS = ("makespan_median_s", "worst_p95_ms")

#: whole-run Postgres and db-container counters (``summary.json`` ``db_counters.run``)
#: shown side by side, unflagged: what they should be depends on the change under test
RUN_COUNTERS = ("reset", "unreadable", "num_done", "num_timed", "num_requested",
                "wal_bytes", "wal_fpi", "temp_bytes", "cgroup_write_bytes", "wal_init_bytes",
                "cgroup_pswpout", "cgroup_oom_kill", "seconds")

_VERDICT_SEVERITY = {"PASS": 0, "DEGRADED": 1, "FAIL": 2}


# --- data contracts ---------------------------------------------------------------------

@dataclass(frozen=True)
class FieldDiff:
    field: str
    baseline: object
    candidate: object
    severity: str               # BLOCKER | WARNING | INFO


@dataclass(frozen=True)
class Comparability:
    status: str                 # "comparable" | "caution" | "not_comparable"
    diffs: tuple[FieldDiff, ...]


@dataclass(frozen=True)
class MetricDelta:
    metric: str
    baseline: Optional[float]
    candidate: Optional[float]
    delta: Optional[float]
    pct: Optional[float]
    flag: str


@dataclass(frozen=True)
class LevelDelta:
    topology: str
    axis: str
    level: int
    baseline_verdict: Optional[str]
    candidate_verdict: Optional[str]
    metrics: tuple[MetricDelta, ...]
    flag: str                   # worst across metrics and verdict flip


@dataclass(frozen=True)
class AxisShift:
    topology: str
    axis: str
    baseline_level: Optional[int]    # None = no breaking point (clean to cap)
    candidate_level: Optional[int]
    direction: str                   # "regressed" | "improved" | "unchanged"


@dataclass(frozen=True)
class DeltaPolicy:
    """
    REGRESSED/IMPROVED require BOTH thresholds: makespan resolution is the poll
    cadence (~1-5 s) and API latency has a measured noise floor, so small absolute
    moves are noise regardless of how large they look in percent.
    """

    regress_pct: float = 20.0
    makespan_floor_s: float = 2.0
    latency_floor_ms: float = 50.0

    def floor_for(self, metric: str) -> float:
        if metric.startswith("makespan"):
            return self.makespan_floor_s
        
        if metric.endswith("_ms"):
            return self.latency_floor_ms
        return 0.0


@dataclass(frozen=True)
class RunComparison:
    baseline: RunData
    candidate: RunData
    comparability: Comparability
    shifts: tuple[AxisShift, ...]
    level_deltas: tuple[LevelDelta, ...]
    #: the thresholds that produced the flags — echoed in the report so a comparison
    #: rendered with non-default thresholds is distinguishable from a default one
    policy: "DeltaPolicy" = field(default_factory=lambda: DeltaPolicy())


# --- comparability ----------------------------------------------------------------------

#: (label, path into environment.json, severity) — workload identity handled separately
_SCALAR_FIELDS = (
    ("storage_mode", ("storage_mode",), BLOCKER),
    ("api_auth_mode", ("api_auth_mode",), BLOCKER),
    ("matrix.thresholds", ("matrix", "thresholds"), WARNING),
    ("matrix.repeat", ("matrix", "repeat"), WARNING),
    ("host.ncpu", ("host", "ncpu"), WARNING),
    ("host.mem_total_bytes", ("host", "mem_total_bytes"), WARNING),
    ("host.operating_system", ("host", "operating_system"), WARNING),
    ("host.architecture", ("host", "architecture"), WARNING),
    ("git.commit", ("git", "commit"), INFO),
    ("git.dirty", ("git", "dirty"), INFO),
    # image rebuilds without a commit change (base-image bump, dirty rebuild) are
    # part of "what version ran" — visible, never blocking
    ("images.chris", ("images", "chris", "image_id"), INFO),
    ("images.pfcon", ("images", "pfcon", "image_id"), INFO),
    ("images.db", ("images", "db", "image_id"), INFO),
    # the Celery broker and the shared cache
    ("images.chris-broker", ("images", "chris-broker", "image_id"), INFO),
    ("images.chris-cache", ("images", "chris-cache", "image_id"), INFO),
)

#: compose services renamed since runs were archived: in a run from before, their limits and
#: peaks read under the current names, so a rename is no difference
RENAMED_SERVICES = {"dragonflydb": "chris-broker"}

#: the level-row metrics the resource sampler produces: a trend of one across runs sampled
#: differently says so
SAMPLED_METRICS = ("peak_cpu_pct", "peak_job_cpu_pct", "peak_write_bytes", "peak_mem_bytes",
                   "peak_mem_anon_bytes", "cpu_throttled_s", "peak_queue_depth")

_NOISE_FLOOR_RATIO = 3.0


def _predates_rename(env: dict) -> bool:
    """
    Whether a run was recorded before the services were renamed: its images and limits
    name none of them by its current name. A later run that records an old name too ran
    beside containers left over from before, and shows them as they are.
    """
    current = set(RENAMED_SERVICES.values())
    return not any(service in current
                   for key in ("images", "limits") if isinstance(env.get(key), dict)
                   for service in env[key])


def _current_names(env: dict) -> dict:
    """
    A run from before the rename with its services under their current names in the
    images and limits recorded per service; a later run as it is.
    """
    if not _predates_rename(env):
        return env

    def renamed(block):
        return {RENAMED_SERVICES.get(service, service): value
                for service, value in block.items()}

    return {**env, **{key: renamed(env[key]) for key in ("images", "limits")
                      if isinstance(env.get(key), dict)}}


def _dig(env: dict, path: tuple) -> object:
    cur: object = env

    for key in path:
        cur = cur.get(key) if isinstance(cur, dict) else None
    return cur


def check_comparability(base_env: dict, cand_env: dict) -> Comparability:
    base_env, cand_env = _current_names(base_env), _current_names(cand_env)
    diffs: list[FieldDiff] = []

    # workload identity: unknown (pre-fingerprint run) is a warning, mismatch a blocker
    a = _dig(base_env, ("matrix", "workload_sha256"))
    b = _dig(cand_env, ("matrix", "workload_sha256"))

    if a is None or b is None:
        diffs.append(FieldDiff("matrix.workload_sha256",
                               a or "unknown (predates fingerprint)",
                               b or "unknown (predates fingerprint)", WARNING))
    elif a != b:
        diffs.append(FieldDiff("matrix.workload_sha256", a, b, BLOCKER))

    diffs += _workload_plugin_diffs(base_env.get("workload_plugins") or {},
                                    cand_env.get("workload_plugins") or {})
    diffs += _block_diffs("workload_plugins_pin", base_env.get("workload_plugins_pin") or {},
                          cand_env.get("workload_plugins_pin") or {}, INFO)

    for label, path, severity in _SCALAR_FIELDS:
        a, b = _dig(base_env, path), _dig(cand_env, path)
        if a != b:
            diffs.append(FieldDiff(label, a, b, severity))

    # STORAGE_ENV is already reported via the blocker-level storage_mode
    diffs += _block_diffs("envelope", base_env.get("envelope") or {},
                          cand_env.get("envelope") or {}, WARNING, skip=("STORAGE_ENV",))

    diffs += _limits_diffs(base_env.get("limits") or {}, cand_env.get("limits") or {})
    diffs += _sampler_diffs(base_env.get("sampler") or {}, cand_env.get("sampler") or {})
    diffs += _postgres_diffs(base_env.get("postgres") or {}, cand_env.get("postgres") or {})

    a = _dig(base_env, ("noise_floor", "health_p50_ms"))
    b = _dig(cand_env, ("noise_floor", "health_p50_ms"))
    
    if a and b and max(a, b) / min(a, b) > _NOISE_FLOOR_RATIO:
        diffs.append(FieldDiff("noise_floor.health_p50_ms", a, b, WARNING))

    if any(d.severity == BLOCKER for d in diffs):
        status = "not_comparable"
    elif any(d.severity == WARNING for d in diffs):
        status = "caution"
    else:
        status = "comparable"
        
    return Comparability(status, tuple(diffs))


def _block_diffs(label: str, base: dict, cand: dict, severity: str,
                 skip: tuple = ()) -> list[FieldDiff]:
    """
    One diff per key whose value differs between two flat blocks of the environment.
    """
    return [FieldDiff(f"{label}.{key}", base.get(key), cand.get(key), severity)
            for key in sorted(set(base) | set(cand))
            if key not in skip and base.get(key) != cand.get(key)]


def _unrecorded(label: str, base: dict, cand: dict) -> Optional[list[FieldDiff]]:
    """
    For an environment block older runs lack: no diff when neither run recorded it, one
    caution when only one did (not one per entry), ``None`` when both did.
    """
    if not base and not cand:
        return []

    if not base or not cand:
        return [FieldDiff(label, "recorded" if base else "not recorded",
                          "recorded" if cand else "not recorded", WARNING)]
    return None


def _workload_plugin_diffs(base: dict, cand: dict) -> list[FieldDiff]:
    """
    Differences in the workload plugins, per role: another name or version is another
    workload (a blocker, like the matrix fingerprint); another image reference is
    information, unless both name digests that differ, and other image digests a caution
    (the same version rebuilt, or a local build).
    """
    unrecorded = _unrecorded("workload_plugins", base, cand)
    if unrecorded is not None:
        return unrecorded

    def flat(plugins: dict, *fields: str) -> dict:
        return {f"{role}.{name}": (p or {}).get(name)
                for role, p in plugins.items() for name in fields}

    def digests(plugins: dict) -> dict:
        # on overlay2 a local build has none
        return {f"{role}.digests": " ".join(record_digests(p["image"])) or "none"
                for role, p in plugins.items() if (p or {}).get("image")}

    diffs = _block_diffs("workload_plugins", flat(base, "name", "version"),
                         flat(cand, "name", "version"), BLOCKER)
    diffs += [replace(d, severity=WARNING) if _other_digests(d.baseline, d.candidate) else d
              for d in _block_diffs("workload_plugins", flat(base, "dock_image"),
                                    flat(cand, "dock_image"), INFO)]

    base_digests, cand_digests = digests(base), digests(cand)
    unrecorded = _unrecorded("workload_plugins.image", base_digests, cand_digests)

    if unrecorded is not None:
        return diffs + unrecorded
    return diffs + _block_diffs("workload_plugins", base_digests, cand_digests, WARNING)


def _other_digests(a, b) -> bool:
    """
    Return ``True`` when both image references include SHA-256 digests and those digests
    differ; otherwise return ``False``.
    """
    a, b = (ref.rpartition("@")[2] if isinstance(ref, str) and "@sha256:" in ref else None
            for ref in (a, b))
    return a is not None and b is not None and a != b


def _limits_diffs(base: dict, cand: dict) -> list[FieldDiff]:
    """
    Cautions for differing container resource limits (``limits.<service>.<limit>``).
    """
    unrecorded = _unrecorded("limits", base, cand)

    if unrecorded is not None:
        return unrecorded

    def flat(limits: dict) -> dict:
        return {f"{service}.{name}": value for service, values in limits.items()
                for name, value in (values or {}).items()}

    return _block_diffs("limits", flat(base), flat(cand), WARNING)


def _sampler_diffs(base: dict, cand: dict) -> list[FieldDiff]:
    """
    Cautions for runs whose resources and queues were sampled differently, the peaks of one
    taken more sparsely or over other windows than the other's.
    """
    unrecorded = _unrecorded("sampler", base, cand)

    if unrecorded is not None:
        return unrecorded
    return _block_diffs("sampler", base, cand, WARNING)


def _postgres_diffs(base: dict, cand: dict) -> list[FieldDiff]:
    """
    Cautions for differing Postgres settings, ``ALTER SYSTEM`` overrides and table
    storage parameters.
    """
    unrecorded = _unrecorded("postgres", base, cand)

    if unrecorded is not None:
        return unrecorded

    diffs = _block_diffs("postgres", base.get("settings") or {},
                         cand.get("settings") or {}, WARNING)

    diffs += _block_diffs("postgres.table_options", base.get("table_options") or {},
                          cand.get("table_options") or {}, WARNING)

    if (base.get("altered") or []) != (cand.get("altered") or []):
        diffs.append(FieldDiff("postgres.altered", base.get("altered"),
                               cand.get("altered"), WARNING))
    return diffs


# --- level deltas -----------------------------------------------------------------------

def classify_delta(metric: str, baseline, candidate, policy: DeltaPolicy) -> MetricDelta:
    if baseline is None or candidate is None:
        # one-sided values must not masquerade as UNCHANGED (a CSV consumer filtering
        # on flags would silently drop or miscount them)
        return MetricDelta(metric, baseline, candidate, None, None, ONLY_ONE_RUN)
    
    delta = candidate - baseline
    pct = round(delta / baseline * 100.0, 1) if baseline else None
    flag = UNCHANGED
    
    if (abs(delta) >= policy.floor_for(metric)
            and (pct is None or abs(pct) >= policy.regress_pct)):
        flag = REGRESSED if delta > 0 else IMPROVED
    
    return MetricDelta(metric, baseline, candidate, round(delta, 2), pct, flag)


def _index(levels: list[dict]) -> dict[tuple, dict]:
    return {(r.get("topology"), r.get("axis"), r.get("level")): r for r in levels}


def _level_flag(base_verdict, cand_verdict, metrics: tuple[MetricDelta, ...]) -> str:
    if base_verdict is None or cand_verdict is None:
        return ONLY_ONE_RUN
    
    sev_a = _VERDICT_SEVERITY.get(base_verdict)
    sev_b = _VERDICT_SEVERITY.get(cand_verdict)
    
    if sev_a is not None and sev_b is not None and sev_a != sev_b:
        # a verdict flip dominates metric flags in BOTH directions: FAIL->PASS counts
        # as IMPROVED even if a metric regressed past its thresholds (the level went
        # from broken to working; the metric rows still show the regression, but it
        # does not trip the --fail-on-regression gate)
        return REGRESSED if sev_b > sev_a else IMPROVED
    
    flags = {m.flag for m in metrics}
    
    if REGRESSED in flags:
        return REGRESSED
    
    if IMPROVED in flags:
        return IMPROVED
    return UNCHANGED


def compare_levels(base_levels: list[dict], cand_levels: list[dict],
                   policy: DeltaPolicy) -> tuple[LevelDelta, ...]:
    """
    Join level rows on (topology, axis, level). Asymmetric ladders are expected —
    an axis that broke earlier has fewer levels; those rows flag as '—' and the
    breaking-point shift carries the meaning.
    """
    base_idx, cand_idx = _index(base_levels), _index(cand_levels)
    out = []

    for key in sorted(set(base_idx) | set(cand_idx),
                      key=lambda k: (str(k[0]), str(k[1]), k[2] or 0)):
        a, b = base_idx.get(key), cand_idx.get(key)
        
        metrics = tuple(classify_delta(m, (a or {}).get(m), (b or {}).get(m), policy)
                        for m in COMPARED_METRICS)
        
        base_verdict = (a or {}).get("verdict")
        cand_verdict = (b or {}).get("verdict")
        
        out.append(LevelDelta(key[0], key[1], key[2], base_verdict, cand_verdict,
                              metrics, _level_flag(base_verdict, cand_verdict, metrics)))
    return tuple(out)


# --- breaking-point shifts --------------------------------------------------------------

def breaking_point_shifts(base: RunData, cand: RunData) -> tuple[AxisShift, ...]:
    def bp_levels(run: RunData) -> dict[tuple, int]:
        return {(bp.get("topology"), bp.get("axis")): bp.get("level")
                for bp in run.summary.get("breaking_points", [])}

    base_axes = {(r.get("topology"), r.get("axis")) for r in base.levels}
    cand_axes = {(r.get("topology"), r.get("axis")) for r in cand.levels}
    base_bps, cand_bps = bp_levels(base), bp_levels(cand)

    shifts = []

    for key in sorted(base_axes & cand_axes):  # axes actually run in both
        a, b = base_bps.get(key), cand_bps.get(key)
        
        if a == b:
            direction = "unchanged"
        elif a is None:  # was clean to cap, now breaks
            direction = "regressed"
        elif b is None:  # broke before, now clean to cap
            direction = "improved"
        else:
            direction = "regressed" if b < a else "improved"

        shifts.append(AxisShift(key[0], key[1], a, b, direction))
    return tuple(shifts)


# --- top level --------------------------------------------------------------------------

def compare_runs(baseline: RunData, candidate: RunData,
                 policy: DeltaPolicy = DeltaPolicy()) -> RunComparison:
    return RunComparison(
        baseline=baseline, candidate=candidate,
        comparability=check_comparability(baseline.environment, candidate.environment),
        shifts=breaking_point_shifts(baseline, candidate),
        level_deltas=compare_levels(baseline.levels, candidate.levels, policy),
        policy=policy)


def flag_counts(level_deltas: tuple[LevelDelta, ...]) -> dict[str, int]:
    counts = {REGRESSED: 0, IMPROVED: 0, UNCHANGED: 0, ONLY_ONE_RUN: 0}
    
    for delta in level_deltas:
        counts[delta.flag] += 1
    return counts


def has_regressions(comparison: RunComparison) -> bool:
    """
    True if any level regressed (including a verdict flip toward FAIL) or any
    breaking point moved down — the --fail-on-regression gate.
    """
    return (any(d.flag == REGRESSED for d in comparison.level_deltas)
            or any(s.direction == "regressed" for s in comparison.shifts))


# --- trend (3+ runs) --------------------------------------------------------------------

def _metric_path(metric: str) -> tuple:
    """
    A level-row metric as a key path: ``peak_write_bytes.db`` or
    ``db_counters.workload.num_requested`` reach into the nested blocks.
    """
    return tuple(metric.split("."))


def _recorded_path(run: RunData, path: tuple) -> tuple:
    """
    ``path`` as ``run`` recorded it: a run from before the rename keeps its per-service
    peaks under the old names (``peak_cpu_pct.dragonflydb``).
    """
    if not _predates_rename(run.environment or {}):
        return path

    old_names = {current: old for old, current in RENAMED_SERVICES.items()}
    return tuple(old_names.get(key, key) for key in path)


def _sampler_note(runs: list[RunData], metric: str) -> list[str]:
    """
    A note when a sampled metric is trended across runs that were sampled differently:
    a run that records no sampler predates the 1 s sampler and read each container only
    every ~2 s times the number of containers, so its peaks are lower bounds.
    """
    if _metric_path(metric)[0] not in SAMPLED_METRICS:
        return []

    groups: dict[str, list[str]] = {}
    for run in runs:
        groups.setdefault(_sampler_label((run.environment or {}).get("sampler")),
                          []).append(run.run_id)

    if len(groups) < 2:
        return []

    parts = "; ".join(f"{', '.join(ids)}: {label}" for label, ids in groups.items())
    return [f"_The runs were sampled differently — {parts}._", ""]


def _sampler_label(sampler: dict | None) -> str:
    """
    Every setting of a run's sampler record, so that runs sampled alike share a label.
    """
    if not sampler:
        return "not recorded (the sparse sampler: lower bounds)"

    return describe_sampler(sampler)


def trend_tables(runs: list[RunData], metric: str) -> list[tuple[tuple, list, list]]:
    """
    Per (topology, axis): ((topology, axis), headers, rows) tracking ``metric`` (a
    dotted path for nested values) across all runs (chronological = argument order);
    last row is the breaking point.
    """
    paths = [_recorded_path(run, _metric_path(metric)) for run in runs]
    indexes = [_index(r.levels) for r in runs]

    axes = sorted({(r.get("topology"), r.get("axis"))
                   for run in runs for r in run.levels},
                  key=lambda k: (str(k[0]), str(k[1])))
    
    bp_maps = [{(bp.get("topology"), bp.get("axis")): bp.get("level")
                for bp in run.summary.get("breaking_points", [])} for run in runs]

    tables = []
    for axis_key in axes:
        levels = sorted({key[2] for idx in indexes for key in idx
                         if (key[0], key[1]) == axis_key},
                        key=lambda v: (v is None, v))
        
        headers = ["Level"] + [run.run_id for run in runs]

        rows = [[lvl] + [_dig(idx.get((*axis_key, lvl), {}), path)
                         for idx, path in zip(indexes, paths)]
                for lvl in levels]
        
        rows.append(["breaking point"] + [bps.get(axis_key) for bps in bp_maps])

        tables.append((axis_key, headers, rows))
    return tables


# --- rendering --------------------------------------------------------------------------

_STATUS_BANNER = {
    "comparable": "COMPARABLE",
    "caution": "CAUTION — runs differ in ways that may affect the numbers",
    "not_comparable": "NOT COMPARABLE — deltas below are unreliable",
}


def render_markdown(comparison: RunComparison, trend_runs: "list[RunData] | None" = None,
                    metric: str = "makespan_median_s") -> str:
    base, cand = comparison.baseline, comparison.candidate
    policy = comparison.policy

    lines = [
        "# CUBE Benchmark Comparison",
        "",
        f"**Baseline:** {base.run_id} "
        f"(commit {_dig(base.environment, ('git', 'commit')) or 'unknown'})  •  "
        f"**Candidate:** {cand.run_id} "
        f"(commit {_dig(cand.environment, ('git', 'commit')) or 'unknown'})",
        "",
        f"**Comparability: {_STATUS_BANNER[comparison.comparability.status]}**",
        "",
        f"_Flag thresholds: |Δ| ≥ {policy.regress_pct:g}% and ≥ "
        f"{policy.makespan_floor_s:g} s (makespan) / {policy.latency_floor_ms:g} ms "
        f"(p95)._",
        "",
    ]

    if comparison.comparability.diffs:
        lines += markdown_table(
            ["Field", "Baseline", "Candidate", "Severity"],
            [[d.field, d.baseline, d.candidate, d.severity]
             for d in comparison.comparability.diffs])
        lines.append("")

    lines += ["## Breaking-point shifts", ""]

    if comparison.shifts:
        lines += markdown_table(
            ["Topology", "Axis", "Baseline broke at", "Candidate broke at", "Direction"],
            [[s.topology, s.axis,
              s.baseline_level if s.baseline_level is not None else "did not break",
              s.candidate_level if s.candidate_level is not None else "did not break",
              s.direction.upper() if s.direction != "unchanged" else ""]
             for s in comparison.shifts])
    else:
        lines.append("_No axes in common._")
    
    lines.append("")
    lines += _run_counters_section(base, cand)

    counts = flag_counts(comparison.level_deltas)
    lines += [
        f"**Levels compared:** {counts[REGRESSED]} regressed, {counts[IMPROVED]} "
        f"improved, {counts[UNCHANGED]} unchanged"
        + (f", {counts[ONLY_ONE_RUN]} present in only one run"
           if counts[ONLY_ONE_RUN] else ""),
        "",
        "## Per-axis deltas",
        "",
    ]
    for axis_key, deltas in _group_by_axis(comparison.level_deltas).items():
        lines += [f"### {axis_key[0]} — {axis_key[1]}", ""]
        rows = []

        for d in deltas:
            mk, p95 = d.metrics            # COMPARED_METRICS order
            
            verdict = (d.baseline_verdict if d.baseline_verdict == d.candidate_verdict
                       else f"{d.baseline_verdict or '—'}→{d.candidate_verdict or '—'}")
            
            rows.append([d.level, mk.baseline, mk.candidate, _pct(mk.pct),
                         p95.baseline, p95.candidate, _pct(p95.pct), verdict,
                         d.flag if d.flag != UNCHANGED else ""])
        
        lines += markdown_table(
            ["Level", "Makespan A (s)", "Makespan B (s)", "Δ%",
             "p95 A (ms)", "p95 B (ms)", "Δ%", "Verdict", "Flag"], rows)
        lines.append("")

    if trend_runs:
        lines += [f"## Trend — {metric} across {len(trend_runs)} runs", ""]
        
        if not any(_dig(row, _recorded_path(run, _metric_path(metric))) is not None
                   for run in trend_runs for row in run.levels):
            lines += [f"_metric {metric!r} not found in any run's level rows — "
                      f"check --metric_", ""]

        lines += _sampler_note(trend_runs, metric)
        
        for axis_key, headers, rows in trend_tables(trend_runs, metric):
            lines += [f"### {axis_key[0]} — {axis_key[1]}", ""]
            lines += markdown_table(headers, rows)
            lines.append("")
    return "\n".join(lines) + "\n"


def _run_counters_section(base: RunData, cand: RunData) -> list[str]:
    """
    The two runs' whole-run Postgres and db-container counters, when either has them.
    """
    a = _dig(base.summary, ("db_counters", "run")) or {}
    b = _dig(cand.summary, ("db_counters", "run")) or {}

    if not a and not b:
        return []

    rows = []
    for key in RUN_COUNTERS:
        va, vb = a.get(key), b.get(key)

        if va is None and vb is None:
            continue

        numbers = all(isinstance(v, (int, float)) and not isinstance(v, bool)
                      for v in (va, vb))
        pct = round((vb - va) / va * 100.0, 1) if numbers and va else None
        rows.append([key, va, vb, _pct(pct)])

    return (["## Postgres over the whole run", "",
             "_Diagnostic, not flagged. `reset` (Postgres's statistics were reset during "
             "the run) or `unreadable` (Postgres could not be read at its start or end): "
             "Postgres's counters are unknown and only the container's remain._", ""]
            + markdown_table(["Counter", "Baseline", "Candidate", "Δ%"], rows) + [""])


def render_csv(comparison: RunComparison) -> str:
    """
    Tidy CSV: one row per (topology, axis, level, metric) — the plotting seam.
    """
    cols = ["topology", "axis", "level", "metric", "baseline", "candidate", "delta",
            "pct", "flag", "baseline_verdict", "candidate_verdict"]
    
    rows = [",".join(cols)]
    
    for d in comparison.level_deltas:
        for m in d.metrics:
            rows.append(",".join(csv_cell(v) for v in (
                d.topology, d.axis, d.level, m.metric, m.baseline, m.candidate,
                m.delta, m.pct, m.flag, d.baseline_verdict, d.candidate_verdict)))
    
    return "\n".join(rows) + "\n"


def _group_by_axis(deltas: tuple[LevelDelta, ...]) -> dict[tuple, list[LevelDelta]]:
    grouped: dict[tuple, list[LevelDelta]] = {}
    
    for d in deltas:
        grouped.setdefault((d.topology, d.axis), []).append(d)
    return grouped


def _pct(value: Optional[float]) -> Optional[str]:
    return None if value is None else f"{value:+.1f}%"


# --- CLI --------------------------------------------------------------------------------

def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(
        description="Compare benchmark runs (first = baseline, last = candidate; "
                    "3+ runs add a trend section)")
    ap.add_argument("runs", nargs="+",
                    help="run ids or run directories, oldest first")
    ap.add_argument("--metric", default=None,
                    help="level-row metric for the trend table (default "
                         "makespan_median_s); a dotted path reaches nested values, e.g. "
                         "peak_write_bytes.db or db_counters.workload.num_requested. "
                         "Given explicitly, the trend table also renders for two runs")
    ap.add_argument("--regress-pct", type=float, default=DeltaPolicy.regress_pct,
                    help="relative change (%%) required to flag a metric")
    ap.add_argument("--makespan-floor", type=float, default=DeltaPolicy.makespan_floor_s,
                    help="absolute makespan change (s) required to flag")
    ap.add_argument("--latency-floor", type=float, default=DeltaPolicy.latency_floor_ms,
                    help="absolute p95 change (ms) required to flag")
    ap.add_argument("--out", default=None,
                    help="output dir (default results/comparisons/<base>_vs_<cand>)")
    ap.add_argument("--fail-on-regression", action="store_true",
                    help="exit 1 if any level regresses, a breaking point moves down, "
                         "or the runs are not comparable")

    args = ap.parse_args(argv)
    if len(args.runs) < 2:
        ap.error("need at least two runs (baseline and candidate)")

    try:
        runs = [load_run(resolve_run(token)) for token in args.runs]
    except FileNotFoundError as exc:
        print(f"[compare] {exc}")
        return 2
    
    baseline, candidate = runs[0], runs[-1]
    policy = DeltaPolicy(args.regress_pct, args.makespan_floor, args.latency_floor)
    comparison = compare_runs(baseline, candidate, policy)

    trend = len(runs) > 2 or args.metric is not None
    markdown = render_markdown(comparison, trend_runs=runs if trend else None,
                               metric=args.metric or "makespan_median_s")
    
    out_dir = (Path(args.out) if args.out else
               default_results_root() / "comparisons"
               / f"{baseline.run_id}_vs_{candidate.run_id}")
    
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "compare.md").write_text(markdown)
    (out_dir / "compare.csv").write_text(render_csv(comparison))

    print(markdown)
    print(f"[compare] wrote {out_dir / 'compare.md'}")
    
    if args.fail_on_regression:
        # an incomparable pair must not gate green: passing thresholds on a
        # meaningless comparison proves nothing
        if comparison.comparability.status == "not_comparable":
            print("[compare] runs are not comparable (--fail-on-regression)")
            return 1
        
        if has_regressions(comparison):
            print("[compare] regressions detected (--fail-on-regression)")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
