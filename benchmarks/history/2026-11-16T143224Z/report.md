# CUBE Load & Scalability Benchmark — Report

**Run:** 2026-11-16T143224Z  •  **Tier:** full  •  **Storage:** fslink  •  **Commit:** de29c61 (dirty: false)

Levels run: 8  •  Breaking points: 0

## Breaking points

_No hard failure reached within the configured caps._

## Postgres over the whole run

_checkpoints 1 done (1 timed / 0 requested), WAL 119.9 MiB (808 full-page images), temp files 0 B, db container wrote 802.3 MiB, WAL segments zero-filled 96.0 MiB_

## Approach to failure (per axis)

_CPU% is relative to one host core (100% = one core); judge saturation against each service's `cpus` limit in the envelope, not against 100%._

### linear — feeds

| Level | Verdict | Makespan p50 (s) | Worst p95 (ms) | 5xx | Completed | Criteria |
|---|---|---|---|---|---|---|
| 1 | PASS | 19.52 | 103.7 | 0 | 15 |  |
| 2 | PASS | 18.87 | 75.5 | 0 | 30 |  |
| 4 | PASS | 20.74 | 78.3 | 0 | 60 |  |
| 8 | PASS | 20.16 | 130.4 | 0 | 120 |  |
| 16 | PASS | 23.97 | 310.8 | 0 | 240 |  |
| 32 | PASS | 41.88 | 499.7 | 0 | 480 |  |
| 64 | PASS | 82.7 | 1007.7 | 0 | 960 |  |
| 128 | PASS | 169.37 | 1711.9 | 0 | 1920 |  |

_Celery workers logged 4.6 MiB in a scenario of level 128 (worker-heavy 37.8 KiB, worker-mains 2.4 MiB, worker-background 1.1 MiB, worker-periodic 104.4 KiB, worker-register 1.0 MiB)_

_Peak CPU at level 128: chris 398.2% (per service: {'worker-heavy': 0.5, 'chris': 398.2, 'celery-scheduler': 3.8, 'worker-mains': 248.4, 'worker-background': 80.4, 'worker-periodic': 35.1, 'worker-register': 102.1, 'db': 207.8, 'chris-broker': 6.6, 'nats': 0.1, 'pfcon': 69.3, 'chris-cache': 4.3}); plugin-jobs peak 66.5%_

_Peak disk write at level 128: db 137.5 MiB (per service: {'worker-heavy': '4.0 KiB', 'worker-mains': '6.6 MiB', 'worker-background': '2.7 MiB', 'worker-periodic': '744.0 KiB', 'worker-register': '2.3 MiB', 'db': '137.5 MiB', 'pfcon': '4.5 MiB'})_

_Postgres at level 128: workload checkpoints 1 done (1 timed / 0 requested), WAL 20.2 MiB (408 full-page images), temp files 0 B, db container wrote 138.1 MiB, WAL segments zero-filled 16.0 MiB; cleanup checkpoints 0 done (0 timed / 0 requested), WAL 797.7 KiB (39 full-page images), temp files 0 B, db container wrote 5.6 MiB_

_Sampling at level 128: every 1.0 s, 540 ticks (0 skipped, slowest 0.426 s), 8492 reads (0 failed); 678 plugin-job containers seen for 1920 instances; queues every 0.5 s, 1073 ticks_

**Sampling fell behind or failed at level(s) 64** (skipped ticks, failed reads or listings, readings missed behind a hung read, or failed queue reads in `levels.jsonl`): their peaks may miss readings.

## Environment

| Field | Value |
|---|---|
| Host OS | Ubuntu 26.04.1 LTS |
| Kernel | 7.0.0-43-generic |
| Arch | x86_64 |
| Host CPUs | 16 |
| Host memory (bytes) | 132318355456 |
| Docker root | /var/lib/docker |
| Engine | 29.9.3 |
| API auth | token |
| Noise floor (health p50/p95 ms) | 3.9 / 5.5 |
| Resource sampler | one-shot reads, CPU between consecutive readings: every 1.0 s, queues every 0.5 s, 8 workers |

### Envelope (recorded knobs)

| Knob | Value |
|---|---|
| CUBE_CELERY_POLL_INTERVAL | 2.0 |
| CUBE_UVICORN_WORKERS | 4 |
| CUBE_DB_POOL_MIN_SIZE | 2 |
| CUBE_DB_POOL_MAX_SIZE | 10 |
| CUBE_DB_POOL_TIMEOUT | 10 |
| CUBE_DB_MAX_CONNECTIONS | 300 |
| CUBE_DB_SYNCHRONOUS_COMMIT | on |
| CUBE_WORKER_MAINS_CONCURRENCY | 4 |
| CUBE_WORKER_REGISTER_CONCURRENCY | 2 |
| CUBE_WORKER_HEAVY_CONCURRENCY | 4 |
| CUBE_WORKER_BACKGROUND_CONCURRENCY | 2 |
| PFCON_WORKERS | 8 |
| STORAGE_ENV | fslink |
| CUBE_DEBUG | false |
| CUBE_CACHE_URL | redis://chris-cache:6379/0 |
| CUBE_CELERY_PENDING_TTL | 60 |
| CUBE_CELERY_VISIBILITY_TIMEOUT | 3600 |
| CUBE_CELERY_REGISTER_FAST_MAX_FILES | 100 |

