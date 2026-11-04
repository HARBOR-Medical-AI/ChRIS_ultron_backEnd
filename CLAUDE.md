# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

CUBE (ChRIS Ultron BackEnd) is the Django/DRF backend of the ChRIS medical-compute platform.
Stack: Django 6.0 (pinned `<6.1`), DRF, PostgreSQL 18, Celery (Dragonfly as the Redis-compatible
broker and shared cache), django-channels + NATS for live PACS progress, uvicorn in production.
The HTTP API at `/api/v1/` speaks `application/vnd.collection+json` first, plain JSON second.

The Django project root is `chris_backend/` (bind-mounted into the container at
`/opt/app-root/src`). Everything else at the repo root is packaging, compose, CI, and the
`benchmarks/` harness (its own `pyproject.toml`/`uv.lock`, built from its own context).

## Commands

Everything runs in containers through `just`. Never call `docker compose` directly: the justfile
injects `UID`/`GID`, the engine socket, and the storage-override compose files.

```shell
just                      # = just dev: start services, migrate, register plugins/users (chrisomatic), attach
just up / just down       # (re)create / stop services; `just nuke` also wipes volumes and job containers
just build && just up     # required after changing Dockerfile, pyproject.toml, or uv.lock
just logs [service]

just test-unit            # manage.py test --exclude-tag integration
just test-integration     # manage.py test --tag integration (needs pfcon + plugin images)
just test-all
just test <label...>      # Django test labels, e.g. just test feeds.tests.test_views
                          #   or just test feeds.tests.test_views.FeedListViewTests.test_feed_list_success
just test-coverage        # full suite under coverage + report
just test-unit-coverage   # writes chris_backend/coverage.xml (what CI uploads to Codacy)

just shell / just bash    # Django shell / bash inside the chris container
just run <cmd>            # docker-compose run --rm chris <cmd>
just makemigrations / just migrate

just openapi > /dev/null        # CI fails on ANY drf-spectacular warning or error
just openapi-split > /dev/null  # same, with SPECTACULAR_SPLIT_REQUEST=true

just lint [--all] [--base REF]  # issues Codacy would call new: files changed since the merge-base
                          #   with origin/master, uncommitted included; exit 1 on any, 2 if a tool
                          #   failed (scripts/lint.py); advisories marked ? may still be new to Codacy
just lint-fix [--unsafe]  # Ruff's fixes for the new pyflakes issues only, then lint again
just lint-test            # scripts/test_lint.py, in the dev image (no stack)

just lock                 # re-resolve both uv.lock files after editing a pyproject.toml
just lock-upgrade <pkg>   # bump one dependency
just lock-check           # fails if a lockfile is stale (CI's first job; Dockerfile uses uv sync --locked)

just set-storage s3|swift|fslink   # persist storage mode (.storage); default fslink
just prefer docker|podman          # persist engine choice (.preference)
just dev-uvicorn          # serve via ASGI on :8000 (runserver is WSGI and cannot serve WebSockets)

just bench-start / bench-run --tier smoke / bench-test / bench-down   # benchmarks/ harness (pytest)
just bench-nuke           # bench-down, reap jobs, remove volumes (the dev stack's too)
```

The dev API is at http://localhost:8000/api/v1/ (superuser `chris`/`chris1234`, user
`cube`/`cube1234`). `uv sync` on the host is only for editor/LSP support; tests need the stack.
There is no lint configuration and no lint step in CI: Codacy checks pull requests, and its
Python tools are dev dependencies for `just lint`.

## Testing conventions

- Runner is Django's `manage.py test` (not pytest). Integration tests are marked
  `@tag('integration')`; they talk to the real pfcon service and run real plugin containers.
  Tests marked `@tag('compose')` check how the compose stacks run PostgreSQL
  (`postgres/cube.conf`); add `--exclude-tag compose` to run the suite against any other server.
