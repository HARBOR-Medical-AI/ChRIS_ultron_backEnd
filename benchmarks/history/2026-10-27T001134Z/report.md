# CUBE Load & Scalability Benchmark — Report

**Run:** 2026-10-27T001134Z  •  **Tier:** smoke  •  **Storage:** fslink  •  **Commit:** c575149 (dirty: false)

Levels run: 10  •  Breaking points: 0

## Breaking points

_No hard failure reached within the configured caps._

## Postgres over the whole run

_checkpoints 0 done (0 timed / 0 requested), WAL 1.1 MiB (67 full-page images), temp files 0 B, db container wrote 9.7 MiB_

## Approach to failure (per axis)

_CPU% is relative to one host core (100% = one core); judge saturation against each service's `cpus` limit in the envelope, not against 100%._

### linear — depth

| Level | Verdict | Makespan p50 (s) | Worst p95 (ms) | 5xx | Completed | Criteria |
|---|---|---|---|---|---|---|
| 1 | PASS | 8.18 | 66.6 | 0 | 2 |  |
| 2 | PASS | 11.33 | 38.6 | 0 | 3 |  |

_Peak CPU at level 2: db 10.1% (per service: {'worker-periodic': 9.0, 'chris': 8.0, 'celery-scheduler': 2.5, 'worker-mains': 9.6, 'pfcon': 1.7, 'db': 10.1, 'nats': 0.1, 'chris-cache': 4.3, 'chris-broker': 4.3})_

_Peak disk write at level 2: db 592.0 KiB (per service: {'worker-periodic': '40.0 KiB', 'chris': '16.0 KiB', 'worker-mains': '132.0 KiB', 'pfcon': '24.0 KiB', 'db': '592.0 KiB'})_

_Postgres at level 2: workload checkpoints 0 done (0 timed / 0 requested), WAL 56.9 KiB (0 full-page images), temp files 0 B, db container wrote 624.0 KiB; cleanup checkpoints 0 done (0 timed / 0 requested), WAL 1.4 KiB (0 full-page images), temp files 0 B, db container wrote 36.0 KiB_

_Sampling at level 2: every 1.0 s, 13 ticks (0 late, slowest 0.022 s), 118 reads (0 failed); 1 plugin-job containers seen for 3 instances; queues every 0.5 s, 23 ticks_

### linear — file_count

| Level | Verdict | Makespan p50 (s) | Worst p95 (ms) | 5xx | Completed | Criteria |
|---|---|---|---|---|---|---|
| 1 | PASS | 7.15 | 45.4 | 0 | 2 |  |
| 10 | PASS | 7.16 | 39.3 | 0 | 2 |  |

_Peak CPU at level 10: db 14.7% (per service: {'worker-periodic': 8.8, 'chris': 6.1, 'celery-scheduler': 1.7, 'worker-mains': 11.9, 'pfcon': 1.6, 'db': 14.7, 'nats': 0.1, 'chris-cache': 3.9, 'chris-broker': 4.8})_

_Peak disk write at level 10: db 1.2 MiB (per service: {'worker-periodic': '24.0 KiB', 'chris': '12.0 KiB', 'worker-mains': '96.0 KiB', 'pfcon': '16.0 KiB', 'db': '1.2 MiB'})_

_Postgres at level 10: workload checkpoints 0 done (0 timed / 0 requested), WAL 270.0 KiB (30 full-page images), temp files 0 B, db container wrote 1.2 MiB; cleanup checkpoints 0 done (0 timed / 0 requested), WAL 2.3 KiB (0 full-page images), temp files 0 B, db container wrote 40.0 KiB_

_Sampling at level 10: every 1.0 s, 9 ticks (0 late, slowest 0.023 s), 81 reads (0 failed); 0 plugin-job containers seen for 2 instances; queues every 0.5 s, 15 ticks_

### fanout_fanin — branches

| Level | Verdict | Makespan p50 (s) | Worst p95 (ms) | 5xx | Completed | Criteria |
|---|---|---|---|---|---|---|
| 1 | PASS | 11.28 | 41.3 | 0 | 3 |  |
| 2 | PASS | 11.37 | 38.5 | 0 | 4 |  |

_Peak CPU at level 2: worker-mains 14.2% (per service: {'worker-periodic': 10.0, 'chris': 10.4, 'celery-scheduler': 2.1, 'worker-mains': 14.2, 'pfcon': 2.9, 'db': 13.9, 'nats': 0.1, 'chris-cache': 4.2, 'chris-broker': 3.5})_

_Peak disk write at level 2: db 1.4 MiB (per service: {'worker-periodic': '36.0 KiB', 'chris': '16.0 KiB', 'worker-mains': '160.0 KiB', 'pfcon': '32.0 KiB', 'db': '1.4 MiB'})_