### Container limits (as applied)

| Service | CPUs | Memory | Memory + swap | /dev/shm |
|---|---|---|---|---|
| celery-scheduler | 0.5 | 512.0 MiB | 1.0 GiB | 64.0 MiB |
| chris | 4.0 | 4.0 GiB | 8.0 GiB | 64.0 MiB |
| chris-broker | 2.0 | 1.0 GiB | 2.0 GiB | 64.0 MiB |
| chris-cache | 1.0 | 512.0 MiB | 1.0 GiB | 64.0 MiB |
| db | 2.0 | 4.0 GiB | 8.0 GiB | 1.0 GiB |
| nats | — | — | — | 64.0 MiB |
| pfcon | 2.0 | 2.0 GiB | 4.0 GiB | 64.0 MiB |
| worker-background | 1.0 | 1.0 GiB | 2.0 GiB | 64.0 MiB |
| worker-heavy | 4.0 | 4.0 GiB | 8.0 GiB | 64.0 MiB |
| worker-mains | 4.0 | 4.0 GiB | 8.0 GiB | 64.0 MiB |
| worker-periodic | 1.0 | 1.0 GiB | 2.0 GiB | 64.0 MiB |
| worker-register | 1.0 | 1.0 GiB | 2.0 GiB | 64.0 MiB |

### PostgreSQL (key settings; environment.json has all)

| Setting | Value |
|---|---|
| autovacuum_max_workers | 3 |
| checkpoint_completion_target | 0.9 |
| checkpoint_timeout | 15min |
| config_file | /etc/postgresql/cube.conf |
| data_checksums | on |
| DateStyle | ISO, MDY |
| default_text_search_config | pg_catalog.english |
| dynamic_shared_memory_type | posix |
| effective_cache_size | 3GB |
| effective_io_concurrency | 200 |
| full_page_writes | on |
| hash_mem_multiplier | 2 |
| io_method | worker |
| io_workers | 3 |
| jit | on |
| lc_messages | en_US.utf8 |
| lc_monetary | en_US.utf8 |
| lc_numeric | en_US.utf8 |
| lc_time | en_US.utf8 |
| listen_addresses | * |
| log_lock_waits | on |
| log_min_duration_statement | 1s |
| log_timezone | UTC |
| maintenance_work_mem | 512MB |
| max_connections | 300 |
| max_parallel_workers_per_gather | 2 |
| max_wal_size | 8GB |
| min_wal_size | 1GB |
| random_page_cost | 1.1 |
| server_version | 18.2 (Debian 18.2-1.pgdg13+1) |
| shared_buffers | 1GB |
| shared_preload_libraries | pg_stat_statements |
| synchronous_commit | on |
| TimeZone | UTC |
| track_io_timing | on |
| wal_buffers | 16MB |
| wal_compression | lz4 |
| wal_level | replica |
| work_mem | 32MB |

_`core_chrisfile` storage parameters: autovacuum_vacuum_scale_factor=0.02, autovacuum_analyze_scale_factor=0.01_

_`core_chrisfolder` storage parameters: autovacuum_vacuum_scale_factor=0.02, autovacuum_analyze_scale_factor=0.01_

### Workload plugins

| Role | Plugin | Version | Image | Digest | Installed versions |
|---|---|---|---|---|---|
| fs | dbg-bigfiles | 1.0.0 | fnndsc/dbg-bigfiles@sha256:b956e35ee69c249677489b2bf1815a578c129c478bbdc4bff8c4c75535cd3e7f | sha256:b956e35ee69c | 1 |
| ds | pl-simpledsapp | 2.1.5 | fnndsc/pl-simpledsapp@sha256:c4e8726281d4dff3266bb78ea80dcd9e3732eb02dbde86ce5633da2d960a7207 | sha256:c4e8726281d4 | 1 |
| ts | pl-topologicalcopy | 1.0.13 | fnndsc/pl-topologicalcopy@sha256:35cb5798f9035cbd4f879049498ecd3d1bcf93bb95aa0b5eb3960fc65e47b8ee | sha256:35cb5798f903 | 1 |

Pinned by `benchmarks/chrisomatic/benchmark.yml` (sha256 3ec92f12faaa).

### Manual fields (fill in)

- **storage_device_type_throughput:** TODO
- **power_thermal_mode:** TODO
- **other_significant_workloads:** TODO

