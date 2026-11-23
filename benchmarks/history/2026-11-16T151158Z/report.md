# CUBE Load & Scalability Benchmark — Report

**Run:** 2026-11-16T151158Z  •  **Tier:** full  •  **Storage:** fslink  •  **Commit:** de29c61 (dirty: false)

Levels run: 4  •  Breaking points: 0

## Breaking points

_No hard failure reached within the configured caps._

## Postgres over the whole run

_checkpoints 1 done (2 timed / 0 requested), WAL 5.1 GiB (231,617 full-page images), temp files 1.5 GiB, db container wrote 20.1 GiB, WAL segments zero-filled 5.0 GiB_

## Approach to failure (per axis)

_CPU% is relative to one host core (100% = one core); judge saturation against each service's `cpus` limit in the envelope, not against 100%._

### diamond — layers

| Level | Verdict | Makespan p50 (s) | Worst p95 (ms) | 5xx | Completed | Criteria |
|---|---|---|---|---|---|---|
| 1 | PASS | 11.33 | 57.4 | 0 | 6 |  |
| 2 | PASS | 19.6 | 40.9 | 0 | 11 |  |
| 4 | PASS | 37.48 | 41.6 | 0 | 21 |  |
| 8 | PASS | 1305.68 | 359.4 | 0 | 41 |  |

_Celery workers logged 1.4 MiB in a scenario of level 8 (worker-periodic 989.3 KiB, worker-mains 222.8 KiB, worker-background 84.4 KiB, worker-heavy 52.2 KiB, worker-register 35.9 KiB)_

_Peak CPU at level 8: worker-mains 400.4% (per service: {'worker-periodic': 20.5, 'chris': 72.6, 'worker-mains': 400.4, 'celery-scheduler': 3.4, 'worker-background': 67.0, 'worker-heavy': 400.4, 'worker-register': 28.0, 'chris-cache': 4.4, 'pfcon': 202.4, 'nats': 0.4, 'db': 205.1, 'chris-broker': 5.3}); plugin-jobs peak 100.1%_

_Peak disk write at level 8: db 18.8 GiB (per service: {'worker-periodic': '348.0 KiB', 'worker-mains': '304.0 KiB', 'worker-background': '136.0 KiB', 'worker-heavy': '981.0 MiB', 'worker-register': '80.0 KiB', 'pfcon': '328.0 KiB', 'db': '18.8 GiB'})_

_Postgres at level 8: workload checkpoints 0 done (1 timed / 0 requested), WAL 4.9 GiB (5,047 full-page images), temp files 956.8 MiB, db container wrote 18.8 GiB, WAL segments zero-filled 4.9 GiB; cleanup checkpoints 1 done (1 timed / 0 requested), WAL 189.0 MiB (125,865 full-page images), temp files 626.5 MiB, db container wrote 1.2 GiB, WAL segments zero-filled 112.0 MiB_

_Sampling at level 8: every 1.0 s, 1309 ticks (0 skipped, slowest 0.801 s), 15908 reads (7 failed, 9 readings missed behind a read still running); 42 plugin-job containers seen for 41 instances; queues every 0.5 s, 2615 ticks_

**Sampling fell behind or failed at level(s) 8** (skipped ticks, failed reads or listings, readings missed behind a hung read, or failed queue reads in `levels.jsonl`): their peaks may miss readings.

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
| Noise floor (health p50/p95 ms) | 4.0 / 6.4 |
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

