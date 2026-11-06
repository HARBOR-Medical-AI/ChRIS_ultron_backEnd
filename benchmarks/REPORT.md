# CUBE Load & Scalability — Findings and Recommendations

**Date:** 2026-06-18  •  **For:** ChRIS_ultron_backEnd maintainers — scalability assessment and architecture roadmap

## 1. Executive Summary

This is a load & scalability assessment of **CUBE** (ChRIS Ultron Backend), run with the repo-native harness under `benchmarks/`: a data-plane DAG (directed acyclic graph) harness that escalates one factor at a time until CUBE breaks, plus a Locust control-plane (RED — rate, errors, duration percentiles) layer. All runs used `fslink` storage on a dedicated 16-core / 128 GB Linux box against a pinned, recorded envelope.

Key findings:

- **Status polling dominates orchestration latency, not real work.** For fast workflows, **~98% of end-to-end makespan is spent waiting for the next Celery-beat poll** (measured via a poll-interval sweep); for long-running jobs it drops to ~5%. This is the largest single architectural lever.
- **The control plane saturates at ~50 concurrent API clients (~117 req/s), and the wall is DB connection-pool exhaustion** — p95 pins at the 10 s pool-acquire timeout, then throughput collapses and 5xx/401 cascades begin.
- **The status-check Celery queue (`main2`) is the data-plane backpressure point** — it floods to hundreds of tasks at every breaking point while the submission queue (`main1`) stays near zero.
- **The database hot path is `core_chrisfile` registration/listing**, with statements reaching **35–66 s** under deep DAGs: an unindexed `fname LIKE` prefix scan, a per-path-component folder-insert storm, and a feed-row `FOR KEY SHARE` lock taken **~2M×** that serializes registration within a feed.
- **The layered-diamond topology is a scalability cliff** — makespan blows up super-linearly (42 s → ~3,400 s from 4 → 8 layers) with a **41–47 GiB** DB write, reproducible on a clean stack.
- **CUBE is robust to state aging** to ~1.9M file rows (feed-execution and create latency stay flat; list latency steps up once, then plateaus), and under `fslink` **file size is irrelevant** — file *count* is the stressor.

§1.1 lists the recommendations these findings drive; §2–§9 give the methodology and the root-cause analysis behind each.

**Corrections (2026-09-16).** Re-reading the raw run artifacts changed the attribution of the linear · depth and concurrent-feeds walls and four claims in §8.2; see §12. The issue set derived from this report uses the corrected attributions.

**Post-upgrade baseline (2026-09-17).** Every analysis the issue set cites was re-run after the upgrade to Django 6.0 and DRF 3.18, and §13 records what reproduced, what moved, and eight further corrections to §3, §4, §5 and §8.

**PostgreSQL configuration (2026-10-14).** Both compose stacks now run `db` on a committed `postgres/cube.conf` sized for the benchmark's 4 GiB `db`. On the layered diamond it removes every size-triggered checkpoint and cuts what `db` writes by 40%. §15 has the before and after runs; the after runs are the baselines from here on.

**Shared cache, broker v2.0.2, envelope, plugin pin and resource sampling (2026-10-27).** CUBE has a shared cache on a Dragonfly of its own, the broker runs Dragonfly v2.0.2, the benchmark pins the CPUs and memory of both and of the periodic worker and beat, registers the workload plugins from a committed, digest-pinned file, and samples every container each second. The old sampler read each container only about every 19 s, so the resource and queue peaks of every earlier run are lower bounds; §16 lists the claims that rested on them. Its runs are the baselines from here on.

### 1.1 Recommendations

Grouped by implementation cost; each cites the section with the supporting measurement and analysis.

**Quick wins — config/envelope, no architecture change**

- **Raise `worker-mains` concurrency and `db` CPUs.** At the feeds and layers walls `worker-mains` leads (219–406%, up to its 4-core limit) with `db` second; both are the levers that move those walls up (§3, §9).
- **Enlarge the DB connection budget.** The control-plane knee is pool exhaustion at ~40 connections; raise `CUBE_DB_POOL_MAX_SIZE` and `CUBE_DB_MAX_CONNECTIONS` (with `db` CPUs to back them) to push the ~50-user / 117-RPS (requests per second) ceiling (§4).
- **Index the filename prefix search.** A `text_pattern_ops` (or `varchar_pattern_ops`) index on `core_chrisfile.fname`, or rewriting the lookup to use the indexed `parent_folder_id` FK (foreign key) instead of `fname LIKE` (§8.2).
- **Keep authenticating once via token** — not per-request Basic auth; `auth-token` runs the password hasher at ~700 ms–1.7 s (§4).

**Architectural**

- **Replace status polling with event-driven status streaming from the compute cluster.** The primary change: `pfcon` pushes status (and log) transitions to CUBE instead of CUBE polling on a beat. Measured impact (§5, §8.1): **~10× makespan reduction for short/interactive workflows**, near-elimination of the `main2` flood, and removal of the poll interval as a scaling constraint. Long compute jobs gain only ~5%, so it is a latency/throughput win for interactive use and a stability win under concurrency.
- **Make registration concurrency-safe without serializing on the feed row.** The per-file-insert `FOR KEY SHARE` lock serializes intra-feed registration; restructure so concurrent instances in the same feed register in parallel (§8.2).
- **Bound the `pl-topologicalcopy` merge cost.** Stop re-reading and re-registering accumulated parent output at every layer, so layered-diamond cost grows linearly, not quadratically (§3.2).
- **Isolate failures between feeds** so one failed or timed-out feed cannot degrade the next (§9).

**Database**

- **Index the `core_chrisfile` hot path** — the `fname` prefix search above, plus verifying indexes cover the registration/listing `WHERE`/`JOIN` columns (`parent_folder_id`, `fname`) (§8.2).
- **Stop rewriting the whole `raw`/`summary` JSONB on every status update** — store large per-instance output metadata out-of-row or update incrementally; the 44 s/call UPDATE feeds the diamond-layers cliff (§8.2).
- **Batch and bound file/folder registration** — 233k `core_chrisfolder` inserts in one scenario points to a folder-model and batch-size review (§8.2).
- **Tune Postgres for the write-heavy registration path** — `db` reached 200%+ CPU and tens of GiB of WAL (write-ahead log) under deep DAGs; review WAL/checkpoint settings and `db` resourcing (§3.2, §8.2). The settings are done for the compose stacks (§15); `db` still saturates its 2 CPUs at 8 layers, and its resources are unchanged.

## 2. Test Environment & Methodology

### 2.1 Hardware and envelope

**Host:** dedicated bare-metal, Ubuntu 26.04 LTS, 16 logical CPUs, 128 GB RAM, Docker 27.3, NVMe storage. Running on metal (no Docker Desktop VM) so Compose `cpus`/`mem_limit` are honored.

The pinned, recorded envelope (production-like `uvicorn`, not the dev `runserver`):

| **Knob** | **Value** |
|---|---|
| `CUBE_UVICORN_WORKERS` | 4 |
| `CUBE_WORKER_MAINS_CONCURRENCY` (`celery -c`) | 4 |
| `PFCON_WORKERS` (`gunicorn -w`) | 8 |
| `CUBE_DB_POOL_MAX_SIZE` (per process) | 10 |
| `CUBE_DB_MAX_CONNECTIONS` | 300 |
| `db` CPUs / `chris` CPUs | 2 / 4 |
| `CUBE_CELERY_POLL_INTERVAL` | 2.0 s (swept 2/4/8) |
| `STORAGE_ENV` | `fslink` |

### 2.2 Two measurement surfaces

- **Data / orchestration plane** — a custom Python harness drives real plugin-instance DAGs (`dbg-bigfiles` → `pl-simpledsapp` → `pl-topologicalcopy`) end-to-end, measuring makespan, per-phase timing, USE (utilization, saturation, errors) resource samples, Celery queue depths, and per-scenario `pg_stat_statements`. It escalates one axis at a time (OFAT — one factor at a time) in binary steps — file count by decades — until a hard failure marks the breaking point.
- **Control plane** — a Locust layer (`locustfile.py`, reusing the `chris_api` Collection+JSON seam) drives concurrent API clients and reports RED.

### 2.3 Scope and validity

- **`fslink` only.** Inter-plugin "copy" is symlink + DB registration, so file **count** stresses the control plane while file **size** does not (confirmed, §7). Byte-copy / object-storage scaling is out of scope.
- **Isolation matters.** Several spurious failures early on were traced to running a scenario on a stack not yet recovered from a prior failure (e.g. a post-timeout cascade). All headline breaking points below were **confirmed on a clean stack**.

