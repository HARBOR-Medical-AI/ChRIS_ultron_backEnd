# CUBE Load & Scalability Benchmark Strategy

**Date:** 2026-06-12  •  **For:** ChRIS_ultron_backEnd maintainers — load & scalability benchmark design

## 1. Purpose & Scope

This is the strategy for adding benchmarks and scalability-simulation scripts to ChRIS_ultron_backEnd (CUBE). The goal is to **find CUBE's breaking points under increasing load and document the exact failing criteria** across both of CUBE's performance surfaces (§4): on the **data plane**, file size, file count, concurrent feeds, plugin-instance linear depth, and branching/merging topology; on the **control plane**, API client concurrency.

**Scope (v1, implemented)** — the suite measures **both surfaces**, never mixing them:

- a **data-plane harness** — a custom Python engine driving real plugin-instance DAGs (directed acyclic graphs) end-to-end; the harder, ChRIS-specific part no off-the-shelf load tool can do (§7).
- a **control-plane load test** — a Locust layer driving concurrent API clients for RED (rate, errors, duration percentiles) metrics, on the shared `chris_api` seam (§4.1, §9.1).
- **`fslink` storage only**, with **authoritative runs on a dedicated Linux box** against a pinned resource envelope (§8, §13).

Deferred, with clean seams left in place: `swift`/`s3` storage and deep file-size scaling.

## 2. Decisions

| **Decision** | **Choice** | **Consequence** |
|---|---|---|
| **Goal** | Find the breaking point | Auto-escalating OFAT (one-factor-at-a-time) engine (§7), not a fixed Cartesian matrix |
| **Run target** | A dedicated Linux box | Compose `cpus`/`mem_limit` limits are meaningful; pin an envelope (§8). Laptop runs are directional only |
| **Performance surfaces** | Measure both, split by plane | Data plane = DAG harness (§7); control plane = Locust RED (§4.1). Built data-plane-first via the `chris_api` seam, but both are measured, never mixed (§4) |
| **Storage (v1)** | `fslink` only | Weight **file count**; treat **file size** as a 2-point sanity check (§3.3) |

## 3. How CUBE Schedules Work

Five codebase facts drive the design; ignoring any of them produces uninterpretable numbers.

### 3.1 Scheduling is poll-gated

The plugin-instance lifecycle is advanced by two Celery-beat tasks (`schedule_waiting_plugin_instances`, `check_running_plugin_instances_exec_status`), each firing every `CUBE_CELERY_POLL_INTERVAL` — **default 5.0 s** (`chris_backend/core/celery.py:56`). A linear chain of depth *N* with instant plugins is bounded below by ~*N × (a few × poll_interval)*, so for fast jobs the poller **dominates** makespan. The benchmark must decompose makespan by phase (§9.2) and sweep the poll interval (`{2s, 4s, 8s}`) to separate poller cost from CUBE's true capacity. **Floor: 2 s** — the periodic tasks' `skip_if_running` guard spends ~1 s in an `inspect().active()` broker broadcast per invocation, so at sub-2 s cadences consecutive invocations overlap and mutually skip, stalling the scheduler itself (measured: 44–116 s `waiting` stalls at 1 s).

### 3.2 Parallelism has fixed ceilings

| **Component** | **Setting** | **Meaning** |
|---|---|---|
| `worker-mains` | `celery worker -c 4 -Q main1,main2` | ≤ 4 concurrent submissions/status calls |
| `worker-periodic` | `celery worker -c 2 -Q periodic` | ≤ 2 concurrent periodic scans |
| `pfcon` | `gunicorn -w 8 -t 120` | ≤ 8 concurrent compute-side calls |
| `chris` | `manage.py runserver` | dev server — **must switch to `uvicorn`** (§3.4) |

"20 concurrent feeds" cannot mean 20 concurrent submissions with only 4 worker slots. These knobs *are* the scalability story and must be recorded as envelope parameters (§8).

### 3.3 `fslink` hides byte-copy cost

In `fslink` mode, inter-plugin "copying" is symlink creation plus DB registration — not byte copying. So file **count** stresses CUBE's control plane (DB rows, folder walks, registration loops) while file **size** barely touches it. v1 escalates file count hard (to 10k+) and keeps file size a 2-point sanity check; meaningful size-scaling needs a non-`fslink` mode.

### 3.4 `runserver` is the wrong system under test

