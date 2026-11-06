"""
Render the run summary into a ticket-ready ``report.md`` (pure transform).

Reads the aggregated ``summary`` dict plus the per-level rows the runner appended to
``levels.jsonl`` and emits Markdown: the environment manifest, the breaking-point table,
and per-axis approach-to-failure curves (also available as tidy CSV).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import fields
from pathlib import Path
from typing import Iterable

from .models import SamplerSummary, describe_sampler


def render(summary: dict, levels: list[dict]) -> str:
    env = summary.get("environment", {})
    lines: list[str] = []
    lines += _header(summary, env)
    lines += _breaking_points(summary.get("breaking_points", []))
    lines += _run_db_counters(summary.get("db_counters") or {})
    lines += _approach_curves(levels)
    lines += _environment_section(env)
    return "\n".join(lines) + "\n"


def render_levels_csv(levels: list[dict]) -> str:
    cols = ["topology", "axis", "level", "verdict", "makespan_median_s", "makespan_max_s",
            "worst_p95_ms", "errors", "status_5xx", "completed", "criteria"]
    
    rows = [",".join(cols)]
    
    for lv in levels:
        rows.append(",".join(csv_cell(lv.get(c)) for c in cols))
    return "\n".join(rows) + "\n"


# -- sections --------------------------------------------------------------------------

def _header(summary: dict, env: dict) -> list[str]:
    git = env.get("git", {})

    return [
        "# CUBE Load & Scalability Benchmark — Report",
        "",
        f"**Run:** {summary.get('run_id', '?')}  •  "
        f"**Tier:** {summary.get('tier', '?')}  •  "
        f"**Storage:** {env.get('storage_mode', '?')}  •  "
        f"**Commit:** {git.get('commit', '?')} (dirty: {git.get('dirty', '?')})",
        "",
        f"Levels run: {summary.get('levels_run', 0)}  •  "
        f"Breaking points: {len(summary.get('breaking_points', []))}",
        "",
    ]


def _breaking_points(bps: list[dict]) -> list[str]:
    out = ["## Breaking points", ""]
    
    if not bps:
        out += ["_No hard failure reached within the configured caps._", ""]
        return out
    
    out += markdown_table(
        ["Topology", "Axis", "Broke at level", "Criteria"],
        [[bp.get("topology"), bp.get("axis"), bp.get("level"),
          "; ".join(bp.get("criteria", []))] for bp in bps])
    
    out.append("")
    return out


def _approach_curves(levels: list[dict]) -> list[str]:
    out = ["## Approach to failure (per axis)", "",
           "_CPU% is relative to one host core (100% = one core); judge saturation "
           "against each service's `cpus` limit in the envelope, not against 100%._",
           ""]
    
    for (topology, axis), rows in _group_by_axis(levels).items():
        out.append(f"### {topology} — {axis}")
        out.append("")

        out += markdown_table(
            ["Level", "Verdict", "Makespan p50 (s)", "Worst p95 (ms)", "5xx",
             "Completed", "Criteria"],
            [[r.get("level"), r.get("verdict"), r.get("makespan_median_s"),
              r.get("worst_p95_ms"), r.get("status_5xx"), r.get("completed"),
              "; ".join(r.get("criteria", []))] for r in rows])
        
        out.append("")
        out += _peak_cpu_note(rows)
        out += _peak_io_note(rows)
        out += _db_counters_note(rows)
        out += _sampler_note(rows)
    return out


_SAMPLER_TROUBLE = tuple(f.name for f in fields(SamplerSummary) if f.metadata.get("trouble"))


def _sampler_note(rows: list[dict]) -> list[str]:
    """
    How the resources and queues were sampled at the highest level reached, and a warning
    naming every level whose sampling fell behind or failed: the peaks above are only as
    good as the readings behind them.
    """
    last = (rows[-1] if rows else {}).get("sampler") or {}

    if not last:
        return []

    instances = sum(((rows[-1].get("final_status_counts") or {}).values()))
    busy = (f", {last['busy_skips']} readings missed behind a read still running"
            if last.get("busy_skips") else "")
    queues = (f"; queues every {last['queue_interval_s']} s, {last.get('queue_ticks')} "
              f"ticks" if last.get("queue_interval_s") else "; queues not sampled")
    out = [f"_Sampling at level {rows[-1].get('level')}: every {last.get('interval_s')} s, "
           f"{last.get('ticks')} ticks ({last.get('late_ticks')} skipped, slowest "
           f"{last.get('max_tick_s')} s), {last.get('reads')} reads "
           f"({last.get('failed_reads')} failed{busy}); {last.get('jobs_seen')} plugin-job "
           f"containers seen for {instances} instances{queues}_", ""]

    behind = [str(r.get("level")) for r in rows
              if any((r.get("sampler") or {}).get(key) for key in _SAMPLER_TROUBLE)]
    if behind:
        out += [f"**Sampling fell behind or failed at level(s) {', '.join(behind)}** "
                f"(skipped ticks, failed reads or listings, readings missed behind a "
                f"hung read, or failed queue reads in "
                f"`levels.jsonl`): their peaks may miss readings.", ""]
    return out


def _run_db_counters(db_counters: dict) -> list[str]:
    """
    Postgres over the whole run, cleanups included.
    """
    run = db_counters.get("run")

    if not run:
        return []
    return ["## Postgres over the whole run", "", f"_{_counters_summary(run)}_", ""]


def _db_counters_note(rows: list[dict]) -> list[str]:
    """
    Postgres at the highest level reached, per window: checkpoints, WAL, temp files and
    what the db container wrote.
    """
    last = rows[-1] if rows else {}
    windows = last.get("db_counters") or {}
    parts = [f"{window} {_counters_summary(windows[window])}"
             for window in ("workload", "cleanup") if windows.get(window)]

    if not parts:
        return []
    return [f"_Postgres at level {last.get('level')}: {'; '.join(parts)}_", ""]


def _counters_summary(delta: dict) -> str:
    """
    One line of counters. ``num_timed`` also counts the timed checkpoints an idle server
    skips, so the line leads with the checkpoints actually done.
    """
    if delta.get("reset"):
        parts = ["Postgres counters n/a (statistics reset in between, e.g. a db crash "
                 "restart)"]
    elif delta.get("unreadable"):
        parts = ["Postgres counters n/a (Postgres unreadable at one end of the window)"]
    else:
        parts = [f"checkpoints {delta.get('num_done', 0)} done "
                 f"({delta.get('num_timed', 0)} timed / "
                 f"{delta.get('num_requested', 0)} requested)",
                 f"WAL {_human_bytes(delta.get('wal_bytes'))} "
                 f"({delta.get('wal_fpi', 0):,} full-page images)",
                 f"temp files {_human_bytes(delta.get('temp_bytes'))}"]

    if "cgroup_write_bytes" in delta:
        parts.append(f"db container wrote {_human_bytes(delta['cgroup_write_bytes'])}")

    # its own item, since a level's maxima can come from different repeats
    if delta.get("wal_init_bytes"):
        parts.append(f"WAL segments zero-filled {_human_bytes(delta['wal_init_bytes'])}")

    if delta.get("cgroup_pswpout"):
        parts.append(f"swapped out {delta['cgroup_pswpout']:,} pages")

    if delta.get("cgroup_oom_kill"):
        parts.append(f"**{delta['cgroup_oom_kill']} OOM kill(s)**")
    return ", ".join(parts)


def _peak_cpu_note(rows: list[dict]) -> list[str]:
    """
    Highlight which service was hottest at the highest level reached. Ephemeral
    plugin-job containers are collapsed into one aggregate so the line stays readable.
    """
    last = rows[-1] if rows else {}
    peak = last.get("peak_cpu_pct", {})

    if not peak:
        return []
    
    hottest = max(peak.items(), key=lambda kv: kv[1])
    job = last.get("peak_job_cpu_pct")
    job_suffix = f"; plugin-jobs peak {job}%" if job else ""

    return [f"_Peak CPU at level {last.get('level')}: "
            f"{hottest[0]} {hottest[1]}% (per service: {peak}){job_suffix}_", ""]


def _peak_io_note(rows: list[dict]) -> list[str]:
    """
    Highlight the heaviest disk writer at the highest level reached — the USE
    saturation signal (e.g. db WAL or the storage volume). Block I/O is measured, not
    capped, so a disk-bound breaking point is visible here without an envelope limit.
    """
    last = rows[-1] if rows else {}
    writes = {k: v for k, v in (last.get("peak_write_bytes") or {}).items() if v}
    
    if not writes:
        return []
    
    hottest = max(writes.items(), key=lambda kv: kv[1])
    pretty = {k: _human_bytes(v) for k, v in writes.items()}
    
    return [f"_Peak disk write at level {last.get('level')}: "
            f"{hottest[0]} {_human_bytes(hottest[1])} (per service: {pretty})_", ""]


def _human_bytes(n: int) -> str:
    size = float(n or 0)

    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024

    return f"{size:.1f} TiB"


def record_digests(record: dict | None) -> list[str]:
    """
    The digests of an image record (``docker_client.image_record``), their ``sha256:…``
    parts only and sorted: on overlay2 a RepoDigest can carry another repository's name for
    the same image.
    """
    return sorted({d.rpartition("@")[2] for d in (record or {}).get("repo_digests") or []})


def _short_digests(image: dict | None) -> str | None:
    """
    An image record's digests, shortened (``None`` for an unrecorded image, so the table
    shows an em dash; "none" for an image without any, a local build on overlay2).
    """
    if not image:
        return None
    return ", ".join(d[:19] for d in record_digests(image)) or "none"


def _environment_section(env: dict) -> list[str]:
    host = env.get("host", {})
    envelope = env.get("envelope", {})
    noise = env.get("noise_floor", {})
    out = ["## Environment", ""]

    out += markdown_table(["Field", "Value"], [
        ["Host OS", host.get("operating_system")],
        ["Kernel", host.get("kernel_version")],
        ["Arch", host.get("architecture")],
        ["Host CPUs", host.get("ncpu")],
        ["Host memory (bytes)", host.get("mem_total_bytes")],
        ["Docker root", host.get("docker_root_dir")],
        ["Engine", env.get("docker", {}).get("engine_version")],
        ["API auth", _auth_cell(env.get("api_auth_mode"))],
        ["Noise floor (health p50/p95 ms)",
         f"{(noise or {}).get('health_p50_ms')} / {(noise or {}).get('health_p95_ms')}"],
        *([["Resource sampler", _sampler_cell(env["sampler"])]] if env.get("sampler")
          else []),
    ])

    out.append("")
    out += ["### Envelope (recorded knobs)", ""]
    out += markdown_table(["Knob", "Value"], [[k, v] for k, v in envelope.items()])
    out += _limits_section(env.get("limits") or {})
    out += _postgres_section(env.get("postgres") or {})

    plugins = env.get("workload_plugins", {})
    if plugins:
        out.append("")
        out += ["### Workload plugins", ""]
        out += markdown_table(
            ["Role", "Plugin", "Version", "Image", "Digest", "Installed versions"],
            [[role, p.get("name"), p.get("version"), p.get("dock_image"),
              _short_digests(p.get("image")), p.get("matches")]
             for role, p in plugins.items()])
        pin = env.get("workload_plugins_pin")
        out += ["", f"Pinned by `{pin['path']}` (sha256 {pin['sha256'][:12]})." if pin
                else "Not pinned: resolved by name."]

    out.append("")
    out += ["### Manual fields (fill in)", ""]
    
    for k, v in env.get("manual_fields", {}).items():
        out.append(f"- **{k}:** {v}")

    out.append("")
    return out


def _sampler_cell(sampler: dict) -> str:
    return describe_sampler(sampler)


def _limits_section(limits: dict) -> list[str]:
    """
    The resource limits each service container ran with; nothing for older runs.
    """
    if not limits:
        return []

    def size(value) -> str:
        return "unlimited" if value == -1 else _human_bytes(value) if value else "—"

    shm = "/dev/shm"  # nosec B108 - a column heading, not a path
    out = ["", "### Container limits (as applied)", ""]
    out += markdown_table(
        ["Service", "CPUs", "Memory", "Memory + swap", shm],
        [[service, values.get("cpus"), size(values.get("mem_limit")),
          size(values.get("memswap_limit")), size(values.get("shm_size"))]
         for service, values in sorted(limits.items())])
    return out


def _postgres_section(postgres: dict) -> list[str]:
    """
    The key settings the Postgres server ran with (``environment.json`` has all of
    them); nothing for runs that predate recording them.
    """
    settings = postgres.get("settings") or {}

    if not settings:
        return []

    names = sorted(postgres.get("key_settings") or settings, key=str.lower)
    out = ["", "### PostgreSQL (key settings; environment.json has all)", ""]
    out += markdown_table(["Setting", "Value"],
                          [[name, settings.get(name)] for name in names])

    if postgres.get("altered"):
        out += ["", "_Overridden by `ALTER SYSTEM` (postgresql.auto.conf): "
                    f"{', '.join(postgres['altered'])}_"]

    for table, options in sorted((postgres.get("table_options") or {}).items()):
        out += ["", f"_`{table}` storage parameters: {', '.join(options) or 'defaults'}_"]
    return out


# -- helpers (shared with ``compare``) ---------------------------------------------------

def markdown_table(headers: list[str], rows: list[list]) -> list[str]:
    """
    A GFM table as a list of lines; ``None`` cells render as an em dash, and pipes
    and newlines in cell values are escaped so they cannot break the table.
    """
    def cell(value) -> str:
        if value is None:
            return "—"
        return str(value).replace("\n", " ").replace("|", "\\|")

    out = ["| " + " | ".join(cell(h) for h in headers) + " |",
           "|" + "---|" * len(headers)]
    
    for row in rows:
        out.append("| " + " | ".join(cell(v) for v in row) + " |")
    return out


def csv_cell(value) -> str:
    """
    One RFC 4180 CSV cell; lists join with '; ', embedded quotes are doubled.
    """
    if isinstance(value, (list, tuple)):
        value = "; ".join(str(v) for v in value)

    text = "" if value is None else str(value)

    if "," in text or '"' in text or "\n" in text:
        return '"' + text.replace('"', '""') + '"'
    return text


def _auth_cell(mode) -> str:
    if mode == "token":
        return "token"
    
    if mode == "basic":
        return ("basic — **degraded run**: every measured latency includes CUBE's "
                "per-request password hashing")
    return str(mode)


def _group_by_axis(levels: Iterable[dict]) -> dict[tuple[str, str], list[dict]]:
    grouped: dict[tuple[str, str], list[dict]] = {}
    
    for lv in levels:
        grouped.setdefault((lv.get("topology"), lv.get("axis")), []).append(lv)
    
    for rows in grouped.values():
        rows.sort(key=lambda r: r.get("level", 0))
    return grouped


def main(argv: list[str] | None = None) -> int:
    """
    Re-render report.md + levels.csv from a results run directory (for `bench-report`).
    """
    ap = argparse.ArgumentParser(description="Re-render a benchmark report")
    ap.add_argument("run_dir", help="path to results/<run_id>")
    args = ap.parse_args(argv)

    run_dir = Path(args.run_dir)
    summary = json.loads((run_dir / "summary.json").read_text())
    levels_path = run_dir / "levels.jsonl"

    levels = [json.loads(line) for line in
              (levels_path.read_text().splitlines() if levels_path.exists() else [])
              if line.strip()]
    
    (run_dir / "report.md").write_text(render(summary, levels))
    (run_dir / "levels.csv").write_text(render_levels_csv(levels))
    
    print(f"rewrote {run_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
