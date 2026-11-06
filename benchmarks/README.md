# CUBE Load & Scalability Benchmark

A repo-native benchmark suite for CUBE. It measures **both of CUBE's performance surfaces** —
the asynchronous **data plane** (orchestration) and the synchronous **control plane** (the
API) — each with the right tool and metrics:

- a **data plane** harness that drives real plugin-instance DAGs (directed acyclic graphs) end-to-end and
  **auto-escalates one factor at a time until CUBE breaks** (Celery + pfcon + Docker +
  `fslink` storage), writing a ticket-ready breaking-point report;
- a **control plane** Locust load test (`locustfile.py`, via `just bench-locust`) that
  drives concurrent API clients and reports **RED** (rate, errors, duration percentiles):
  the API saturation knee and DB connection-pool limits (see [§ Control plane](#control-plane-api-saturation-with-locust-red)).

`chris_api.py` is the shared Collection+JSON adapter used by both. See [STRATEGY.md](STRATEGY.md)
for the design rationale and [envelope.md](envelope.md) for the pinned resource envelope.

**Findings:** the full analysis and recommendations are in [REPORT.md](REPORT.md);
[history/README.md](history/README.md) indexes the committed milestone runs and the exact
commands used to generate each result.

## Quick start

```bash
just bench-start                 # bring up the fslink compose stack, uvicorn envelope + pinned plugins
just bench-run --tier smoke      # ~2 min sanity sweep
just bench-run --tier default    # ~15-30 min directional sweep (laptop)
just bench-run --tier full       # overnight breaking-point sweep on a dedicated Linux box
just bench-report <run_id>       # re-render report.md from a results dir
just bench-compare <a> <b> [...] # diff runs over time (breaking points, makespan, p95)
just bench-archive <run_id>      # keep a run's small artifacts in version control
just bench-test                  # run the harness unit tests in-container
just bench-locust '-u 100 -t 3m' # control-plane RED load test (Locust); CSVs -> results/locust/
just bench-down                  # stop the stack
just bench-nuke                  # stop it and remove its data (the dev stack's too: shared)
just bench-bash                  # shell in the benchmark container (debugging)
just bench-compose <args>        # low-level compose helper the other bench-* recipes wrap
```

The **combined-stress number** — how many concurrent feeds before CUBE breaks —
is the linear `feeds` axis of the `full` tier. For more files per feed (same DAG) and a single
repeat (each scenario is large), run it directly:

```bash
just bench-run --tier full --topology linear --axis feeds --cap 128 --repeat 1 --file-count 100
```

Useful flags (`just bench-run -- <flags>`):

| Flag | Effect |
|---|---|
| `--tier smoke\|default\|full\|aging-grow\|aging-probe` | matrix tier (see `matrix.yml`) |
| `--topology linear\|fanout_fanin\|diamond` | restrict to one topology |
| `--axis depth\|branches\|layers\|merges\|feeds\|file_count` | restrict to one escalation axis |
| `--cap N` | override every axis cap |
| `--repeat N` | override repeats per level |
| `--file-size 1MiB` | override the baseline file size (the 2-point size sanity check) |
| `--file-count N` | override the baseline file count (held while escalating another axis) |
| `--sleep-length 5` | override simpledsapp sleepLength in whole seconds (long-active-job workload class) |
| `--no-cleanup` | keep created feeds (study cumulative-growth effects; skips the quiesce gate) |
| `--restart-on-fail` | restart services if health doesn't recover after a failure |

## Data plane: the escalation engine

For each topology and each escalation axis, the runner holds every other dimension at the
tier baseline and escalates the axis `1, 2, 4, 8 …` (`file_count` by decades:
`1, 10, 100 …`) up to a cap, repeating `R` times per level, until a **hard failure**
marks the breaking point:

- **PASS** — keep escalating.
- **DEGRADED** — an SLO (service-level objective) breach (e.g. p95 latency over threshold); recorded, escalation
  continues toward the real wall.
- **FAIL** — a hard failure (any `finishedWithError`/`cancelled` instance, any 5xx/timeout,
  no-progress, scenario timeout, or a build error); records the breaking point, recovers
  (reap job containers, capture logs, health-check), and moves to the next axis. Remaining
  repeats at a failing level are skipped — re-running an already-broken stack proves
  nothing and costs up to a scenario-timeout each.

Between scenarios the runner deletes the created feeds and then **waits for quiescence**
(global feed/instance counts back at the pre-scenario baseline): feed deletion in CUBE is
asynchronous, and without the gate its churn would contaminate the next level's numbers.

Three topologies (`linear`, `fanout_fanin`, `diamond`) are built from `dbg-bigfiles` (fs
root), `pl-simpledsapp` (ds), and `pl-topologicalcopy` (ts merge) — all registered by
`just bench-start` from the committed pin, `benchmarks/chrisomatic/benchmark.yml`.

## Reading the results

Each run writes `benchmarks/results/<run_id>/`:

```
report.md            # ticket-ready: environment, breaking-point table, approach-to-failure curves
summary.json         # machine-readable run summary (+ Postgres counters over the whole run)
levels.jsonl/.csv    # one row per (topology, axis, level): the approach-to-failure curve
environment.json     # host hardware, image ids (CUBE, pfcon, Postgres, the broker and the cache), envelope knobs, container limits, Postgres settings, workload plugin versions and images, the pin they were checked against, the resource sampler's settings, noise floor, attribution availability
api_requests.jsonl   # every measured API call (latency, status, endpoint class)
status_samples.jsonl # per-instance status timelines (client-observed phase timing)
docker_stats.jsonl   # every service + job container read every 1 s: CPU over the time since its previous reading, or since a container born in the scenario started (null without a window: a service's first reading, one under 0.75 s after the previous, as the closing reading can be, whose CPU then goes into the next reading's window, a job's first under 0.25 s after its start, one after a restart), memory with and without the page cache, CPU throttling, cumulative blkio and CPU counters
queue_depths.jsonl   # Celery queue depths (main1/main2/periodic), sampled every 0.5 s
scenarios/<id>/      # scenario.json with db_counters and the sampler's summary (observer.sampler) (+ pg_stats.json; failure.json + logs/ on hard failures)
```

The key artifact is the **approach-to-failure curve** (in `report.md` and `levels.csv`):
per-level makespan, worst p95, 5xx counts and completions, with `report.md` adding at each
axis's highest level the peak per-service CPU, the bytes each service wrote, Postgres and how
the sampling went — so each breaking point comes with a *cause*, not just a pass/fail.
`levels.jsonl` has every level's peaks: CPU, memory with and without the page cache, CPU
throttling, bytes written, queue depths, and the sampler's summary.

Three attribution streams turn resource saturation into named culprits:

- **Queue depths** — a sustained rise on `main2` means status checks are produced
  faster than the workers consume them; `main1` is job submission. Per-scenario
  peaks/means are in `scenario.json` (`queues`) and level rows (`peak_queue_depth`).
- **`pg_stats.json`** — every statement the scenario ran (calls, time, rows, temp blocks
  written, WAL and full-page images), most total execution time first, reset per scenario
  (via `pg_stat_statements`, which `postgres/cube.conf` preloads on `db`). Names the
  queries behind a hot db, and shows whether a statement ran at all (statements that failed
  with an error are never recorded).
- **Postgres counters** — checkpoints (done, timed and requested), WAL and full-page images,
  temp files, bytes written or extended per process type, autovacuum runs, and the `db`
  container's disk writes, swap and OOM kills, as deltas over each scenario's workload and
  cleanup windows (`scenario.json` and level rows, `db_counters`) and over the whole run
  (`summary.json`, which `bench-compare` shows side by side).
  [envelope.md](envelope.md) § "What each run records about Postgres" has the details.

`environment.json`'s `attribution` block records whether each stream was available for the
run.

## Control plane: API saturation with Locust (RED)

The escalation harness above measures the **data plane** (orchestration). The **control
plane** — the synchronous API + DB + connection pool — is measured separately with a Locust
load test that drives concurrent API clients and reports **RED** (rate, errors, duration
percentiles): the saturation knee, error onset, and DB connection-pool exhaustion that the
data plane doesn't surface. Bring the stack up (`just bench-start`), then:

```bash
just bench-locust '-u 100 -r 20 -t 3m'   # 100 users, ramp 20/s, for 3 minutes
```

It is **read-only by default** (list feeds/instances/files, plugin search) — the cleanest
saturation signal, and it won't flood the compute side. Set `BENCH_LOCUST_WRITE=1` to also
exercise the create path (it spawns real DAGs, so run on a stack you can `just nuke`).
`host` comes from `CUBE_URL` (no `--host` needed); auth is once via a DRF (Django REST Framework) token, like the
data-plane harness.

To find the knee, sweep the user count, giving each level its own CSV prefix:

```bash
for u in 25 50 100 200 400; do
  just bench-locust "-u $u -r $u -t 90s --csv /app/benchmarks/results/locust/read_u$u"
done
```

Each step also checks the API's health, so that a stack an overload left broken cannot pass
for the next step's measurement. Before the step starts, and for up to
`BENCH_LOCUST_RECOVERY_S` seconds after it ends (default 60; `0` disables both checks), the
locustfile sends bursts of 16 concurrent `GET plugins/` requests, each on its own connection,
which most likely reaches every `uvicorn` worker: a single request can be answered by the one
healthy worker. The API counts as recovered once a burst succeeds and every worker's
`db_pool_stats` log line reports no checked-out connection, which does make sure of every
worker (see `CUBE_DB_POOL_STATS_INTERVAL` in [envelope.md](envelope.md)). The result,
with every probe round and the database's connection states, goes to
`<csv prefix>_recovery.json`, and Locust exits with code 3 when the API was unhealthy before
the step or did not recover after it.

Locust writes `*_stats.csv` / `*_failures.csv` to `benchmarks/results/locust/` (a plain run
uses the `run_*` prefix). Render a human-readable summary — the control-plane counterpart of
the data-plane `report.md` — with:

```bash
python -m benchmarks.locust_report benchmarks/results/locust   # writes report.md there
```

The report's sweep table then also says, for each step, whether the API was healthy before it
and how long it took to recover after it.

**Reading it:** throughput climbs to a peak then *collapses* past the knee while p95 spikes;
a p95 pinned at `CUBE_DB_POOL_TIMEOUT` (10 s) with rising 5xx/401s is the signature of **DB
connection-pool exhaustion** (pool size × uvicorn workers vs `db` CPUs). See
[REPORT.md](REPORT.md) §4 for the measured curve and [history/README.md](history/README.md)
for the archived RED report.

## Comparing runs over time

Every run records a **workload fingerprint** (the effective tier baseline + axes +
cadence, hashed), the full envelope, host manifest, and auth mode — enough to decide
later whether two runs are comparable and to diff them:

```bash
just bench-compare <baseline_run_id> <candidate_run_id>
just bench-compare A B C D                        # 3+ runs: adds a per-axis trend table
just bench-compare A B --metric peak_write_bytes.db     # trend a level-row value (dotted path)
just bench-compare A B --fail-on-regression       # CI gate: exit 1 on any regression,
                                                  # downward breaking-point shift, or
                                                  # when the runs are NOT COMPARABLE
```

Run ids resolve against `results/` and the committed `history/` archive alike. The
output (`results/comparisons/<a>_vs_<b>/compare.{md,csv}`) leads with a
**comparability verdict**: workload-fingerprint / workload-plugin version / storage / auth
mismatches are *blockers* (the diff is still rendered, loudly labelled), envelope / host /
threshold / container-limit / Postgres-settings / resource-sampler / workload-plugin image
differences are *cautions*, and commit or service image differences are the point of
comparing.

A level is flagged **REGRESSED**/**IMPROVED** only when the change exceeds *both* the
relative threshold (`--regress-pct`, default 20%) and the metric's absolute floor
(`--makespan-floor` 2 s — makespan is quantized by the poll cadence; `--latency-floor`
50 ms — below the API noise floor). Verdict flips and **per-axis breaking-point
shifts** (the most noise-robust trend signal) are flagged regardless.

To build a history worth comparing against, archive milestone runs:

```bash
just bench-archive <run_id>     # copies summary.json, levels.jsonl, environment.json and
                                # the rendered report.md (a few KB) into
                                # benchmarks/history/<run_id>/
```

Commit the archived run together with the change it validates; raw streams
(`api_requests.jsonl`, `docker_stats.jsonl`) stay out of version control.

## Measuring state aging

CUBE accumulates feeds/files over years; a benchmark on a fresh DB misses
time-degradation. The `aging-*` tiers measure it as a loop:

```bash
just bench-run --tier aging-grow --no-cleanup   # accumulate rows (~56k files/invocation)
just bench-run --tier aging-probe               # fixed 2-instance probe, with cleanup
```

Alternate them N times; plot the probe's makespan and create/list p95 against the
absolute row counts each scenario records in `db_deltas`
(`feeds_total`/`instances_total`/`files_total`). Growth is permanent by design — run on
a stack you can `just nuke` afterwards.

## Caveats (v1)

- **Phase timing is client-observed.** CUBE keeps no status-transition history, so the
  poller timestamps the first sighting of each status — resolution equals the poll
  cadence, which itself stretches (up to 5 s) as the feeds axis escalates to keep the
  harness's own observation traffic bounded. Each `scenario.json` records the effective
  cadence and the poll request count under `observer`.
- **The harness authenticates once via a DRF token.** Per-request HTTP Basic would make
  CUBE run its password hasher on every call (~100 ms each) and distort every number; if
  the report's environment section says `basic`, the run is degraded — fix auth and re-run.
- **`fslink` hides byte-copy cost.** File **count** is the real control-plane stressor;
  file **size** is a sanity check only (escalate count, not size, for meaningful results).
- **Makespan is poll-gated.** Record `CUBE_CELERY_POLL_INTERVAL`; run the sweep at
  `2s`, `4s` and `8s` to separate poller cost from CUBE's true capacity. Do **not** go
  below `2s`: the `skip_if_running` guard's ~1 s `inspect()` broadcast makes the
  periodic scheduler tasks mutually skip at sub-2 s cadences (see `envelope.md`).
- **Run authoritative sweeps on a dedicated Linux box.** On Docker Desktop the VM's own
  limits dominate the per-container envelope, so laptop numbers are directional only.
- **The workload plugins are pinned.** `just bench-start` registers `dbg-bigfiles` 1.0.0,
  `pl-simpledsapp` 2.1.5 and `pl-topologicalcopy` 1.0.13 by image digest from
  `benchmarks/chrisomatic/benchmark.yml`, not from the dev stack's `chrisomatic.yml` (which takes
  `pl-topologicalcopy` from the ChRIS store, now a no-op 2.0.0). The harness refuses to start
  unless CUBE holds exactly those registrations, one version each, and records them and their
  images under `workload_plugins` in `environment.json`, and the pin under
  `workload_plugins_pin`. The dev and benchmark
  stacks share their volumes, so after a dev `just` run `just bench-nuke` (it deletes the dev
  data too) before `just bench-start`; an arm64 laptop needs a pin of its own. `envelope.md`
  § "Pinned workload plugin versions" has the details (STRATEGY.md § Workload Plugins).