`manage.py runserver` is a dev server and yields meaningless concurrency numbers. The CUBE image's default command is already `uvicorn` (production-like ASGI — Asynchronous Server Gateway Interface); the benchmark Compose override (§8) restores it. Normal `just` development is unchanged.

### 3.5 `ts` (merge) instances wait on all parents

A `ts` instance (`pl-topologicalcopy`) lists parent ids via `plugininstances`; CUBE validates the previous instance is included and all parents share the feed (`chris_backend/plugininstances/serializers.py:279`). The merge can't schedule until **every** parent reaches `finishedSuccessfully` — so fan-in makespan is gated by the slowest branch and the poller.

## 4. Two Performance Surfaces

CUBE has two distinct performance surfaces; mixing them yields uninterpretable numbers. Both are now implemented and reported **split by plane**: the **data plane** (the DAG harness) and the **control plane** (`locustfile.py`, run via `just bench-locust`), sharing the `chris_api` Collection+JSON adapter.

| | **Control plane** | **Data / orchestration plane** |
|---|---|---|
| **What** | API + DB: create, list, register | Celery + pfcon + Docker + storage: DAG execution |
| **Nature** | Synchronous request/response | Asynchronous, poll-gated |
| **Measure with** | RED — Rate, Errors, Duration percentiles | Makespan, per-phase decomposition, throughput |
| **Bottlenecks** | `uvicorn` workers, DB connections, registration | poll interval, worker `-c`, pfcon `-w`, Docker spin-up |
| **Failure modes** | 5xx, latency knee, DB pool exhaustion | stuck/cancelled/errored instances, makespan blowup |
| **Best tool** | Locust / k6 | Custom Python harness |

Both planes share `chris_api.py` (the Collection+JSON adapter): the data-plane engine (§7) uses it to build and poll DAGs; the control-plane load test (§4.1) uses its payload helpers while Locust owns request timing. The data plane was built first — `chris_api.py` was designed as a standalone importable seam so the control plane was a small `locustfile.py`, not a rewrite.

### 4.1 The control-plane load test (Locust)

`locustfile.py` (run via `just bench-locust`) drives concurrent API clients against the control-plane endpoints and reports RED through Locust's own statistics — the counterpart to the data-plane escalation engine (§7). Design points:

- **Read-dominant by default** — list feeds/instances/files + plugin search: the cleanest saturation signal, and it does not flood the compute side. The create/write path is opt-in (`BENCH_LOCUST_WRITE=1`) since it spawns real DAGs.
- **Saturation sweep** — drive increasing user counts (e.g. `25/50/100/200/400`), each to its own `--csv` prefix, to locate the latency knee and the DB connection-pool wall (p95 pinning at `CUBE_DB_POOL_TIMEOUT`).
- **Auth once via a DRF (Django REST Framework) token**; `host` from `CUBE_URL`. Locust owns request timing so RED percentiles are accurate; `chris_api`'s Collection+JSON helpers only shape payloads.
- **Outputs** — Locust `*_stats.csv` / `*_failures.csv` under `results/locust/`, rendered to a human-readable summary by `locust_report.py` (the control-plane peer of `report.py`; §9.1).

## 5. Workload Plugins

`just bench-start` registers all three through `chrisomatic` from the benchmark's own committed pin, `benchmarks/chrisomatic/benchmark.yml`, not from the dev environment's `chrisomatic/chrisomatic.yml`, which is not pinned and takes `pl-topologicalcopy` from the public ChRIS store (now a no-op 2.0.0 under that name).

| **Plugin** | **Type** | **Role** | **Key parameters** |
|---|---|---|---|
| **`dbg-bigfiles`** | `fs` | Root: generates random data | `--total`, `--size` → file count = `total / size` |
| **`pl-simpledsapp`** | `ds` | Downstream: streams input→output (8 MiB chunks) | `--sleepLength`, `--prefix` |
| **`pl-topologicalcopy`** | `ts` | Fan-in/merge: copies filtered parent output | `--plugininstances`, `--filter`, `--groupByInstance` |

