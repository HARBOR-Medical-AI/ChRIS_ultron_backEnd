# CUBE Load & Scalability Benchmark — Report

**Run:** 2026-10-27T004515Z  •  **Tier:** full  •  **Storage:** fslink  •  **Commit:** c575149 (dirty: false)

Levels run: 6  •  Breaking points: 1

## Breaking points

| Topology | Axis | Broke at level | Criteria |
|---|---|---|---|
| fanout_fanin | feeds | 32 | cancelled:inst-352 |

## Postgres over the whole run

_checkpoints 0 done (0 timed / 0 requested), WAL 13.9 MiB (125 full-page images), temp files 0 B, db container wrote 98.1 MiB, WAL segments zero-filled 16.0 MiB_

## Approach to failure (per axis)

_CPU% is relative to one host core (100% = one core); judge saturation against each service's `cpus` limit in the envelope, not against 100%._

### fanout_fanin — feeds

| Level | Verdict | Makespan p50 (s) | Worst p95 (ms) | 5xx | Completed | Criteria |
|---|---|---|---|---|---|---|
| 1 | PASS | 12.51 | 64.3 | 0 | 6 |  |
| 2 | PASS | 10.79 | 53.5 | 0 | 12 |  |
| 4 | PASS | 12.82 | 77.5 | 0 | 24 |  |
| 8 | PASS | 16.04 | 97.4 | 0 | 48 |  |
| 16 | PASS | 27.57 | 181.4 | 0 | 96 |  |
| 32 | FAIL | 70.04 | 485.3 | 0 | 191 | cancelled:inst-352 |

_Peak CPU at level 32: worker-mains 411.9% (per service: {'celery-scheduler': 3.1, 'worker-periodic': 30.0, 'worker-mains': 411.9, 'chris': 385.0, 'chris-broker': 6.7, 'db': 198.8, 'nats': 0.1, 'chris-cache': 3.5, 'pfcon': 50.2}); plugin-jobs peak 82.1%_

_Peak disk write at level 32: db 39.3 MiB (per service: {'worker-periodic': '380.0 KiB', 'worker-mains': '7.4 MiB', 'chris': '128.0 KiB', 'db': '39.3 MiB', 'pfcon': '1.5 MiB'})_

_Postgres at level 32: workload checkpoints 0 done (0 timed / 0 requested), WAL 6.7 MiB (47 full-page images), temp files 0 B, db container wrote 39.3 MiB; cleanup checkpoints 0 done (0 timed / 0 requested), WAL 406.7 KiB (0 full-page images), temp files 0 B, db container wrote 3.8 MiB_

_Sampling at level 32: every 1.0 s, 75 ticks (0 late, slowest 0.237 s), 859 reads (0 failed); 88 plugin-job containers seen for 192 instances; queues every 0.5 s, 148 ticks_

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
| Noise floor (health p50/p95 ms) | 4.5 / 6.5 |
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

