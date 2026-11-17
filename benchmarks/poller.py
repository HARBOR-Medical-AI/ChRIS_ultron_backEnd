"""
Poll a scenario's feeds to completion and observe per-instance phase timing.

CUBE keeps no status-transition history, so the poller records the wall-clock time each
status is *first seen* (resolution = poll cadence). It returns a :class:`PollResult` of
facts only — the PASS/DEGRADED/FAIL decision is made later by ``classifier``.

``no_progress`` means "no evidence of forward motion within the window". Status
transitions are the primary evidence; an optional injected *progress probe* supplies
secondary evidence for long single-phase work that would otherwise trip the window
(see :class:`ProgressProbe`).
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Callable, Optional

from .chris_api import ChrisApi
from .metrics import terminal_ts
from .models import (ACTIVE_STATUSES, HARD_FAILURE_STATUSES, InstanceRef, PollResult,
                     StatusTimeline)


class ProgressProbe:
    """
    Secondary evidence that a scenario is progressing when no status transition has
    been seen — a single long phase (e.g. ``registeringFiles`` for 10k files, or a
    ``sleepLength`` longer than the window) must not be misread as a stall.

    * ``registeringFiles`` / ``uploading`` / ``copying`` — CUBE-internal work states
      with no API-visible increment (output files land in one ``bulk_create``), so
      their mere presence counts as progress. Truly stuck instances are still bounded:
      the scenario timeout fires, and CUBE's own watchdog cancels instances stuck in
      ``registeringFiles`` (-> ``cancelled`` -> hard failure).
    * ``started`` — counts as progress only while a plugin-job container is actually
      running; if Docker is not observable we stay lenient and count it.
    * Queue states (``created``/``waiting``/``scheduled``) are deliberately *not*
      evidence: nothing leaving them is exactly the scheduler stall ``no_progress``
      exists to catch.
    """

    WORK_STATUSES = frozenset({"registeringFiles", "uploading", "copying"})

    def __init__(self, docker=None):
        self._docker = docker

    def __call__(self, statuses: dict[int, str]) -> bool:
        vals = set(statuses.values())

        if vals & self.WORK_STATUSES:
            return True
        
        if "started" in vals:
            if self._docker is None or not self._docker.available:
                return True                      # cannot verify; scenario timeout bounds
            
            try:
                return bool(self._docker.job_containers())
            except Exception:                    # noqa: BLE001 - best-effort evidence
                return True
        
        return False


def poll_to_completion(api: ChrisApi, feed_ids: list[int], instances: list[InstanceRef],
                       *, poll_every: float, scenario_timeout: float,
                       no_progress_timeout: float,
                       progress_probe: Optional[Callable[[dict[int, str]], bool]] = None,
                       ) -> PollResult:
    if not feed_ids:
        # nothing was built (e.g. every feed's creation failed) — don't burn the
        # no-progress timeout; the build error is surfaced by the runner.
        return PollResult(timelines=[], makespan_s=None, terminal=True,
                          hard_failures=(), no_progress=False, timed_out=False,
                          final_status_counts={})

    timelines: dict[int, StatusTimeline] = {
        ref.instance_id: StatusTimeline(ref.instance_id, ref.node_key, ref.role,
                                        feed_id=ref.feed_id, parent_ids=ref.parent_ids)
        for ref in instances
    }
    expected = len(timelines)

    start = last_progress = time.monotonic()
    terminal = timed_out = no_progress = False

    while True:
        observed = _scan(api, feed_ids)
        statuses = {iid: status for iid, (status, *_) in observed.items()}

        if _record(observed, timelines):
            last_progress = time.monotonic()
        elif progress_probe is not None and progress_probe(statuses):
            last_progress = time.monotonic()

        now = time.monotonic()
        active = any(s in ACTIVE_STATUSES for s in statuses.values())
        all_present = len(statuses) >= expected

        if statuses and all_present and not active:
            terminal = True
            break
        if now - start > scenario_timeout:
            timed_out = True
            break
        if now - last_progress > no_progress_timeout:
            no_progress = True
            break
        time.sleep(poll_every)

    tls = list(timelines.values())

    return PollResult(
        timelines=tls,
        makespan_s=_overall_makespan(tls),
        terminal=terminal,
        hard_failures=tuple(
            f"{tl.final_status}:inst-{tl.instance_id}"
            for tl in tls if tl.final_status in HARD_FAILURE_STATUSES
        ),
        no_progress=no_progress,
        timed_out=timed_out,
        final_status_counts=dict(Counter(
            tl.final_status for tl in tls if tl.final_status)),
    )


def _scan(api: ChrisApi, feed_ids: list[int]) -> dict[int, tuple[str, str, bool]]:
    """
    Each instance of the feeds with its status, its error code (blank for none), and
    whether a pfcon response is recorded for it (its ``raw``): a job of its went out.
    """
    observed: dict[int, tuple[str, str, bool]] = {}

    for fid in feed_ids:
        for item in api.get_feed_instances(fid):
            try:
                observed[int(item["id"])] = (item.get("status") or "",
                                             item.get("error_code") or "",
                                             bool(item.get("raw")))
            except (KeyError, ValueError, TypeError):
                continue
    return observed


def _record(observed: dict[int, tuple[str, str, bool]],
            timelines: dict[int, StatusTimeline]) -> bool:
    """
    Apply a status scan to the timelines; return True if anything advanced.
    """
    now = time.time()
    progressed = False

    for iid, (status, error_code, submitted) in observed.items():
        tl = timelines.get(iid)

        if tl is None:  # unexpected instance — track it anyway
            tl = StatusTimeline(iid, "?", None)
            timelines[iid] = tl

        if status and status not in tl.first_seen:
            progressed = True

        tl.observe(status, now, error_code, submitted)
    return progressed


def _overall_makespan(timelines: list[StatusTimeline]):
    starts = [min(tl.first_seen.values()) for tl in timelines if tl.first_seen]
    ends = [terminal_ts(tl) for tl in timelines]
    ends = [e for e in ends if e is not None]
    
    if not starts or not ends:
        return None
    return max(ends) - min(starts)