## 3. Concurrency & Breaking Points (data plane)

Each axis was escalated until a hard failure (any `finishedWithError`/`cancelled`, 5xx, timeout, or no-progress). Envelope as in §2.1, `CUBE_CELERY_POLL_INTERVAL=2`.

| **Topology · axis** | **Last PASS** | **Breaks at** | **Dominant resource at the wall** |
|---|---|---|---|
| linear · depth | 32 | **64** | `chris` 77%; cancellations + no-progress |
| linear · concurrent feeds | 32 | **64** | `worker-mains` 292%, `db` 107%, p95 991 ms |
| linear · file_count | 10,000 | not reached | `worker-mains` 103%, `db` 147 MiB write |
| fanout_fanin · branches | 64 | not reached | — |
| fanout_fanin · concurrent feeds | 8 | **16** | `worker-mains` 386%, `db` 72% |
| fanout_fanin · file_count | 10,000 | not reached | `worker-mains` 403%, `db` 204 MiB write |
| fanout_fanin · merges | 8 | not reached | trivial (`db` 16%) |
| diamond · branches | 32 | not reached | `db` 106% |
| diamond · layers | 4 | **16** (timeout) | see §3.2 — the cliff |
| diamond · concurrent feeds | 8 | **16** | `worker-mains`, `db` |

### 3.1 Concurrent-feeds capacity

A plain chain (`linear`) sustains **32 concurrent feeds** and breaks at 64. Both **fan-in** topologies break at half that or less (`fanout_fanin` and `diamond` feeds both wall at **16**) — the `ts` merge's "wait for all parents" dependency plus simultaneous branch submission costs more per feed. At every feeds wall the leading resource is `worker-mains` CPU (219–386%, up to its 4-core limit at the fanout wall) with `db` second — and at the diamond-feeds wall `db` (174%) nearly co-limits it (219%).

**Note:** `merges` is cheap — escalating the number of parallel merge instances 1→8 stayed all-PASS at trivial CPU, so the *merge count* is not a bottleneck; branches, feeds, and layers are.

### 3.2 The layered-diamond cliff

Repeated split/merge waves (`diamond · layers`) are pathological:

| **Layers** | **Makespan** | **Verdict** |
|---|---|---|
| 1 | 11.8 s | PASS |
| 2 | 19.9 s | PASS |
| 4 | 41.1 s | PASS |
| 8 | **3,363 s (~56 min)** | DEGRADED |
| 16 | timeout (>3,600 s), **~41 GiB `db` write** | **FAIL** |

Makespan explodes **super-linearly** (42 s → 3,363 s from 4 → 8 layers) and layer 16 drives a **41–47 GiB** Postgres write. Root cause in §8.2: the merge re-registers output that accumulates across layers, so the `core_chrisfile` work grows roughly quadratically with layer depth.

**Note (run provenance):** the makespan/verdict column blends two runs — the figures (3,363 s, ~41 GiB) are from the clean isolated re-run (`192652Z`), which classified layer 8 **PASS** (API p95 stayed under the 2 s SLO — service-level objective — on a quiet stack); the **DEGRADED** label is from the full sweep (`022252Z`, 3,458 s, p95 breached mid-sweep). The makespan blowup reproduces either way — that's the point of the re-run.

### 3.3 Long-running jobs scale *better* on the control plane

Re-running the feeds axes with `pl-simpledsapp --sleepLength 60` (long-active jobs) instead of instant jobs:

| **Topology · feeds** | **Instant jobs** | **60 s jobs** |
|---|---|---|
| linear | breaks at 64 | breaks at 64 |
| fanout_fanin | breaks at 16 | **breaks at 32** |

Long jobs spend their time *sleeping in compute containers* rather than hammering CUBE's control plane, so the concurrency wall holds or moves up. CUBE handles realistic long-running workloads more gracefully than the synthetic instant-job stress.

## 4. Control-Plane Behavior (RED)

Locust read-only saturation sweep, standard envelope:

| **Users** | **RPS** | **Fail %** | **p50** | **p95** | **p99** |
|---|---|---|---|---|---|
| 25 | 67 | 0% | 22 ms | 61 ms | 190 ms |
| **50** | **117** | 2.2% | 32 ms | 110 ms | 520 ms |
| 100 | 49 | 20.6% | 39 ms | **10,000 ms** | 17,000 ms |
| 200 | 81 | 16.9% | 580 ms | 11,000 ms | 14,000 ms |
| 400 | 41 | 66.4% | 8,000 ms | 22,000 ms | 61,000 ms |

**The knee is ~50 concurrent clients / ~117 RPS, and the wall is DB connection-pool exhaustion.** Beyond the knee, throughput *collapses* (117 → 49 RPS) while p95 pins at **exactly 10,000 ms** — the value of `CUBE_DB_POOL_TIMEOUT`. Requests block on psycopg pool acquisition and time out; failures are a 500 cascade, and `auth-token` itself begins 500-ing (cascading to 401s). The pool budget (10/process × 4 `uvicorn` = 40 connections) against 2 `db` CPUs is the constraint.

A clean write-path run (10 users, healthy stack, 0 failures) shows the write path is cheap and `auth-token` is the slowest endpoint:

| **Endpoint** | **p50** | **p95** |
|---|---|---|
| create-instance | 62 ms | 140 ms |
| list-feeds / list-instances | 51 / 72 ms | 110 / 150 ms |
| **auth-token** | **700 ms** | **1,700 ms** |

**Note:** `auth-token` is slow by design (password hasher). This independently validates the harness's "authenticate once, reuse a token" approach — per-request Basic auth would add ~1 s to *every* call.

## 5. Latency: the Polling Bottleneck

The plugin-instance lifecycle is advanced by Celery-beat tasks firing every `CUBE_CELERY_POLL_INTERVAL`. Sweeping that interval over {2, 4, 8} s on a `linear · depth` chain isolates the poller's share of makespan.

**Instant jobs (`sleepLength=0`):**

| **Depth** | **poll=2** | **poll=4** | **poll=8** | **poll8 / poll2** |
|---|---|---|---|---|
| 8 | 36.1 s | 69.5 s | 142.5 s | **3.94×** |

**Long jobs (`sleepLength=60`):**

| **Depth** | **poll=2** | **poll=4** | **poll=8** | **poll8 / poll2** |
|---|---|---|---|---|
| 8 | 516.9 s | 550.1 s | 591.2 s | **1.14×** |

Fitting `makespan = work + k · poll` at depth 8:

- **Instant jobs: work ≈ 0.7 s, i.e. ~98% of makespan is poll-wait.** Makespan tracks the poll interval almost perfectly.
- **60 s jobs: work ≈ 492 s, i.e. only ~5% is poll-wait.**

**Implication:** moving from polling to **event-driven status** would cut makespan by roughly **an order of magnitude for short / interactive workflows** and only marginally (~5%) for long compute jobs. This scopes the event-driven recommendation in §1.1.

## 6. State Aging

CUBE accumulates feeds/files over years. Repeated grow→probe cycles (a fixed 2-instance probe measured against a growing DB) took the file table from ~200 to **~1.9M rows**:

| **files_total** | **makespan p50** | **list p95** | **create p95** |
|---|---|---|---|
| 201 | 9.4 s | 215 ms* | 175 ms* |
| 56,201 | 9.4 s | 440 ms | 68 ms |
| 224,201 | 8.3 s | 432 ms | 55 ms |
| 1,344,201 | 9.4 s | 490 ms | 65 ms |
| 1,904,201 | 8.4 s | 487 ms | 70 ms |

*The 201-row baseline ran cold right after a restart; its high p95/create are warm-up artifacts.*

**CUBE shows no material aging degradation to ~1.9M files.** Feed-execution makespan and create latency are flat; list/query latency takes a one-time step from empty → populated, then **plateaus** (~440–490 ms across a 34× row increase). Registration throughput also held (heavy grows stayed ~155 s/feed past 1M files).