**Versions (pinned).** Runs use `dbg-bigfiles` 1.0.0, `pl-simpledsapp` 2.1.5, and `pl-topologicalcopy` 1.0.13, each pinned by the index digest of the image the archived baselines ran: a digest is immutable, while a plugin's `latest` moves with every master commit and a version tag is a separate build. chrisomatic only registers what an image reports, so the harness reads the same pin and verifies it: it resolves each plugin **by name** (`get_plugin` → `plugins/search/?name_exact=`) and refuses to start unless each has **exactly one** registered version — the pinned one, under the pinned `dock_image`, with its image present where Docker can tell. Exactly one, rather than the pinned one among several, because CUBE orders versions as strings (`version` is a `CharField`, so `1.0.9` sorts above `1.0.13`) and itself resolves `pl-topologicalcopy` by name. Each run records the registrations (name, version, id, `dock_image`, match count), each image's id and digests, and the pin's path and sha256 (`workload_plugins`, `workload_plugins_pin` in `environment.json`); `bench-compare` treats another version as a blocker and other image digests as a caution. `envelope.md` § "Pinned workload plugin versions" covers changing a pin, the shared dev/benchmark volumes, and a laptop's own pin.

**Note:** `--sleepLength` produces two workload classes — storage/registration pressure at `0`, scheduling/long-active-job pressure at `>0`.

**Note on the merge:** the `pl-topologicalcopy` *container* is a no-op (it only logs) — CUBE itself assembles the merge output server-side (`plugininstances/services/pluginjobs.py`, `get_ts_plugin_instance_input_objs`). For a CUBE benchmark this is a feature, not a caveat: the fan-in topology measures pure CUBE-side merge cost (storage walks, symlink/registration under `fslink`) with zero plugin-compute noise. The harness runs it with `--groupByInstance` (the usual way), which gives each parent its own subdir in the merged output and prevents same-named files from sibling branches colliding. CUBE zips `--filter` regexes positionally with `--plugininstances` and copies everything for parents beyond the regex list, so a single `.*` covers all parents.

## 6. Topologies

Three topologies satisfy the issue's "three different topologies" requirement and exercise distinct scheduler paths.

### 6.1 Linear

```text
dbg-bigfiles -> simpledsapp -> ... -> simpledsapp   (depth N)
```

Sequential scheduling latency, file propagation, cumulative registration. Primary axis: **depth**.

### 6.2 Fan-out / Fan-in

```text
                  -> simpledsapp branch 1 -
dbg-bigfiles root -> simpledsapp branch 2 - -> pl-topologicalcopy (merge)
                  -> simpledsapp branch N -
```

Parallel child scheduling, simultaneous active instances, TS dependency waiting (§3.5). Primary axis: **branch count**.

### 6.3 Layered Diamond

```text
root -> fan-out -> merge -> fan-out -> merge   (repeated)
```

Repeated split/merge waves, graph-size effects. Primary axes: **branch count**, **layer count**.

## 7. The Escalation Engine

Because the goal is *find the breaking point*, the runner is an auto-escalating **OFAT** engine, not a Cartesian matrix: hold every dimension at a baseline, escalate one axis in binary steps until a hard failure, record the breaking point, recover, move on.

### 7.1 Algorithm

```python
for topology in (linear, fanout_fanin, diamond):
    for axis in axes_of(topology):
        level = 1
        while level <= cap:
            result = run(topology, axis=level, others=BASELINE, repeat=R)
            classify(result)                        # PASS | DEGRADED | FAIL
            record(metrics, phase_decomposition, docker_stats, result)
            if result is FAIL:
                record_breaking_point(topology, axis, level, criteria)
                recover()                           # reap containers, capture logs, health-check, restart
                break
            level *= step(axis)                     # ×2 (1, 2, 4, 8 ...); file_count ×10 (1, 10, 100 ...)
```

- **PASS** — keep escalating.
- **DEGRADED** — an SLO (service-level objective) breach (p95 over threshold, throughput collapse, makespan knee): record and **keep escalating**; the aim is the hard wall, noting where bending begins.
- **FAIL** (any 5xx, `finishedWithError`/`cancelled`, no-progress timeout, scenario timeout, DAG build error, service crash) — breaking point for the axis; recover and continue.

Treating a latency-threshold breach as a stop would halt escalation early and hide the real wall — hence DEGRADED ≠ FAIL. Repeats at a level short-circuit on the first FAIL: re-running an already-broken stack proves nothing and costs up to a scenario timeout each.

### 7.2 Axes and baseline

