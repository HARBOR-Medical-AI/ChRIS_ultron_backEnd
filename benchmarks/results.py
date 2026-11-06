"""
Results persistence — the single source of truth for the on-disk layout.

    results/<run_id>/
      environment.json
      summary.json
      report.md
      levels.jsonl, levels.csv
      api_requests.jsonl
      docker_stats.jsonl
      queue_depths.jsonl
      status_samples.jsonl
      scenarios/<scenario_id>/{scenario.json, pg_stats.json}
      scenarios/<scenario_id>/{failure.json, logs/}   # on the level's failing repeat only

Both the runner (writes) and ``report``/``compare`` (reads) go through this module so
the layout is defined in exactly one place. ``ResultsStore`` is the write side;
``load_run``/``find_runs`` are the read-only side (they never create directories) and
read archived runs under ``benchmarks/history/`` interchangeably with live results.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import RunData


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def _default(obj: Any):
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    
    if isinstance(obj, enum.Enum):
        return obj.value
    
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"not JSON-serializable: {type(obj)!r}")


def to_json(obj: Any, *, indent: int | None = 2) -> str:
    return json.dumps(obj, default=_default, indent=indent)


class ResultsStore:
    def __init__(self, root: str | Path, run_id: str | None = None):
        self.run_id = run_id or new_run_id()
        self.run_dir = Path(root) / self.run_id
        (self.run_dir / "scenarios").mkdir(parents=True, exist_ok=True)

    # -- top-level artifacts ---------------------------------------------------------

    def write_json(self, name: str, obj: Any) -> Path:
        path = self.run_dir / name
        path.write_text(to_json(obj))
        return path

    def write_text(self, name: str, text: str) -> Path:
        path = self.run_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def append_jsonl(self, name: str, rows: Iterable[Any]) -> None:
        path = self.run_dir / name
        with path.open("a") as fh:
            for row in rows:
                fh.write(to_json(row, indent=None) + "\n")

    def read_json(self, name: str) -> Any:
        return json.loads((self.run_dir / name).read_text())

    def read_jsonl(self, name: str) -> list[Any]:
        path = self.run_dir / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    # -- per-scenario ----------------------------------------------------------------

    def scenario_dir(self, scenario_id: str) -> Path:
        path = self.run_dir / "scenarios" / scenario_id
        path.mkdir(parents=True, exist_ok=True)
        return path


# -- roots & run discovery ---------------------------------------------------------------

def default_results_root() -> Path:
    """
    The live results root, honoring the ``BENCH_RESULTS_DIR`` override that the
    benchmark compose sets inside the container.
    """
    return Path(os.environ.get("BENCH_RESULTS_DIR",
                               str(Path(__file__).with_name("results"))))


def history_root() -> Path:
    """
    The committed archive of milestone runs (populated by ``just bench-archive``).
    """
    return Path(__file__).with_name("history")


def is_run_dir(path: str | Path) -> bool:
    """
    A run directory is any directory containing a ``summary.json``.
    """
    return (Path(path) / "summary.json").is_file()


def resolve_run(token: str, roots: tuple[Path, ...] | None = None) -> Path:
    """
    Resolve a run-directory path or a bare run id against the given roots
    (default: live results, then the committed history archive).
    """
    if roots is None:
        roots = (default_results_root(), history_root())

    candidates = [Path(token)] + [Path(root) / token for root in roots]
    
    for path in candidates:
        if is_run_dir(path):
            return path
        
    raise FileNotFoundError(f"run not found: {token!r} (looked in: "
                            f"{', '.join(str(c) for c in candidates)})")


# -- read-only loading ------------------------------------------------------------------

def load_run(run_dir: str | Path) -> RunData:
    """
    Load a persisted run. ``summary.json`` is required; environment and levels
    degrade to empty defaults so older or partial runs still load (the comparator
    reports their missing provenance as warnings rather than crashing).
    """
    path = Path(run_dir)
    summary_path = path / "summary.json"

    if not summary_path.is_file():
        raise FileNotFoundError(f"not a benchmark run (no summary.json): {path}")
    
    summary = json.loads(summary_path.read_text())

    env_path = path / "environment.json"
    environment = (json.loads(env_path.read_text()) if env_path.is_file()
                   else summary.get("environment") or {})

    levels_path = path / "levels.jsonl"
    levels = ([json.loads(line) for line in levels_path.read_text().splitlines()
               if line.strip()] if levels_path.is_file() else [])

    return RunData(run_id=summary.get("run_id") or path.name, path=path,
                   environment=environment, summary=summary, levels=levels)


def find_runs(*roots: str | Path) -> list[Path]:
    """
    Run directories (those containing a ``summary.json``) under the given roots,
    newest first — run ids are UTC timestamps, so name order is time order.
    """
    runs: list[Path] = []

    for root in roots:
        root = Path(root)

        if not root.is_dir():
            continue
        
        runs.extend(child for child in root.iterdir() if is_run_dir(child))
    return sorted(runs, key=lambda p: p.name, reverse=True)