- Tests that exercise Celery use `TransactionTestCase` and start an in-process worker with
  `celery.contrib.testing.worker.start_worker(celery_app)` after setting
  `celery_app.conf.update(task_routes=None)`, so tasks land on the default `celery` queue, which
  is reserved for tests (see `core/celery.py`). `core/testrunner.py` terminates leftover DB
  connections so the test database can be dropped (only when the run set up a test database).
- `core/tests/test_cache.py` runs against the stack's cache server (`chris-cache`, database
  0), which a running dev stack also uses: its tests use names of their own and short
  TTLs, and never call `cache.clear()` or `SCRIPT FLUSH`, which can wedge a Dragonfly that is
  running scripts. Its `CacheProxy` takes the server away mid-test (stopped or hung).
- `core.storage.helpers.mock_storage(target_settings)` swaps the storage backend for a temp
  directory. `core/tests/test_swiftmanager.py` and `test_s3manager.py` need the matching
  `just set-storage` environment.
- Test fixtures create users directly with `User.objects.create_user`; the `chris` superuser and
  root folders already exist because `core/apps.py` creates them on `post_migrate`.
- The plugin-instance and feed list views have a test built on
  `core.tests.query_counts.QueryCountAssertionsMixin` (`assertQueriesPerListItem`): it fails when
  a view's query count grows with the rows it serializes (N+1), beyond any per-row cost the test
  states (the descendant walk's one query per node). Their detail views use `assertNumQueries`.
  Give the added rows every relation the serializer reads, or a missing join goes unnoticed.

## Architecture

### Settings and entry points
- `config/settings/common.py` holds app/DRF/spectacular config; `local.py` (dev, `DEBUG=True`,
  hard-coded service hostnames that match compose network aliases: `chris_dev_db`,
  `pfcon.remote`, `nats`, `chris-broker`, `chris-cache`, `lldap`), `production.py`
  (everything via `get_secret` from env; missing vars raise `ImproperlyConfigured`),
  `benchmark.py` (local + env-driven DB pool). Both `local` and `production` verify the storage
  connection at import time, and the cache's (`production` only with a `CACHE_URL`) with
  `core.cache.verify_cache_connection`, which raises only for a wrong configuration and only
  warns when the cache server does not answer or is a full Dragonfly, so CUBE starts without it.
- `manage.py` and `core/celery.py` default to `config.settings.local`; `config/asgi.py`
  defaults to `production`. The ASGI app routes HTTP to Django and WebSockets through
  `core/websockets/` (token-in-query-string auth).
- `config/urls.py` mounts `core.api` under `/api/`, the admin-only plugin/compute-resource API
  under `/chris-admin/api/v1/`, and the drf-spectacular schema views. `core/api.py` is the single
  URL table for every app's views; serializers' `HyperlinkedRelatedField`s reference its names.

### Collection+JSON API pattern
- Views are DRF generic views. Every list view overrides `list()` to append a `queries` block
  (derived from the sibling `.../search/` view's `FilterSet`) and a write `template` via
  `collectionjson.services`. `FilterSet`s live in each app's `models.py` next to the model.
- `collectionjson/renderers.py` turns hyperlinked fields into `links`; `core.middleware.
  ResponseMiddleware` renders unhandled exceptions as Collection+JSON 500s in production (re-raises
  under `DEBUG`).
- OpenAPI: `get_queryset` methods guard with `if getattr(self, "swagger_fake_view", False)`;
  schema fix-ups live in `*/spectacular_hooks.py` and are listed in `SPECTACULAR_SETTINGS`.
- The plugin-instance and feed views fetch the related objects their serializer reads for every
  item in the list query: `PluginInstance.add_serializer_relations` (`select_related` over the
  model's `SERIALIZER_RELATIONS`, or `PARTIAL_SERIALIZER_RELATIONS` for the partial serializer)
  and `Feed.add_jobs_status_count`. A list built in Python (the descendants walk) uses
  `prefetch_serializer_relations` on its page instead. `PluginInstanceSerializer` reads its ids
  from the FK columns (`ReadOnlyField()` named `<fk>_id`), so they need no join.
- Auth: DRF token (`/api/v1/auth-token/`), basic, and session. File downloads and WebSockets use
  short-lived `downloadtokens` (JWT signed with a key derived from `SECRET_KEY`, see
  `core/utils.py`) passed as `?token=`. The superuser named `chris` is special-cased everywhere
  (`core.permissions.IsOwnerOrChris`, `core.models.user_can_access_obj`).

### Virtual filesystem (`core/models.py`)
- `ChrisFolder` (unique `path`, parent chain auto-created on save), `ChrisFile` (`fname` is the
  storage key), and `ChrisLinkFile` (`.chrislink` pointing at another path). Each carries owner,
  `public`, and per-user/per-group `r`/`w` permission tables. `validate_path_access` is the single
  path-authorization rule; use it rather than re-deriving access.
- Roots created on `post_migrate`: `home`, `SHARED`, `PUBLIC`, `PIPELINES`, `SERVICES/PACS`.
  `users.models.UserProxy.save` builds `home/<user>/{uploads,feeds}` plus `public`/`shared`
  link files for every new user (also via `CustomLDAPBackend`). Groups `all_users` and
  `pacs_users` gate access.
- Subtypes are proxies/subclasses of `ChrisFile`: `UserFile` (`home/...`), `PACSFile`
  (`SERVICES/PACS/...`), `PipelineSourceFile`. The `filebrowser` app exposes the tree generically.
  `post_delete` receivers delete the storage object, so deleting a model row deletes bytes.
- Storage backends are behind `core.storage.StorageManager` (Swift, S3, filesystem) chosen by
  `STORAGE_ENV`; get one with `connect_storage(settings)`. Files are immutable (WORM).

### Shared cache (`core/cache.py`)
- `CACHES['default']` is Django's `RedisCache` on a Dragonfly of its own, `chris-cache`,
  apart from the broker `chris-broker` (database 0 Celery, 1 reserved for event streams):
  `KEY_PREFIX` `cube`, 0.5 s socket timeouts, no retries. It runs with `--cache_mode` (evicts
  when full) and no snapshot (starts empty), so anything in it may vanish. `LOCATION` comes from
  `CUBE_CACHE_URL` (local and benchmark, default `redis://chris-cache:6379/0`) or
  `CACHE_URL` (production, optional until HARBOR-planning #152 needs it: without one the
  backend is a `DummyCache`, nothing connects, and the helpers go ahead degraded, with one
  WARNING per process at the first call). The start-up check refuses, among the mistakes its
  docstring lists, the broker's host and port as written (whatever the database; an IP or an
  alias gets past it), socket timeouts in the URL outside (0, 5] s, and a server that refuses
  the helpers' two scripts (it loads and runs each once on keys of the helpers' namespace). A
  server that does not answer only gets a warning, as do most TLS mistakes (a TLS-only
  Dragonfly answers a plain-text URL's password with an error, which is refused).
  The code speaks only the Redis protocol: Redis 7+ and Valkey 8 pass
  `core.tests.test_cache` too; for them, the compose service needs `--maxmemory`, an
  `allkeys-*` policy and no persistence (see the comment on `chris-cache`).
- Coordinate processes through its helpers rather than a Redis client of your own:
  `acquire_lock`/`release_lock` (the release checks the owner's token), `mark_once`/
  `clear_mark`, and `acquire_slot`/`release_slot`, a bounded counter of expiring leases (a
  Lua script on a sorted set). TTLs are seconds, applied in milliseconds.
- The helpers fail open: when the cache does not answer or refuses a command, an acquire
  returns a degraded `Lease` and `mark_once` returns true, so what they guard must be safe to
  run twice. When it does not answer each process skips it for 5 s, then lets one call try it
  while the others keep skipping until that call's outcome is known; it logs one WARNING per
  outage and one INFO when the cache answers again, and replays the releases and clears it
  could not send, at most 256 per helper call in one pipeline. An error reply (out of memory, a key of the wrong type) is logged at ERROR
  and does not count as an outage (it ends one: the cache answered); the ones redis-py raises
  as connection errors (authentication, loading, too many clients) do.
- Django's `RedisCache` builds a new connection pool, and so a new connection, for each request
  of the API (Django ticket #35651); Celery workers keep one per process.

### Feeds, plugin instances, and job orchestration
- A `Feed` is a DAG of `PluginInstance`s. Plugin types: `fs` (creates a feed), `ds` (one
  `previous`), `ts` (topological, multiple parents listed in its `plugininstances` string param).
  `PluginInstance.save` creates the feed and the output `ChrisFolder` at
  `home/<owner>/feeds/feed_<id>/<plugin>_<id>/.../data`; the folder is only null transiently.
- `PluginInstance.filter_visible_to_user` filters a plugin-instance queryset down to what a user
  may read (public feed, feed owner, feed shared with the user or one of their groups; `chris`
  sees all). It matches the feed against a subquery, so a feed shared several times doesn't
  repeat rows. List views use it rather than re-deriving the lookup; single instances are
  checked by the permission classes in `plugininstances/permissions.py`.
- Status state machine in `plugininstances/enums.py` (`created → waiting → copying → scheduled →
  started → uploading → registeringFiles → finishedSuccessfully | finishedWithError |
  cancelled`). `plugininstances.utils.run_if_ready` decides whether a new instance runs now or
  waits on its parents; the `periodic` beat tasks in `plugininstances/tasks.py` advance waiting
  instances, poll running ones, and cancel stuck ones.
- Remote execution goes through pfcon via `plugininstances/services/`: `abstractjobs.
  PluginInstanceJob` handles auth-token refresh and job id prefixing; `PluginInstanceAppJob`,
  `PluginInstanceCopyJob`, `PluginInstanceUploadJob`, `PluginInstanceDeleteJob` are the
  concrete flows. `ComputeResource` flags
  (`compute_innetwork`, `compute_requires_copy_job`, `compute_requires_upload_job`) select which
  jobs run. Post-terminal cleanup is tracked separately in `remote_cleanup_status`. Errors are
  logged and stored as `CODEnn` codes on the instance.
- Celery: queues `main1`, `main2`, `periodic` with explicit `task_routes` and the beat schedule in
  `core/celery.py` (`CUBE_CELERY_POLL_INTERVAL` env var). Compose runs `worker-mains`,
  `worker-periodic`, and `celery-scheduler` (django-celery-beat DatabaseScheduler).
- Deletion of feeds, folders, plugin instances, and PACS series is asynchronous:
  `AsyncDeletableModel.mark_deletion_pending()` then a `delete_*` task; the tasks are idempotent
  and record failures in `deletion_status`/`deletion_error`.

### Plugins, pipelines, workflows
- Plugins are registered from a ChRIS-store-style JSON representation through the
  `/chris-admin/api/v1/` API, the Django admin, or `plugins/services/manager.py` (CLI). In dev,
  chrisomatic (`chrisomatic/chrisomatic.yml`) registers the `host` compute resource, users, and
  the plugin list on every `just`. `chrisomatic/postscript.yml` is an optional larger set.
  `just bench-start` registers the benchmark's workload plugins instead from the committed,
  digest-pinned `benchmarks/chrisomatic/benchmark.yml` (or another file there it is given, such
  as a git-ignored `*.local.yml`), and
  the harness refuses to run unless CUBE holds exactly those registrations
  (`benchmarks/envelope.md` § "Pinned workload plugin versions").
- `Pipeline` = template graph of `PluginPiping`s with default piping parameters; a `Workflow`
  instantiates it into plugin instances (`workflows/_types.py` resolves per-node overrides).
  `core/graph.py` provides the DAG/cycle checks. `plugins/fields.py` defines `CPUInt`/`MemoryInt`
  (values like `1000m`, `512Mi`).

### PACS and DICOMweb
- `pacsfiles`: `PACS`, `PACSQuery`/`PACSRetrieve` (sent to pfdcm via `pacsfiles/services.py`),
  `PACSSeries`, `PACSFile`. oxidicom writes files to storage and then calls the
  `register_pacs_series` Celery task. Retrieval progress is streamed from NATS using LONK
  (`pacsfiles/lonk.py`) over the WebSocket route `v1/pacs/ws/` and the SSE view `v1/pacs/sse/`.
- `dicomweb`: `PACSStudy` and `PACSInstance` index rows for QIDO-RS, populated at ingest by
  `dicomweb.tasks.index_pacs_instance`; study counters are maintained by signals with `F()`
  updates and rely on `pg_trgm` (threshold set per connection via `DATABASES OPTIONS`).

## Gotchas

- `postgres/cube.conf` is the `db` server's configuration file: both compose files start it
  with `-c config_file=/etc/postgresql/cube.conf` (the benchmark override repeats that command
  line and adds `-c` flags), so the file replaces initdb's and restates `listen_addresses` and
  the locale lines. After editing it, restart the whole stack, not `db` alone (CUBE's pools
  would keep dead connections). `work_mem` there is why `db` needs `shm_size`.
- Keep the broker and the cache apart: one Dragonfly has one memory limit and one eviction
  policy for all its logical databases, so a full cache in the broker's server would refuse
  Celery's writes, and `--cache_mode` there would evict Celery queues. The broker `chris-broker`
  never evicts and snapshots to the `chris_broker_data` volume when it stops, one snapshot
  (`--dbfilename=dump`) overwritten each time (queues survive restarts); `chris-cache` evicts
  and keeps nothing (its `/data` is a tmpfs). `CUBE_BROKER_IMAGE` and `CUBE_CACHE_IMAGE`
  replace their images; the command lines are Dragonfly's. `cache.clear()` flushes the cache's
  whole database whatever the key prefix. Dragonfly's Lua scripts may only touch the keys they
  declare.
- `pyproject.toml` constraints are lower bounds; `uv.lock` is the source of truth. Django is
  capped `<6.1` because django-celery-beat 2.9 requires it. After any pyproject change run
  `just lock` and commit the lockfile, otherwise CI and the image build fail.
- `chris_backend/__version__.py` is a placeholder that CI overwrites from the git tag; do not
  edit it by hand.
- Codacy (`gh/HARBOR-Medical-AI`) fails a pull request on any new issue. Its Pylint passes no
  `--rcfile`, so a root `pylintrc`, `.pylintrc`, `setup.cfg` or `[tool.pylint]` would change
  what it reports, and its Bandit reads `pyproject.toml`, which has no `[tool.bandit]`: every
  test runs, B101 in tests too. Silence a false positive with `# nosec <ID> - <reason>` (new
  pytest asserts in `benchmarks/tests` carry `# nosec B101 - pytest assertion`; older ones are
  issues Codacy already has), or for Opengrep, whose
  ~1,400 rules `just lint` doesn't run, `nosemgrep` in a comment on the reported line.
  `scripts/lint.py` copies Codacy's 80 Pylint rules; keep them in step with its standard.
- The `README.md` "Documentation" section (Sphinx under `docs/`) is marked outdated upstream; the
  OpenAPI schema generated by `just openapi` is the current API reference.
- `docker-compose.yml` comments flag hostnames and ports that are hard-coded in
  `config/settings/local.py`, `chrisomatic/*.yml`, `benchmarks/chrisomatic/*.yml` and, for the
  two Dragonfly services, the benchmark harness; change them together.
- Both Dockerfiles bind-mount `pyproject.toml`/`uv.lock` (`RUN --mount=type=bind`), so
  Podman must be 5.4 or above: `just check-builder` (a dependency of `build` and the
  `bench-*` recipes that build) refuses older Podman, and its comment says why. CI runs on
  `ubuntu-26.04` (Podman 5.7), a version the README supports. On Ubuntu 26.04 the action's
  `githubActions/main.js` adds a missing AppArmor rule for `pasta` before a podman run.
- `UID`/`GID` in the compose files (every `user: ${UID}:${GID}`, and pfcon's
  `CONTAINER_USER`) come from `just get-user`: the invoking user's ids, except `0:0` under
  rootless Podman, where container root is the invoking user and any other uid cannot write
  into the checkout.