_Postgres at level 2: workload checkpoints 0 done (0 timed / 0 requested), WAL 103.7 KiB (1 full-page images), temp files 0 B, db container wrote 1.4 MiB; cleanup checkpoints 0 done (0 timed / 0 requested), WAL 1.9 KiB (0 full-page images), temp files 0 B, db container wrote 24.0 KiB_

_Sampling at level 2: every 1.0 s, 13 ticks (0 late, slowest 0.02 s), 117 reads (0 failed); 0 plugin-job containers seen for 4 instances; queues every 0.5 s, 24 ticks_

### diamond — branches

| Level | Verdict | Makespan p50 (s) | Worst p95 (ms) | 5xx | Completed | Criteria |
|---|---|---|---|---|---|---|
| 1 | PASS | 11.35 | 41.9 | 0 | 3 |  |
| 2 | PASS | 11.41 | 37.7 | 0 | 4 |  |

_Peak CPU at level 2: worker-mains 19.9% (per service: {'worker-periodic': 10.1, 'chris': 9.8, 'celery-scheduler': 1.9, 'worker-mains': 19.9, 'pfcon': 3.2, 'db': 17.3, 'nats': 0.1, 'chris-cache': 3.5, 'chris-broker': 4.2}); plugin-jobs peak 76.0%_

_Peak disk write at level 2: db 816.0 KiB (per service: {'worker-periodic': '36.0 KiB', 'chris': '16.0 KiB', 'worker-mains': '156.0 KiB', 'pfcon': '32.0 KiB', 'db': '816.0 KiB'})_

_Postgres at level 2: workload checkpoints 0 done (0 timed / 0 requested), WAL 79.2 KiB (0 full-page images), temp files 0 B, db container wrote 840.0 KiB; cleanup checkpoints 0 done (0 timed / 0 requested), WAL 183.2 KiB (29 full-page images), temp files 0 B, db container wrote 868.0 KiB_

_Sampling at level 2: every 1.0 s, 13 ticks (0 late, slowest 0.082 s), 125 reads (0 failed); 1 plugin-job containers seen for 4 instances; queues every 0.5 s, 24 ticks_

### diamond — layers

| Level | Verdict | Makespan p50 (s) | Worst p95 (ms) | 5xx | Completed | Criteria |
|---|---|---|---|---|---|---|
| 1 | PASS | 12.43 | 38.0 | 0 | 3 |  |
| 2 | PASS | 19.72 | 40.5 | 0 | 5 |  |

_Peak CPU at level 2: chris 14.4% (per service: {'worker-periodic': 9.3, 'chris': 14.4, 'celery-scheduler': 1.7, 'worker-mains': 11.0, 'pfcon': 1.9, 'db': 12.9, 'nats': 0.1, 'chris-cache': 4.0, 'chris-broker': 4.7})_

_Peak disk write at level 2: db 1.1 MiB (per service: {'worker-periodic': '60.0 KiB', 'chris': '28.0 KiB', 'worker-mains': '252.0 KiB', 'pfcon': '40.0 KiB', 'db': '1.1 MiB'})_

_Postgres at level 2: workload checkpoints 0 done (0 timed / 0 requested), WAL 131.2 KiB (6 full-page images), temp files 0 B, db container wrote 1.1 MiB; cleanup checkpoints 0 done (0 timed / 0 requested), WAL 2.4 KiB (0 full-page images), temp files 0 B, db container wrote 24.0 KiB_

_Sampling at level 2: every 1.0 s, 21 ticks (0 late, slowest 0.02 s), 189 reads (0 failed); 0 plugin-job containers seen for 5 instances; queues every 0.5 s, 40 ticks_

## Environment

| Field | Value |
|---|---|
| Host OS | Ubuntu 26.04.1 LTS |
| Kernel | 7.0.0-38-generic |
| Arch | x86_64 |
| Host CPUs | 16 |
| Host memory (bytes) | 132318355456 |
| Docker root | /var/lib/docker |
| Engine | 29.9.2 |
| API auth | token |
| Noise floor (health p50/p95 ms) | 4.1 / 6.9 |
| Resource sampler | one-shot reads, CPU between consecutive readings: every 1.0 s, queues every 0.5 s |

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
| PFCON_WORKERS | 8 |
| STORAGE_ENV | fslink |
| CUBE_DEBUG | false |
| CUBE_CACHE_URL | redis://chris-cache:6379/0 |

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
| worker-mains | 4.0 | 4.0 GiB | 8.0 GiB | 64.0 MiB |
| worker-periodic | 1.0 | 1.0 GiB | 2.0 GiB | 64.0 MiB |

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