**Caveat:** this grows the *file* table specifically (the largest, and the one the probe's `count` queries hit). A `count()`/index knee at much larger scale (10M+) is not ruled out.

## 7. Storage / File-Size Sanity

Same `linear · depth` curve at 1 KiB vs 1 MiB files (a 1024× increase in bytes):

| **Depth** | **1 KiB** | **1 MiB** | **registered bytes (1 KiB → 1 MiB)** |
|---|---|---|---|
| 8 | 35.2 s | 36.3 s | 92 KB → 94 MB |

Makespan is **identical within noise** despite 1024× the bytes. Under `fslink`, file *size* does not touch the control plane — confirming the design decision to weight file **count** and treat size as a sanity check. Meaningful byte-copy scaling requires a non-`fslink` storage mode (future work).

## 8. Bottleneck Attribution

### 8.1 Queue backpressure — `main2` floods, `main1` does not

Peak Celery queue depths at the breaking points:

| **Scenario** | **main1 (submit)** | **main2 (status)** | **periodic** |
|---|---|---|---|
| linear feeds 64 (FAIL) | 0 | **576** | 0 |
| fanout feeds 16 (FAIL) | 0 | **306** | 0 |
| diamond layers 16 (FAIL) | 0 | **234** | 0 |

Across the entire sweep, peak `main1` was **12** and peak `main2` was **2,249**. The system breaks because **status-check tasks (`check_running_plugin_instances_exec_status`) are produced faster than the 4 worker slots consume them** — never because submission backs up. This is the data-plane signature of the polling problem (§5) and the most direct argument for event-driven status.

### 8.2 Database hot path — `core_chrisfile` registration & listing

Top `pg_stat_statements` entries (by total exec time) in the pathological scenarios:

| **Statement** | **Mean / call** | **Where** |
|---|---|---|
| `SELECT core_chrisfile.* …` | **35 s** | diamond layers 16 (68 calls = 2,371 s) |
| `UPDATE plugininstance SET summary=$1::jsonb, raw=$2 …` | **44 s** | diamond layers 8 (45 calls = 1,984 s) |
| `INSERT INTO core_chrisfile … UNNEST(...)` (bulk register) | 18–66 s | deep layers / high file_count |
| `SELECT COUNT(*) … WHERE fname::text LIKE $1` | 794 ms (×315) | listing (diamond layers 16) |
| `INSERT INTO core_chrisfolder …` | 0.2 ms × **233,123** | per-path-component folders |
| `SELECT … feeds_feed … FOR KEY SHARE OF x` | 0.0 ms × **1,981,408** | feed-row lock per file insert |

Four distinct problems are visible:

1. **Unbounded `core_chrisfile` SELECT/INSERT at deep DAGs** — the merge re-reads and re-registers output that accumulates across layers, so per-call cost reaches tens of seconds. This is the diamond-layers cliff (§3.2).
2. **`fname::text LIKE $1` prefix scans** — the `::text` cast + `LIKE` pattern likely defeats normal indexing; this is the list/aging cost.
3. **A `core_chrisfolder` row per path component** — 233k folder inserts in one scenario is heavy churn.
4. **`FOR KEY SHARE` on the feed row for every file insert** — taken ~2M× in the layers-16 cliff; cheap individually but it **serializes file registration within a feed** under concurrency.

## 9. Architecture Issues

1. **Poll-based scheduling dominates latency.** Beat polling makes makespan a function of the poll interval (~98% poll-wait for fast jobs, §5) and floods `main2` (§8.1). It is the root cause of both the orchestration latency and the data-plane breaking points.
2. **DB connection-pool exhaustion is the control-plane ceiling** (§4) — ~50 concurrent clients saturate the 40-connection budget against 2 `db` CPUs.
3. **The `core_chrisfile` registration path does not scale** — unbounded SELECT/INSERT under deep DAGs, unindexed prefix scans, folder-per-component churn, and per-file feed-row locking (§8.2).
4. **Plugin-instance `raw`/`summary` JSONB is rewritten wholesale** on status updates — O(output size) per update, reaching 44 s/call at depth (§8.2).
5. **The layered-diamond merge accumulates data quadratically** with layer count (§3.2).
6. **No isolation/recovery between failures** — a timed-out scenario left the stack degraded enough to fail the *next* one spuriously, until services were restarted.
7. **Async deletion of large feeds is slow** — multi-minute cleanup/quiesce of the deep-diamond feeds.
8. **`worker-mains` (`-c 4`) and `db` (2 CPUs) are the compute bottlenecks at the feeds and layers walls** — the linear-depth wall instead fails on cancellations/no-progress with `chris` CPU highest (§3).

## 10. Limitations & Future Work

- **`fslink` only** — byte-copy and object-storage (`swift`/`s3`) scaling, and meaningful file-size scaling, are not covered (§7).
- **Single envelope** — bottlenecks were identified but not yet confirmed by sweeping the levers (e.g. `worker-mains -c`, `db` CPUs, pool size); a confirmation sweep would quantify each fix's headroom.
- **Aging to ~1.9M files** — robust in range, but a `count()`/index knee at 10M+ rows is untested (§6).
- **No fault-injection** — error handling was characterized from observed failures (cancellations, pool timeouts, contamination cascades) rather than injected faults (worker kill, DB failover, `pfcon` outage).

## 11. Sources & Reproduction

**Harness & data.** All measurements come from `benchmarks/` (data-plane harness + `locustfile.py`); raw per-run artifacts are under `benchmarks/results/` (gitignored, uncommitted) — `report.md`, `levels.jsonl`, `scenarios/*/pg_stats.json`, `queue_depths.jsonl`, and Locust CSVs under `results/locust/`. The committed milestone subset — with the exact per-run commands, the clean-stack discipline, and the run-ID → finding map — is under [`benchmarks/history/`](history/README.md).

**Reproduce.** Environment and the pinned envelope are in §2.1 ([envelope.md](envelope.md) documents each knob). The core reproduction is two commands against the pinned `fslink` stack; the poll-interval sweep (§5), aging loop (§6), file-size sanity (§7), control-plane RED sweep (§4, `just bench-locust`), and the isolated clean-stack re-runs (§3.2 cliff, §3.3 long jobs) each take their own invocation, listed verbatim in `history/README.md`. Workload plugins were pinned to `dbg-bigfiles` 1.0.0, `pl-simpledsapp` 2.1.5, and `pl-topologicalcopy` 1.0.13; `just bench-start` now registers them from the committed pin, and the harness refuses to start unless the stack holds exactly one version of each, the pinned one ([STRATEGY.md](STRATEGY.md) § Workload Plugins).

```bash
just bench-start              # pinned uvicorn envelope + fslink + workload plugins
just bench-run --tier full    # authoritative OFAT sweep (§3 breaking points, §8 attribution)
```

Every headline figure traces to an archived run (`benchmarks/history/<run_id>/`; re-render with `just bench-report <run_id>`):

| Finding | Archived run(s) |
|---|---|
| §3 breaking points, §8 attribution | `022252Z` (full sweep) |
| §3.1 diamond-feeds wall | `153019Z` (isolated) |
| §3.2 layered-diamond cliff | `192652Z` (clean) + `022252Z` (sweep) |
| §3.3 long-running jobs | `155156Z` (linear), `162609Z` (fanout) |
| §4 control-plane RED | `history/locust/` CSVs |
| §5 poll-interval sweep | `224601/224933/225520Z` (instant), `234231/235952Z` + `001826Z` 06-17 (long) |
| §6 state aging | `165243Z` … `190409Z` |
| §7 file-size sanity | `230851Z` (1 KiB), `231046Z` (1 MiB) |

**Design references:** [STRATEGY.md](STRATEGY.md) (benchmark design rationale), [README.md](README.md) (usage), [envelope.md](envelope.md) (pinned envelope).

## 12. Corrections (2026-09-16)

Re-reading the raw per-scenario artifacts (`pg_stats.json`, `status_samples.jsonl`, `api_requests.jsonl`, the captured service logs) and probing the live schema changed the following. The text above is left as written; the issues derived from this report use these attributions.

- **§3, §9.1: the linear · depth wall (64) is not a load wall.** The scenario logged an HTTP 500 on instance creation, `DataError: value too long for type character varying(1024)`: `ChrisFolder.path` and `ChrisFile.fname` are `varchar(1024)` and each hop adds a `/<plugin>_<id>` path component. The cancellation of `ds40` and the cascade to its descendants are a registration that overflowed the same limit inside a bare `except Exception`, with no log line and no error code.
- **§3.1, §8.1, §9.1: the concurrent-feeds walls are duplicate job submissions, not pfcon or worker capacity.** The captured `pfcon` logs of the diamond · feeds failures (runs 022252Z and 153019Z) show `409 Conflict: The container name "/chris-jid-NNNN" is already in use`. The copy job's `finishedSuccessfully` handler moves the instance to `scheduled` with a plain `save()` and submits the app job with no compare-and-swap, so two overlapping status checks (the `main2` flood) both submit and the loser cancels the instance with `CODE01`. In linear · feeds = 64 all 64 roots and all 64 `ds0` instances ended `finishedSuccessfully`, yet `ds1`–`ds3` of two feeds were cancelled from `waiting`: their `ds0` parent was cancelled by one task and written back by another task's stale in-memory object. The `main2` flood is still the trigger, but the damage goes through non-idempotent handlers, which event-driven status alone would not fix.
- **§1.1, §8.2: the `fname` prefix index already exists.** Django creates a `varchar_pattern_ops` index (`core_chrisfile_fname_<hash>_like`) for the unique field, and the Django-escaped `startswith` queries use it (a 100k-row prefix count takes 41 ms on 2 M rows). The slow `COUNT(*) ... LIKE 'home/%'` is the `userfiles/` pagination counting the whole tree, and ordered pages are slow because a locale-collated b-tree cannot be range-restricted by a prefix; the fix is a code-point collation and cheaper counts, not another index.
- **§1.1, §8.2: `FOR KEY SHARE` does not serialize registration.** The ~2 M statements are PostgreSQL's per-row foreign-key checks on the inserted file and folder rows (`pg_stat_statements` mislabels the table); they cost 23 s of the 3,363 s run, and the lock is shared.
- **§1.1, §8.2, §9.4: the 44 s `UPDATE ... SET summary, raw` is not the per-poll status write.** It is the remote-cleanup write (`WHERE remote_cleanup_status = ...`), executed 44 times and blocked behind the single-transaction cascade deletion of the scenario's feed. The per-poll status write costs 0.2 ms.
- **§3.2, §9.5: the layered-diamond growth is geometric by construction.** Each merge collects all branches and each branch copies its input, so files multiply by `branches` per layer (1,747,610 files at 8 layers). No implementation makes it linear in layers; what CUBE controls is the cost per merged object and the number of passes over the data, and the pre-submission `get_previous_output_path` scan (13–32 s per call, up to 64% of the timed-out run) is the largest of those.

## 13. Post-upgrade baseline (2026-09-17)

The analyses above were re-run on commit 30fbbaf (Django 6.0.8, DRF 3.18.1, Celery 5.6.3, psycopg 3.3.5, PostgreSQL 18.2) on the same box and the same pinned envelope, one run per benchmark command the issue set uses. The runs are archived under [`history/`](history/README.md) as `2026-09-17T*` and the Locust CSVs under `history/locust_2026-09-17/`. Environment differences from June: Docker 27.3 → 29.8 and kernel 7.0.0-22 → 7.0.0-31; `pl-topologicalcopy` was held at 1.0.13 for these runs by a local, uncommitted edit to `chrisomatic/chrisomatic.yml`, because the ChRIS store now serves 2.0.0 (a no-op image) under the same name and `bench-compare` did not check workload plugin versions; the committed pin that has since replaced that edit is described in [envelope.md](envelope.md) § "Pinned workload plugin versions". A side script next to the harness kept each service's full log, `pg_stat_statements` with full query text, `pg_stat_activity` wait events, checkpointer/WAL counters and new-session counts; the numbers below that the harness does not record come from it. Those side artifacts stay on the benchmark box under `benchmarks/results/_campaign/` (gitignored, about 1 GB) and are deliberately not committed.

**Reproduced.** The poll sweep (depth 8: 35.6 / 69.9 / 142.1 s at 2 / 4 / 8 s, June 36.1 / 69.5 / 142.5; with 60 s jobs 516 / 550 / 587 s). The linear · depth wall at 64, same instance, same `DataError: value too long for type character varying(1024)`, same silent cancellation. The layered-diamond cliff (11.8 / 19.9 / 41.5 s at 1 / 2 / 4 layers, 3,260 s at 8, timeout at 16 with 41.9 GB written by `db`; the full sweep's three repeats at 8 layers: 3,364–3,419 s). Registration of a 10,000-file instance: 32.4 s. Remote-cleanup writes blocked behind the feed-deletion transaction (46 calls, 43.6 s mean, longest lock wait 727 s). State aging: probe makespan and create latency flat to 1.85 M files. The control-plane knee on a nearly empty database (~50 users).

**Changed.** With three repeats per level the concurrent-feeds walls came out at linear 128, fanout_fanin 32 and diamond 32, and the full sweep put them at 64, 64 and 32 (June: 64, 16, 16). The walls move because they are a race: pfcon logged duplicate-submission `409`s at every level from 16 feeds up, in runs that passed as well as in runs that failed. A single feed with 64 branches of 1,000 files cancels its merge in every run, which is the reliable reproducer. In the aging series `list p95` grew with the file table (42 ms at 56k files, 385 ms at 1.85 M) instead of stepping once and plateauing. On a database with 1.85 M files the read-only Locust sweep is saturated at 25 users (6 RPS, `userfiles/` 8.9 s p50).

**Corrections this baseline adds.**

- **§5: the poll-wait share is 75 / 88 / 94% at 2 / 4 / 8 s, not ~98%.** Measured per job from submit time, Docker's `FinishedAt` and CUBE's detection time: at 2 s the depth-8 chain spends 26.7 s waiting on polls, 7.4 s running containers and 1.2 s in CUBE. The "~98%" came from a straight-line fit through three makespans. Only two of the three transitions per hop scale with the interval; the scheduling wait stays near 0.9 s because `schedule_waiting_plugin_instances` runs on the same tick and its `inspect().active()` broadcast takes ~1 s.
- **§8.2: most of registration is a redundant listing loop.** `check_files_from_json_exist` lists the whole output tree 60 times on every successful registration, because the retry loop has no `break` once the files are present. Per instance the loop takes 27–34 s at 10,000 files and 267–342 s at 100,000, against 1.8–2.4 s and 19–25 s for everything after it (the sanitize walk, the per-file log line, the insert and the per-file `stat`), so it is 92–94% of each instance's `registeringFiles`, which averaged 32.4 s and 325 s in the targeted runs; in the last layer of diamond · layers = 8 it takes 1,193–1,202 s per branch. Registration is already linear in file count (3.2 ms per file at 10k and at 100k).
- **§8.2: the pre-submission scan runs twice per `ds` child**, once in the copy job and once in the app job (64 calls for 32 `ds` instances at layers 8, 1,008 s total; 2,357 s and 65% of the timed-out layers-16 run). Its `ORDER BY` costs 1,416 ms for 100k rows against 55 ms without it, and 1,402 ms with `work_mem` at 32 MB, so the cost is locale-aware comparison rather than the disk spill. On a synthetic 2 M-row table the ordered query took 2,097 ms with `en_US.utf8` and 65 ms with `pg_c_utf8`, both spilling the same 11 MB.
- **§8.2: the 148 file/folder `SELECT`s that return a whole feed are Django's deletion collector**, not registration.
- **§8.1: the worker log volume is the per-file registration line.** Of the 1.06 GB `worker-mains` wrote in diamond · layers = 8, 1,051 MB is `Registering file -->…` and 4.9 MB the pfcon response bodies. The benchmark settings also write the same lines to an unrotated `/tmp/debug.log` inside the container (916 MB in that scenario, 4.18 GB over the full sweep).
- **§4: `SELECT set_config` is Django's TimeZone, and the Celery workers reconnect per task.** The server default is `Etc/UTC`, Django wants `UTC`, so it sets it on every new connection; Celery's Django fixup closes the whole connection pool before and after every task in a prefork worker, so workers open 100–180 new sessions per second at the feeds walls (about 5 per second in a single-feed run).
- **§4: past the knee the API does not recover.** After a 100-user or 400-user read sweep, with no load, every request failed with a 500 after 10 s for at least three minutes while all 40 pooled connections sat idle on the server, until `chris` was restarted; 25 and 50 users left it healthy. June's sweep did not behave this way: its 200-user step, run right after the 100-user step, still served most requests, while September's served none that needed the database. The September results above 100 users therefore describe a broken stack, and the change may have come with the upgrade. Each sweep also begins with a login storm: `auth-token` takes 0.55 s alone, 1.6 s with 8 concurrent calls and 9 s with 16.
- **§3: a transient compute failure cancels instances; a pause does not.** Restarting pfcon (about 2 s) during a 16-feed fanout run cancelled 24 of 96 instances and stopping it for 30 s cancelled 30, because the copy job's three retries run back to back with no wait, and the app job's submission has no retry at all: it cancels with `CODE01` on the first failed request to pfcon. Pausing pfcon for 60 s cancelled nothing.
- **Envelope notes.** `CELERYD_PREFETCH_MULTIPLIER = 2` has no effect (Celery reads settings with the `CELERY` namespace), so `worker-mains` prefetches 16; `max_wal_size` at 1 GB forced 17 size-triggered checkpoints in the 68 minutes of diamond · layers = 8, next to 11 timed ones, not one every few seconds; the `core_chrisfile` pattern index is used by prefix counts (23 ms for 100k rows on a 1.95 M-row table); and the file browser's folder listing walks the `fname` index rather than the folder foreign key (523 ms for the first page of a 100k-file folder), which a code-point collation does not fix but an index on `(parent_folder_id, fname)` does (0.05 ms).

## 14. API pool leak after an overload (2026-10-06)

The API's failure to recover past the knee (§13) is a database connection leak in Django's error handling, triggered by the `DEBUG` setting the benchmark had inherited from `config.settings.local`. It did not come with the upgrade. The archived evidence is under [`history/locust_2026-10-06/`](history/locust_2026-10-06).

**Cause.** In the async request path Django renders an error response with `sync_to_async(response_for_exception, thread_sensitive=False)`, that is on the event loop's default executor rather than on the request's own thread (Django ticket #36027, fixed for Django 6.2, which is not released yet, and not backported). Under `DEBUG`, CUBE's `ResponseMiddleware` re-raises a view's exception, so each `PoolTimeout` of an overloaded step became a DEBUG 500 page rendered on an executor thread, and printing the QuerySets of its stack frames ran queries there. Django closes a request's connections when the request ends, on the request's thread; a connection opened on an executor thread is never closed, and executor threads live as long as the worker. Each worker's executor threads (20 on the 16-CPU box) took its 10 connections one at a time, and from then on every request that needed the database waited 10 s and failed while the server saw 40 idle connections. In a reproduction on a laptop (a 400-user step), a diagnostic that recorded the thread and call stack of every connection taken from the pool and not yet returned found all 40 held by executor threads, each taken in `django/views/debug.py` printing a QuerySet.

**Not the upgrade.** aeb4b94, built with the exact package versions of the pre-upgrade image (Django 5.2.9, asgiref 3.11.1, uvicorn 0.38.0, psycopg 3.3.4, psycopg-pool 3.3.1, Python 3.12.11), at the same envelope with `DEBUG` on, recovered after a 100-user step (250 RPS; 31 500s, 30 of them logins) and did not recover after a 400-user step: for all 18 probe rounds (about ten minutes, as each one waited out the timeouts) every request failed with a 500 after 10 s or got no answer, with 40 idle connections, as on 30fbbaf. The upgrade made the leak arrive sooner: September's single 100-user step (`recovery/`) served 10 RPS with 67% failures, and the sweep's 72.5%. In a directional A/B on a laptop with the same code, asgiref 3.12.1 instead of 3.11.1 alone took a 400-user step from 240 to 39 RPS, so more requests reach the pool timeout and render error pages; with 3.11.1 three of the four workers still lost all their connections. June's 200-user step probably served most requests because its earlier steps had not yet taken every connection, and its 400-user step probably left the stack broken too; nothing checked either then.

**Fix.**

- `core.middleware` backports Django's fix: error responses are rendered on the request's thread, so their connections are closed with the request's. It is installed from `Core.ready()` and does nothing from Django 6.2.
- The benchmark runs with `DEBUG` off (`CUBE_DEBUG`, default `false`), as production does. `DEBUG` also put every SQL statement through Django's query recorder and ran the debug toolbar's middleware on every request, so this moves every baseline (see below).
- `core.tests.test_middleware` serves concurrent failing requests under `DEBUG` through Django's ASGI handler and CUBE's middleware, and checks that the pool gets every connection back. Run on 30fbbaf with the pool-counter helper it uses added, it fails (two connections stranded); it passes with the fix. A second test drops clients while their requests hold, or wait for, a connection.

Separately, `psycopg-pool` went from 3.3.1 to 3.3.3 as maintenance: 3.3.3 stops a sync pool's worker threads from exiting after 24 hours without maintenance work (psycopg #1419). Django's pool queues such work every ten minutes, so CUBE is unlikely to have hit it, and it has nothing to do with the leak.

**Harness.** Each Locust step now records whether the API was healthy before it and whether it recovered after it, in `<csv prefix>_recovery.json` and two columns of the Locust report, and Locust exits with code 3 otherwise. The probe sends bursts of 16 concurrent requests on separate connections, which most likely reach every worker, where a single request can be answered by the one healthy worker; with `CUBE_DB_POOL_STATS_INTERVAL` set (5 s in the benchmark) it also requires every `uvicorn` worker to report a pool with no checked-out connection, which does make sure of all of them.

**Results.** The issue's single 100-user step, then probes every 10 s for three minutes: every request answered in 13–26 ms. The full read sweep on a fresh stack, without restarts between steps:

| Users | Requests | Failures | RPS | p50 / p95 / p99 (ms) | Recovered | 2026-09-17 fresh sweep: RPS, failures |
|---|---|---|---|---|---|---|
| 25 | 6,402 | 0 | 71 | 12 / 23 / 140 | yes, 0 s | 61, 0 |
| 50 | 11,874 | 41 | 132 | 10 / 26 / 470 | yes, 0 s | 97, 303 |
| 100 | 22,057 | 1,744 | 246 | 10 / 35 / 1,100 | yes, 0 s | 10, 678 |
| 200 | 37,342 | 5,147 | 416 | 34 / 190 / 3,000 | yes, 0 s | 27, 1,601 |
| 400 | 39,053 | 7,179 | 435 | 320 / 710 / 12,000 | yes, 0 s | 35, 2,115 |

After every step all four pools reported no checked-out connection, and three minutes of probes after the 400-user step answered in 11–23 ms. Of the 400-user step's failures, 311 are logins that failed with a 500 and 6,829 the 401s of the users those logins left without a token; 39 are other 500s. The login storm (§13) is now the first wall.

**Consequences.**

- The September Locust results from 100 users up measured a broken stack (§13). The control-plane knee of §4 (~50 users, p95 pinned at the 10 s pool timeout, in June as in September) is the leak's and `DEBUG`'s together, and no run separates the two: at 25 users, where nothing failed and so nothing leaked, p50 still fell from 29 to 12 ms. With both changed, throughput keeps rising to 400 users and most failures are logins.
- Turning `DEBUG` off moves every baseline. Runs up to 2026-09-17 had it on, and `bench-compare` reports `envelope.CUBE_DEBUG` as a caution against them. The two runs issue 1 ([#150](https://github.com/HARBOR-Medical-AI/HARBOR-planning/issues/150)) compares against were run again at the new envelope: `2026-10-07T002040Z` (linear · file_count, 1,731 s at 100,000 files against 1,766 s) and `2026-10-07T005703Z` (diamond · layers, 2,906 s at 8 layers against 3,260 s). `bench-compare` finds no regression against 041540Z and 072731Z, and the API p95 the harness measured during those runs was 28–51% lower at 9 of their 10 levels.
- Production runs with `DEBUG` off, where `ResponseMiddleware` turns a view's exception into its own 500 on the request's thread, so the September failure does not happen there in that form. One path remains: Django still logs error responses from the default executor, and with `ADMINS` set (production) the error email reports the request's user, which for a session-authenticated request queries the database on that thread. Django 6.2 does not change that call; it is left for later.

## 15. PostgreSQL configuration (2026-10-14)

Issue 1 ([#150](https://github.com/HARBOR-Medical-AI/HARBOR-planning/issues/150)) replaces the stock `postgres:18` configuration of both compose stacks with a committed `postgres/cube.conf`, sized for the benchmark's `db` limit (4 GiB, 2 CPUs), and adds migration `core.0004`, which vacuums and analyzes the file and folder tables sooner. [envelope.md](envelope.md) § "PostgreSQL configuration" lists each value and why; the main ones are `shared_buffers` 1GB, `work_mem` 32MB, `max_wal_size` 8GB, `checkpoint_timeout` 15min, `wal_compression` lz4 (the issue asked for `on`, which means pglz and costs far more CPU on a 2-CPU `db`) and `timezone` UTC. `db` also gets a 1 GB `/dev/shm`: at 32 MB `work_mem` a parallel hash join no longer fits in Docker's 64 MB default.

The harness now records what the server runs with and what it does. `environment.json` has every Postgres setting and each container's limits, and each scenario has deltas of checkpoints, WAL, full-page images, temp files, writes by process type and what the `db` container wrote, for its workload and its cleanup, with whole-run totals in `summary.json` ([envelope.md](envelope.md) § "What each run records about Postgres"). The figures below come from those counters; checkpoint causes come from the `db` log, and the WAL zero-fill of the before runs was read by hand from `pg_stat_io` at the end of each run.

**Protocol.** All runs were made in one session on the benchmark box, from 2026-10-13 23:38 to 2026-10-14 05:57 UTC, each on a fresh stack (`down -v`, plugin-job containers removed, `just bench-start`). The before runs are at 8ea010b: the stock configuration and the harness that records settings, limits and most counters, though not `wal_init_bytes` or the per-statement temp and WAL columns, which 2d52608 adds. The after runs are at 2d52608, the configuration. diamond · layers = 8 varied by about 10% between fresh-stack runs on 2026-09-17, enough to move a single pair's result, so it was run twice each way, alternating (before, after, before, after), and the criterion is judged on the means. All eight runs used `dbg-bigfiles` 1.0.0, `pl-simpledsapp` 2.1.5 and `pl-topologicalcopy` 1.0.13 (`workload_plugins` in `environment.json`); on a later commit `just bench-start` registers the same versions from the committed pin ([envelope.md](envelope.md) § "Pinned workload plugin versions").

| Command (`just bench-run --tier ...`) | Before | After |
|---|---|---|
| `smoke` | `2026-10-13T233911Z` | `2026-10-14T022103Z` |
| `full --topology linear --axis file_count --cap 100000 --repeat 1 --restart-on-fail` | `2026-10-13T234257Z` | `2026-10-14T022450Z` |
| `full --topology diamond --axis layers --cap 8 --repeat 1 --restart-on-fail` | `2026-10-14T001854Z`, `040048Z` | `2026-10-14T030059Z`, `045928Z` |

**Results.**

| Acceptance criterion | Before | After |
|---|---|---|
| Temp files during linear · file_count = 100000 (the 100,000-file level) | 166.2 MB, and 61.5 MB more in its cleanup | 0, and 0 in the whole run |
| Requested checkpoints during diamond · layers = 8, whole run | 19 and 21 (the `db` log names 18 and 21 as triggered by WAL size) | 0 and 0; the `db` log names only timed ones |
| What `db` wrote at diamond · layers = 8 (`peak_write_bytes.db`) | 30.47 and 32.34 GB, mean 31.41 GB | 18.91 and 18.80 GB, mean 18.85 GB: **−40.0%** (target −30%) |
| Effective settings in `environment.json` | — | in every run |
| `test-unit`, `test-integration`, `bench-run --tier smoke` | — | pass (see Tests) |

Against the earlier baselines of that command the drop is 35.1% (`2026-10-07T005703Z`, 29.05 GB, `DEBUG` off but run on a stack the linear run had used) and 42.4% (`2026-09-17T072731Z`, 32.74 GB, `DEBUG` on).

**Where the bytes went** (diamond · layers = 8, means of the two runs each way). The `pg_stat_io` rows count what Postgres handed to the kernel; a page rewritten before the kernel flushes it reaches the disk once, so these rows do not add up to what the container wrote.

| At 8 layers | Before | After |
|---|---|---|
| Table and index pages written by backends to free a buffer (`pg_stat_io`) | 22.96 GB | 5.53 GB |
| … written by the checkpointer and the background writer | 1.95 GB | 2.06 GB |
| WAL generated | 10.09 GB | 5.22 GB |
| Full-page images in it | 846k | 11.7k |
| Checkpoints written (`num_done`) | 21 | 1 |
| Checkpoint triggers counted, timed / requested (skipped checkpoints included) | 7 / 17 | 3 / 0 |
| Temp files | 1.54 GB | 1.07 GB |
| Zero-filling new WAL segments (see below) | at most about 1.1 GB | 4.04 GB |
| `db` wrote, net of that zero-fill | about 30.3 GB | 14.81 GB (about −51%) |
| Its cleanup: requested checkpoints; `db` wrote | 2.5; 3.88 GB | 0; 1.14 GB |
| Makespan | 2,898 s | 2,861 s |

Most of the saving is buffer evictions. With 128 MB of `shared_buffers`, registering layer 8's files filled the pool with dirty table and index pages, and backends had to write pages out to make room, many of them again and again as the next rows touched them. With 1 GB they mostly stay in memory until a checkpoint writes them once. The rest is the WAL: at `max_wal_size` 1GB a checkpoint completed every few minutes, 21 in the level, many in the middle of the large `INSERT … UNNEST` statements, and after each one every page those statements touched again went to the WAL in full. With one checkpoint completed during the level, the same work wrote 98.6% fewer full-page images, and lz4 compresses the ones that remain. Over the whole run, cleanups included, `db` wrote 43.9% less (35.87 → 20.12 GB).

**WAL zero-fill.** PostgreSQL writes zeros into every new 16 MB WAL segment before using it, and keeps old segments for reuse only up to about what recent checkpoints needed. At `max_wal_size` 1GB a fresh volume's pool stayed at 64 segments (1.16 and 1.11 GB zero-filled since the stack started); at 8GB it grew to 258 and 261 segments, 4.29 and 4.33 GB, of which 4.04 GB fell in the measured level each time. On a volume whose pool is already that large, the saving would be larger by about the zero-fill (−51% instead of −40%). How a larger, aged database changes the eviction saving was not measured: every run started from an empty database.

**Unchanged.** Every level that takes more than a minute moved by less than 2% (100,000 files: 1,719 → 1,724 s; 8 layers: 2,898 → 2,861 s), shorter ones by up to one 2 s poll interval either way, and `bench-compare` flags no makespan change. The time goes to registration, most of it outside Postgres: all the statements of the 100,000-file level take 90 s of its 1,724 s, and issue 4 ([#155](https://github.com/HARBOR-Medical-AI/HARBOR-planning/issues/155)) addresses the rest. The pre-submission scan of that output shows what `work_mem` buys and what it does not: its sort, which spilled before, now runs in memory and still takes 1.93 s per call instead of 2.10 s, because the cost is locale-aware comparison (issue 8b, [#160](https://github.com/HARBOR-Medical-AI/HARBOR-planning/issues/160)). API latency is within its noise: the worst p95 at 8 layers was 718 and 318 ms before, 353 and 355 ms after, and the latency flags `bench-compare` raised (one "regressed", three "improved") are each the p95 of a handful of requests. `db`'s memory use, page cache included, reached its 4.0 GiB limit in all four diamond runs, with no swap and no OOM kill; its CPU still peaks at its 2-CPU limit.

**What remains.** In diamond · layers = 8, cleanup included, three statements still spill to temp files, each because it handles hundreds of thousands of rows at once (from `pg_stats.json`, which now has each statement's temp and WAL bytes): Django's deletion collector (`parent_folder_id IN (…) ORDER BY fname DESC`, 354–657 MB over 148 calls), registration's bulk insert (1,638,400 rows in 10 statements, 644 MB; it also writes 4.39 of the 5.42 GB of WAL that the level and its cleanup wrote) and the pre-submission scan (`fname LIKE … ORDER BY fname DESC`, 422 MB over 64 calls). Issues 4 ([#155](https://github.com/HARBOR-Medical-AI/HARBOR-planning/issues/155)) and 6 ([#157](https://github.com/HARBOR-Medical-AI/HARBOR-planning/issues/157)) address all three. Until registration's inserts each finish in under a second, `log_min_duration_statement` logs them with every file path inlined, because Django binds parameters on the client: the `db` log grew to 816 MB in each diamond run on this configuration (135 KB before) and to 73 MB in the linear one.

**Tests.** On the final commit, 58285cf, on the benchmark box, both suites pass on the development stack with `cube.conf`: `just test-unit` (1,368 tests, 17 skipped, 1 expected failure) and `just test-integration` (77 tests, 34 skipped) on Docker, and the same two jobs as CI's podman job runs them, under rootless Podman 5.7 on Ubuntu 26.04 with the branch merged into `master` (same counts). Among them, `core.tests.test_models` checks that the server reads `cube.conf`, that its default time zone is the one Django connects with, and that migration `core.0004` applies and reverses. The smoke tier passes before and after, 10 levels each, with no level flagged.

**Consequences.**

- The after runs are the baselines for these three commands from here on; the epic's baseline table ([#148](https://github.com/HARBOR-Medical-AI/HARBOR-planning/issues/148)) is to be updated to point at them. Other commands get their own baseline run on this configuration when an issue needs one.
- `bench-compare` against any earlier run reports three cautions it did not before: `postgres` and `limits` "not recorded", and `envelope.CUBE_DB_SYNCHRONOUS_COMMIT` (shown as `—`, though those runs had PostgreSQL's default `on`). Against a run of this configuration, a differing setting is a caution of its own.
- The development stack runs on the same file, and CUBE's tests run on it (Tests above).

## 16. Shared cache, broker v2.0.2, envelope, plugin pin and resource sampling (2026-10-27)

Issue 1b ([#151](https://github.com/HARBOR-Medical-AI/HARBOR-planning/issues/151)) gives CUBE a shared cache: Django's `RedisCache` on a Dragonfly of its own, `chris-cache` (`--cache_mode`, no snapshot), apart from the Celery broker, and the helpers that the queue, event-driven, failure-handling, control-plane and token-cache issues build on (`core/cache.py`: locks, once-per-window markers and a bounded counter of expiring leases, all of which fail open when the cache does not answer, and a start-up check that refuses a wrong configuration). The broker moves to Dragonfly v2.0.2 behind a healthcheck and is renamed `chris-broker`, its volume `chris_broker_data`. The benchmark envelope pins the CPUs and memory of both Dragonflies and of `worker-periodic` and `celery-scheduler` ([envelope.md](envelope.md) § "Knobs (env vars, with defaults)"); `just bench-start` registers the workload plugins from the committed, digest-pinned `benchmarks/chrisomatic/benchmark.yml`, and the harness refuses to run against any other registration (envelope.md § "Pinned workload plugin versions"). And the harness's resource sampler, which read each container only about every 19 s, now reads every container each second. Nothing in CUBE calls the helpers yet, so the runs below check that none of this changes what the harness measures, re-measure what the old sampler could not, and give the four commands baselines.

**Why a second Dragonfly.** One Dragonfly has one memory limit and one eviction policy for all its logical databases. On a v2.0.2 instance capped at 256 MB, filling a cache database made the server refuse the broker's `LPUSH` in database 0 with `Out of memory`, and the eviction mode a cache wants (`--cache_mode`) would evict Celery's queues instead. With the cache apart, the broker never evicts and keeps its snapshot, while the cache evicts when full and starts empty after a restart, and the cache can be stopped while the broker, and every task, keeps running (below).

**During development.** A/B runs on the benchmark box on 2026-10-20/21, with the old sampler and not archived, moved no makespan beyond run-to-run variation: Dragonfly v1.38.0 against v2.0.2 (linear · depth = 16 took 67.69 and 67.79 s against 67.74 and 67.69 s at 16 deep), and the new limits at `0` against their defaults (67.81 and 68.78 s against 67.56 and 66.47 s). Their feeds and fanout walls moved between levels from run to run in both arms, each failure a `cancelled:inst-*` criterion in a run whose pfcon log had duplicate submissions, the race of the queue issue ([#152](https://github.com/HARBOR-Medical-AI/HARBOR-planning/issues/152)). Their resource figures, which sized the limits, are superseded below. Before the pin's runs, three disturbances were each refused with the problem named: a pin naming another build's digest, a pin registering a plugin a second time, and a second registered version of a plugin.

### 16.1 The resource sampler

The old sampler read the containers one after another with `container.stats(stream=False)`, which blocks 1-2 s while Docker primes its CPU figure, and read the queue depths once per pass. Each container, and the queues, were therefore read every 2 s times the number of containers, 19.1-19.8 s on the benchmark box, not every 2 s as STRATEGY.md said, in every run since the harness landed. Short scenarios never reached the containers Docker lists last (23 of the 189 level rows of the interim runs of 2026-10-20/22 lack a CPU peak for a running service), plugin-job CPU was effectively never measured (`peak_job_cpu_pct` above 0 in 2 of 606 level rows: the 417 archived before this section and those 189), `peak_write_bytes.db` was 0 in 149 of 234 rows in which the db container's cgroup counter shows writes (26 of the 48 archived ones and 123 of the interim runs' 186), and `stop()` gave up joining after 7 s with the thread still sampling, so late samples reached `docker_stats.jsonl` and each scenario's end waited up to 7 s. The interim runs are no longer in `history/`: 1be0d14 dropped them, so `git show 1be0d14^:benchmarks/history/<run>/levels.jsonl` reads them.

The new one lists the containers each second, start to start, without inspecting them (a new one is inspected once, for its start time), and reads each with a one-shot stats read, a few milliseconds, 8 at a time; a read still running after 0.8 of a tick counts as failed and is not sent again until it answers. Each container's CPU is its CPU time over the daemon's clock between two readings: the ~1 s window of Docker's own figure (a 2 s window would read a load busy 1 s and idle 1 s, the 2 s beat's shape, as a flat 50%), a plugin job's first reading its CPU since it started, and no figure for a window under 0.75 s (0.25 s since a start). The queue depths have a timer of their own, every 0.5 s. Each sample also records memory without the page cache, CPU throttling and the cumulative counters; `environment.json` records the sampler's settings, each level row and `report.md` how the sampling went, and `bench-compare` reports runs sampled differently as a caution (envelope.md § "Knobs (env vars, with defaults)", "Resource sampling"). It was calibrated on the benchmark box against loads whose CPU is known, run as plugin-job containers:

| Load (one CPU = 100%) | Mean | Peak | Other |
|---|---|---|---|
| A busy loop | 100.0% | 100.0% | |
| The same under `--cpus 0.5` | 50.0% | 50.2% | throttled 10.1 s in about 21 s |
| Busy 1 s, idle 1 s | 50.0% | 99.2% | |
| A 3 s job | | | 90.4% since its start over its first 0.83 s, then 100.0% and 100.0%; 2.74 CPU-seconds at its last reading |
| A 0.5 s job | | | not seen: no polling sampler sees a job shorter than a tick reliably |

### 16.2 Runs

**Protocol.** All runs were made on the benchmark box on 2026-10-27 from 00:07 to 01:10 UTC, an A/B by commit: arm A at cb15830, the old sampler, arm B at c575149, the new one; only `benchmarks/` differs, which `bench-run` rebuilds. Each command ran once per arm, linear · depth = 16 three times, successive pairs alternated which arm went first, and every run started on a fresh stack (`just bench-nuke`, `just bench-start`) with every envelope knob at its default. Outside the harness, each run recorded the CPU time of dockerd, containerd and the shims over its `bench-run` (including the benchmark image's rebuild after a checkout) and the benchmark container's own, from its cgroup. Host: kernel 7.0.0-38, Docker 29.9.2. Only arm B's runs are archived.

| Command (`just bench-run --tier ...`) | A, old sampler | B, new sampler (archived) |
|---|---|---|
| `smoke` | `2026-10-27T000735Z` | `001134Z` |
| `full --topology linear --axis depth --cap 16 --repeat 1 --restart-on-fail` | `001747Z`, `005259Z`, `010314Z` | `001433Z`, `005643Z`, `005959Z` |
| `full --topology linear --axis feeds --cap 128 --repeat 1 --restart-on-fail` | `002132Z` | `003342Z` |
| `full --topology fanout_fanin --axis feeds --cap 64 --repeat 1 --restart-on-fail` | `004859Z` | `004515Z` |

**Results.**

| | A, old sampler | B, new sampler |
|---|---|---|
| `smoke` | 10 PASS | 10 PASS, none flagged |
| linear · depth = 16, 16 deep | 68.63, 68.56, 68.61 s | 66.40, 68.57, 67.59 s |
| linear · feeds = 128 | PASS; 128 feeds 317.8 s | FAIL at 128 (`cancelled:inst-1143`); 323.1 s |
| fanout_fanin · feeds = 64 | FAIL at 32 (`cancelled:inst-351`, `-356`); 69.7 s | FAIL at 32 (`cancelled:inst-352`); 70.0 s |
| Engine CPU, share of one CPU: smoke; depth 16; feeds; fanout | 7.1%; 7.5-7.6%; 44.5%; 39.0% | 10.8%; 10.6-10.7%; 51.4%; 45.1% |
| The benchmark container's CPU | 0.9-2.1% | 2.4-4.1% |
| linear · depth = 16, the whole `bench-run` | 199-200 s | 170-172 s |

Makespans moved by no more than run-to-run variation; at the smoke levels by one 1 s poll either way. The feeds and fanout failures are the `cancelled:inst-*` criterion of every feeds wall since §13 and moved between levels in both arms during development; the old arm's fanout failure has pfcon's `409 … is already in use` in the 200 log lines the harness keeps, which for arm B's two do not reach back to the submissions. The new sampler costs the engine about 3% of a CPU with the stack's services (depth = 16) and 6-7% under the feeds and fanout loads, where the engine as a whole (dockerd, containerd and the shims of pfcon's job containers) already takes 39-45% with the old sampler, and the harness 1.5-2% more of its own, outside every container limit. Without the old sampler's 7 s waits at the end of each of its 5 scenarios, linear · depth = 16 runs 28 s shorter. Arm B sampled with no late tick, failed read, failed listing or failed queue read in any run; its slowest tick took 0.30 s (linear · feeds = 128). It saw 583 plugin-job containers for the 1,275 instances of linear · feeds = 128, 202 for the 378 of fanout_fanin · feeds = 64, 0-11 for the 36 of linear · depth = 16 and 11 for the 31 of the smoke run. These are containers, not instances: an instance can run a copy job as well (diamond · branches = 1 of the smoke run saw 6 for 3 instances), and a job that lives under a second is seen only when a tick falls in it.

**What the new sampler shows** (arm B; the highest figure over its six runs, memory with the page cache and, in brackets, without):

| Service | Limit | Highest CPU (one CPU = 100%) | Highest memory | Throttled at most |
|---|---|---|---|---|
| `chris-broker` | 2 CPUs, 1 GiB | 7.7% | 30 MiB (26 MiB) | 0 s |
| `chris-cache` | 1 CPU, 512 MiB | 4.4% | 15 MiB (11 MiB) | 0 s |
| `worker-periodic` | 1 CPU, 1 GiB | 30.0% | 256 MiB (247 MiB) | 0.17 s |
| `celery-scheduler` | 0.5 CPU, 512 MiB | 3.8% | 114 MiB (107 MiB) | 0 s |
| `worker-mains` | 4 CPUs, 4 GiB | 412.4% | 412 MiB (380 MiB) | 257.8 s (linear · feeds = 128) |
| `db` | 2 CPUs, 4 GiB | 201.8% | 353 MiB (141 MiB) | 54.8 s (linear · feeds = 128) |
| `chris` | 4 CPUs, 4 GiB | 390.4% | 503 MiB (490 MiB) | 0.41 s |
| `pfcon` | 2 CPUs, 2 GiB | 67.9% | 619 MiB (606 MiB) | 0 s |

- None of this issue's limits held its service back for long: `worker-periodic`'s 1 CPU throttled it in 10 of the 39 arm B level rows, for 0.17 s at most, and the other three not at all. Arm A, sampling sparsely, read `worker-periodic` at 16.7%, against the 30.0% above, and the broker and the cache at about what arm B reads. The headroom is for what the next issues add (markers per status check, #152; a lease per pfcon call, [#158](https://github.com/HARBOR-Medical-AI/HARBOR-planning/issues/158); token and count lookups per API request, [#159](https://github.com/HARBOR-Medical-AI/HARBOR-planning/issues/159), [#161](https://github.com/HARBOR-Medical-AI/HARBOR-planning/issues/161); the event streams on the broker, [#153](https://github.com/HARBOR-Medical-AI/HARBOR-planning/issues/153)), whose runs read these figures against the limits.
- `worker-mains` and `db` run into their CPU limits at the feeds walls, for the first time measured directly as throttling (`worker-mains` 14.8 s at 64 feeds and 33.6 s at 32 fanout_fanin feeds as well): the bottleneck §3 and §8 describe.
- Queues: `main1` peaked at 92 at 128 feeds and 107 at 32 fanout_fanin feeds (arm A read 7 and 40), so job submission does back up at the walls; `main2` peaked at 2,553 and 1,268 (2,414 and 1,039), with means of 1,861 and 617.
- At 64 and 128 feeds `chris` averaged 46.8% and 48.0% of a core.
- The means come from each scenario's `scenario.json`, which the archive leaves out: the runs' whole results directories are kept on the benchmark box (`benchmarks/results/<run>`).
- What `db` wrote by its blkio counter is 0.95-1.00 of what its cgroup counter says, at every level of every arm B run; arm A's was 0 at the short levels and 0.21-0.78 at the others.

**Without the cache** (arm B, a fresh stack): with `chris-cache` stopped, 50 token-authenticated GETs of the API root returned 200, as with it up; CUBE's services restarted while it was stopped started and answered, each uvicorn worker and each Celery service logging one start-up line (`CUBE shared cache: chris-cache:6379/0 does not answer …`: four from `chris`, one from each of the other three); a smoke run passed all 10 levels; and a process calling the helpers each second went ahead without the cache, logging one WARNING when it failed and one INFO when it answered again, 10.1 s later, counting the 22 calls that went ahead without it. This smoke run was started with `just bench-compose '--profile cube --profile bench run --rm --no-deps benchmark python -m benchmarks.run_bench --tier smoke'`, since `just bench-run` would have started the cache again.

**Tests.** On the benchmark box, on Docker: at cb15830's tree, `just test-unit` (1,437 tests, 17 skipped, 1 expected failure), `just test-integration` (77 tests, 34 skipped), `core.tests.test_cache` and `test_testrunner` (69 tests), and `just openapi` and its split variant with nothing on stderr that CI's check matches; c575149 changes only `benchmarks/` and CLAUDE.md, and at it `just bench-test` with the stack up (358 tests, the live ones included).

### 16.3 Corrections to earlier sections

The old sampler's peaks are maxima of a few 1 s windows taken about 19 s apart, so they are lower bounds: a claim that something reached a figure stands, one that something stayed under a figure, or a ranking of services by their peaks, does not.

- §1 and §8.1: "`main1` stays near zero" does not hold; `main1` reached 92 and 107 at the feeds walls above. "Never because submission backs up" rests on pfcon's logs and §12, not on the queue samples. `main2`'s hundreds, and §8.1's 2,249, are lower bounds.
- §1, §3, §8 and §9: the CPU peaks of `worker-mains`, `db` and `chris` (219-406%, 200%+, 77%, 107%, 147 MiB and 204 MiB of writes and the like) are lower bounds, and the rankings built on them ("leads", "second", "co-limits", "highest") are unsupported; the runs above measure `worker-mains` and `db` at their limits directly. §3's "trivial CPU" for merges was never measured.
- §15: what `db` wrote is better read from the cgroup counter of the same runs: 31.83 GB before and 18.89 GB after, −40.7% rather than −40.0%; against the earlier baselines' sampled 29.05 and 32.74 GB (§15), at least 35.0% and 42.3% less, as those are lower bounds; net of the WAL zero-fill, about 30.7 and 14.84 GB, about −52%. The sampled figures elsewhere (the 41-47 GiB at 16 layers of §13, the 29-33 GB at 8 of §15) are lower bounds.

**Consequences.**

- Arm B's runs are the baselines for these four commands from here on: `smoke` `2026-10-27T001134Z`, linear · depth = 16 `001433Z`, `005643Z` and `005959Z`, linear · feeds = 128 `003342Z` and fanout_fanin · feeds = 64 `004515Z`. Other commands get theirs when an issue needs one.
- Comparing a new run with an earlier baseline, `bench-compare` reports rows that come from this issue, not from a change in performance: against any of them, `sampler` "not recorded" and `envelope.CUBE_CACHE_URL` as cautions, the broker's and the cache's images as information, `workload_plugins.image` as a caution and the plugins' `dock_image` strings and the pin as information; against the 2026-10-13/14 runs, which recorded container limits, a caution for each of the new limits; against earlier runs `limits` once as not recorded. `host.mem_total_bytes` differs against every earlier campaign, a caution: by 4 KiB against 2026-10-06 and 8 KiB against 2026-10-13/14, but by 628 KiB against 2026-09-17 and 680 KiB against June. A trend of a sampled metric across them says which runs were sampled which way.