| **Topology** | **Escalated axes** | **Baseline (held)** |
|---|---|---|
| linear | depth, concurrent_feeds, file_count | size=1KiB, count=10, depth=4, feeds=1, poll=2s |
| fanout_fanin | branch_count, merge_count, concurrent_feeds, file_count | merges=1, others as above |
| diamond | branch_count, layer_count, concurrent_feeds | as above |

- **file_count** escalates hard — `1, 10, 100, 1000, 10000, …` — the real `fslink` control-plane stressor.
- **file_size** is a 2-point sanity check — `{1KiB, 1MiB}` — with the non-`fslink` caveat.
- **poll_interval** swept `{2s, 4s, 8s}` for at least the linear-depth axis to quantify the poller's contribution (2 s floor — see §3.1).

### 7.3 Combined-stress run

The "how many concurrent feeds before CUBE falls over, against envelope X" number is the linear `feeds` axis of the `full` tier. For a heavier per-feed DAG, run it with a larger baseline and a single repeat (each scenario is large, so one repeat per level; re-run the failing level by hand to confirm the wall):

`just bench-run --tier full --topology linear --axis feeds --cap 128 --repeat 1 --file-count 100`

## 8. The Pinned Resource Envelope

A breaking point is only reproducible relative to a fixed resource envelope. On a dedicated Linux box, Compose limits are real, so `docker-compose.benchmark.yml` pins a small, version-controlled envelope and restores the production-like `uvicorn` runtime.

```yaml
services:
  chris:
    command: python3 -m uvicorn --host 0.0.0.0 --port 8000 --workers ${CUBE_UVICORN_WORKERS:-4} config.asgi:application
    cpus: ${CUBE_CPUS:-4}
    mem_limit: ${CUBE_MEM_LIMIT:-4g}
    environment:
      CUBE_CELERY_POLL_INTERVAL: "${CUBE_CELERY_POLL_INTERVAL:-2.0}"
  worker-mains:
    command: celery -A core worker -c ${CUBE_WORKER_MAINS_CONCURRENCY:-4} -l info -Q main1,main2
    cpus: ${CUBE_WORKER_CPUS:-4}
    mem_limit: ${CUBE_WORKER_MEM_LIMIT:-4g}
  db:
    command: postgres -c config_file=/etc/postgresql/cube.conf -c max_connections=${CUBE_DB_MAX_CONNECTIONS:-300} -c synchronous_commit=${CUBE_DB_SYNCHRONOUS_COMMIT:-on}
    cpus: ${CUBE_DB_CPUS:-2}
    mem_limit: ${CUBE_DB_MEM_LIMIT:-4g}
  pfcon:
    command: gunicorn -b 0.0.0.0:30005 -w ${PFCON_WORKERS:-8} -t 120 pfcon.wsgi:application
    cpus: ${PFCON_CPUS:-2}
    mem_limit: ${PFCON_MEM_LIMIT:-2g}
```

(Excerpt — the full set, including the env-driven DB connection pool, is in `docker-compose.benchmark.yml` and documented knob-by-knob in `envelope.md`.)

Every report header pins the envelope and knobs: git commit + dirty status, CUBE/pfcon image ids, envelope limits, poll interval, worker `-c`, pfcon `-w`, `uvicorn` worker count, storage mode, API auth mode, host hardware (§13), and a measured noise floor.

**Note:** `cpus`/`mem_limit` are the portable controls. Block I/O is **measured** per container (reported as the bytes each one wrote in the scenario's window), but **not capped**: `blkio_config` is platform-sensitive (cgroup v2, real block devices, ignored on Docker Desktop) and stays out of v1 — a real disk cap belongs with the deferred non-`fslink` storage modes, where byte-copy is real.

## 9. Metrics & Reporting

Reported in standard vocabulary — RED for the control plane, USE (utilization, saturation, errors) for resources, makespan/phase for orchestration — and **split by plane**.

### 9.1 Control plane (RED)

requests/sec by endpoint class; error rate (4xx/5xx/timeouts); p50/p95/p99 latency for `create-instance`, `list-instances`, `list-feeds`, `register/upload`; the saturation curve (latency & throughput vs. concurrency) and its knee. Produced by the Locust layer (§4.1) and rendered by `locust_report.py` (saturation sweep + per-endpoint tables + top errors).

### 9.2 Orchestration (makespan / phase)

end-to-end makespan per feed; **per-phase decomposition** (`created → waiting → copying → scheduled → started → uploading → registeringFiles`, status set in `chris_backend/plugininstances/enums.py`; `copying`/`uploading` fire only when the compute environment requires copy/upload jobs — not under `fslink`/local — so the typical benchmark phase sequence omits them); instances completed/sec; max concurrent in-flight; Little's Law estimate (avg in-flight ≈ throughput × mean makespan); final-status counts. Phase timing is client-observed (CUBE keeps no transition history), so resolution equals the harness poll cadence.

### 9.3 Resources (USE) and backpressure

CPU/mem, block-I/O deltas and peak pids per container — `chris`, `worker-mains`, `worker-periodic`, `db`, the broker, the cache, `pfcon`, and plugin-job containers (label `org.chrisproject.miniChRIS=plugininstance`) — read every 1 s, start to start, via the Docker SDK (`docker_client.py` against the mounted socket): one-shot stats reads, a few milliseconds each and made concurrently, with each container's CPU over the second since its previous reading — the ~1 s window of Docker's own figure, short enough not to average away the 2 s Celery beat's bursts. A plugin job's first reading gives its CPU since it started. The **Celery queue depths** (`LLEN main1/main2/periodic`, `broker.py`) have a timer of their own, every 0.5 s, under the beat's period: a sustained rise on `main2` means status checks are produced faster than workers consume them — backpressure shows here before CPU graphs make it obvious. Report peak and mean, and how the sampling went (ticks, late ticks, failed reads, plugin-job containers seen) in `report.md` and the level rows; `environment.json` records the sampler's settings (`benchmarks/envelope.md` § "Knobs (env vars, with defaults)", "Resource sampling"). Runs without that record read each container in turn with a call that blocks 1–2 s, about every 2 s × the number of containers, so their peaks are lower bounds.

### 9.4 DB / state deltas and query attribution

Per scenario: feed/instance/file count deltas **and absolute totals** (the x-axis for state-aging analysis — the `aging-grow`/`aging-probe` tier pair), total registered output bytes, and a **`pg_stat_statements` snapshot** (reset at scenario start, every statement at the end, most total execution time first, `pg_stats.py`) that names the queries behind db load, and **Postgres counter deltas** for the workload and cleanup windows plus a whole-run total: checkpoints done, timed and requested, WAL and full-page images, temp files, bytes written or extended per process type, autovacuum runs, and the `db` container's disk writes, swap and OOM kills (`pg_stats.py`, `docker_client.py`). Folder deltas and connection-pool stats are future work.

### 9.5 The key artifact

The most valuable output is not pass/fail — it is the **approach-to-failure curve**: per-phase makespan and per-service CPU/mem/disk as the axis escalates, so each breaking point arrives with a *cause* ("DB CPU saturates at feeds=32" vs. "pfcon submit times out" vs. "worker queue backs up").

## 10. Failure Criteria & Recovery

**Hard failure (stops the axis, records the breaking point):** any instance ends `finishedWithError`/`cancelled`; any 5xx or request timeout; healthcheck timeout or any service unreachable/exited; no-progress timeout; scenario timeout; a DAG that could not be fully created.

"No progress" means no status transition *and no secondary evidence of work*: instances in CUBE-internal work states (`registeringFiles`/`uploading`/`copying`) or `started` with a live job container count as progressing — a single long phase (10k-file registration, a long `sleepLength`) must not read as a stall.

**SLO breach / DEGRADED (recorded, escalation continues):** p95 API latency over threshold.

Per-tier thresholds (`matrix.yml`; full-tier values shown):

```yaml
healthcheck_timeout_s: 30
api_request_timeout_s: 30
api_p95_latency_ms: 2000
no_progress_timeout_s: 300
scenario_timeout_s: 3600
quiesce_timeout_s: 900
```

**On failure:** capture recent service logs, reap plugin-job containers (the `reap-plugin-instances` pattern from `justfile`), health-check, optionally restart services (`--restart-on-fail`); continue with the next axis.

**Between every two scenarios (pass or fail):** delete the created feeds and wait for quiescence — global feed/instance counts back at the pre-scenario baseline (bounded by `quiesce_timeout_s`). Feed deletion is asynchronous in CUBE; without the gate its churn contaminates the next level's numbers.

## 11. Tiers

| **Tier** | **Runtime** | **Where** | **Purpose** |
|---|---|---|---|
| **smoke** | ~2 min | anywhere / CI | proves the harness works; gross-regression guard |
| **default** | ~15–30 min | laptop | directional; catches obvious regressions |
| **full** | hours/overnight | dedicated Linux box | authoritative breaking-point sweep for the ticket (incl. the combined-stress `feeds` run, §7.3) |
| **aging-grow / aging-probe** | ~10 min/cycle | dedicated Linux box | state-aging loop: accumulate rows (`--no-cleanup`), then measure a fixed probe |

Each point repeats `R` times (default 3, short-circuiting on FAIL); reports show median + min/max. Single runs are noise.

These tiers drive the **data plane** (OFAT escalation). The **control-plane** Locust load test (§4.1) is a separate mode run via `just bench-locust` — not a tier, since it sweeps client concurrency rather than the OFAT axes.

## 12. Layout & `just` Commands

```text
benchmarks/
  STRATEGY.md / README.md / envelope.md   # design, usage, pinned envelope
  matrix.yml           # tiers: baseline, axes + caps, thresholds, repeat
  models.py            # shared dataclass contracts between the layers
  config.py            # matrix loader + workload fingerprint (tier_fingerprint)
  units.py / topologies.py                # dbg-bigfiles sizing; DAG builders (pure)
  escalation.py / classifier.py           # OFAT engine; PASS/DEGRADED/FAIL policy (pure)
  metrics.py           # RED/USE/makespan rollups + thread-safe sink (pure)
  chris_api.py         # measured Collection+JSON adapter (token auth; the Locust seam)
  executor.py / poller.py                 # DAG instantiation; status polling + ProgressProbe
  docker_client.py / stats_sampler.py     # Docker SDK access; 1 s resource + 0.5 s queue sampler
  broker.py / pg_stats.py                 # LLEN queue depths; pg_stat_statements snapshots
  environment.py / recovery.py            # env manifest; logs/reap/health + quiescence
  results.py           # on-disk layout: store (write) + load_run/resolve_run (read)
  run_bench.py         # composition root + CLI
  report.py / compare.py                  # per-run report; cross-run comparison + trend
  locustfile.py / locust_report.py        # control-plane RED load test + its report renderer
  tests/               # unit tests for the pure core (no stack needed)
  results/  history/   # generated runs (gitignored); committed milestone archives
docker-compose.benchmark.yml
chris_backend/config/settings/benchmark.py
```

| **Command** | **Action** |
|---|---|
| `just bench-start` | start the `fslink` benchmark stack with `uvicorn` + the pinned envelope |
| `just bench-run --tier smoke\|default\|full\|aging-grow\|aging-probe` | run the escalation sweep |
| `just bench-report <run_id>` | re-render `report.md` from a results dir |
| `just bench-compare <run> <run> [...]` | diff runs over time; `--fail-on-regression` gates CI |
| `just bench-archive <run_id>` | keep a run's small artifacts in `history/` (committed) |
| `just bench-test` | run the harness unit tests in-container |
| `just bench-locust <args>` | control-plane RED load test (Locust) |
| `just bench-bash` | shell in the benchmark container (debugging) |
| `just bench-compose <args>` | low-level compose helper the other recipes wrap |
| `just bench-down` | stop benchmark services |

**Note on the client:** all measured calls go through `chris_api.py`, a thin Collection+JSON `requests` adapter — client libraries swallow latency and HTTP status codes, which the benchmark must capture. It authenticates once via a DRF token; per-request Basic auth would run CUBE's password hasher on every call (~100 ms each) and distort every number.

**Comparing runs over time:** every run records a workload fingerprint — a hash over what the harness *does to* CUBE (baseline, axes, steps, observation cadence) — separate from *how CUBE works inside* (commit, images, envelope). `bench-compare` uses it to label two runs comparable or not, then diffs makespans/p95 per level (flagged only past both a relative threshold and a per-metric noise floor) and breaking-point shifts per axis. Architecture changes (e.g. a future event-driven scheduler) keep old runs comparable by design: the workload hash is mechanism-agnostic, and the comparison is the instrument for quantifying the change.

## 13. Test Environment

Authoritative `full`-tier runs are performed on a **dedicated bare-metal Linux box** (no Docker Desktop VM layer so Compose `cpus`/`mem_limit` are honored directly). The box's full hardware is captured in each report header. Quick `smoke`/`default` tiers may run on a developer laptop and are treated as **directional only** — on Docker Desktop the VM's own CPU/mem/disk limits dominate per-container limits.

The runner auto-collects what it can and leaves explicit manual placeholders.

- **Automatic (`environment.json`):** host OS, kernel, architecture, logical CPU count, total memory, Docker engine/API versions and data root, CUBE/pfcon/db/broker/cache image ids, git commit + dirty status, storage mode, the full envelope knob set, each service container's applied CPU/memory/swap/`/dev/shm` limits, the Postgres server's settings, API auth mode, the workload-plugin registrations, their images and the pin they were checked against, the workload fingerprint, the resource sampler's settings, attribution availability (resource sampling, queue sampling, `pg_stat_statements`, Postgres counters), and a measured idle noise floor.
- **Manual (in the report):** storage device type/throughput, power/thermal mode, and other significant workloads running during the benchmark.

## 14. Implementation Phases

All phases (1–7) are implemented.

1. **Harness skeleton** — `benchmarks/` package, `chris_api.py` adapter (token auth + Collection+JSON), plugin lookup, environment collection, CLI scaffolding.
2. **Topology builders** — linear, fan-out/fan-in, layered diamond; record every feed/instance id; poll to success/failure/timeout.
3. **Escalation engine + metrics** — OFAT escalation, PASS/DEGRADED/FAIL classification, per-phase decomposition, resource sampling, failure detection + recovery, logs capture.
4. **Reporting** — `summary.json` + ticket-ready `report.md` with breaking-point table and approach-to-failure curves; hardware manifest with manual placeholders.
5. **Benchmark Compose + `just` commands** — `docker-compose.benchmark.yml` (uvicorn + pinned envelope) and the `bench-*` commands.
6. **Longitudinal comparison** — workload fingerprint, `bench-compare` (comparability, deltas, trend, CI gate), `bench-archive` + committed `history/`.
7. **Control plane (Layer 1)** — `locustfile.py` reusing `chris_api.py` for RED metrics, `locust_report.py` to render the summary, `just bench-locust` to run it.

## 15. Risks & Future Work

- **Matrix explosion** — avoided structurally by OFAT escalation; `--cap` and per-scenario timeouts bound runtime.
- **Polling cadence skews makespan** — record `CUBE_CELERY_POLL_INTERVAL`, sweep `{2s, 4s, 8s}` (never below 2 s, §3.1), decompose makespan by phase.
- **Laptop noise** — authoritative runs on the Linux box; laptop tiers labelled directional; repeat `R×` and report median + spread.
- **`fslink` under-reports byte-copy cost** — weight file count; flag `swift`/`s3` as future work for meaningful size-scaling.
- **`pl-topologicalcopy` merge compute** — TS path stresses CUBE dependency tracking, not plugin-side merge compute; noted in the report.
- **In-repo results grow large** — commit only `.gitkeep`; gitignore generated run directories.
- **Future:** add `s3`/`swift` storage runs; add a realistic registered pipeline as a fourth topology; define `small`/`medium`/`large` envelopes.

## 16. Sources

**Codebase:**

- `chris_backend/core/celery.py:56` — `CUBE_CELERY_POLL_INTERVAL` default and beat schedule.
- `chris_backend/plugininstances/tasks.py` — `skip_if_running` guard (the 2 s poll-interval floor, §3.1).
- `chris_backend/plugininstances/enums.py` — plugin-instance status set.
- `chris_backend/plugininstances/serializers.py:279` — `ts` `plugininstances` parent validation.
- `docker-compose.yml` — worker `-c`, pfcon `-w`, `runserver`, `fslink`, `JOB_LABELS`.
- `benchmarks/chrisomatic/benchmark.yml` — the pinned workload plugin registration (`just bench-start`); `chrisomatic/chrisomatic.yml` is the dev environment's.

**Docker Compose resource controls:**

- [Compose service attributes](https://docs.docker.com/reference/compose-file/services/)
- [Compose `cpus`](https://docs.docker.com/reference/compose-file/services/#cpus)
- [Compose `mem_limit`](https://docs.docker.com/reference/compose-file/services/#mem_limit)
- [Compose `blkio_config`](https://docs.docker.com/reference/compose-file/services/#blkio_config)
- [Compose stats](https://docs.docker.com/reference/cli/docker/compose/stats/)

**Load-testing tools (control-plane layer):**

- [Locust](https://docs.locust.io/en/stable/what-is-locust.html)
- [Grafana k6](https://grafana.com/docs/k6/latest/)
