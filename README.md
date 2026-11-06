# ![ChRIS logo](./docs/assets/logo_chris.png) ChRIS\_ultron\_backEnd

[![Build](https://github.com/HARBOR-Medical-AI/ChRIS_ultron_backEnd/actions/workflows/ci.yml/badge.svg)](https://github.com/HARBOR-Medical-AI/ChRIS_ultron_backEnd/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/HARBOR-Medical-AI/ChRIS_ultron_backEnd.svg)](./LICENSE)

_ChRIS_ is an open-source platform for containerized medical compute.

https://chrisproject.org/

## TL;DR

With [Docker Compose](https://docs.docker.com/compose/) and [just](https://just.systems/) installed, run

```shell
git clone https://github.com/HARBOR-Medical-AI/ChRIS_ultron_backEnd.git
cd ChRIS_ultron_backEnd
just
```

## Introduction

The _ChRIS_ backend, a.k.a. _ChRIS Ultron Backend_ or _**CUBE**_ for short,
is a component of the _ChRIS_ system. It is responsible for maintaining the database
of users, files, plugins, and pipelines.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://chrisproject.org/img/figures/ChRIS_architecture_dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="https://chrisproject.org/img/figures/ChRIS_architecture.svg">
  <img alt="Architecture diagram" src="https://chrisproject.org/img/figures/ChRIS_architecture.svg">
</picture>

Here lives the code of _CUBE_. It is a Django project using PostgreSQL and Celery.
The HTTP API primarily supports the [collection+json](http://amundsen.com/media-types/collection/) content-type.

## Development

Development is mainly supported on Linux. MacOS and WSL on Windows also work (because Docker Desktop is a Linux VM). You will need at least 8GM RAM, 20GB disk space, and a good internet connection.

Install Docker (version 27 or above) or Podman (version 5.4 or above), Docker Compose, and [just](https://github.com/casey/just?tab=readme-ov-file#installation).

<details>
<summary>
Docker Installation Instructions
</summary>

- For MacOS and Windows, see: https://docs.docker.com/get-started/get-docker/
- For Linux, see: https://docs.docker.com/engine/install/

> [!CAUTION]
> On **Linux**, the official Docker Documentation will try to trick you into installing "Docker Desktop." Do not install "Docker Desktop." Look for "Docker Engine" instead.

> [!CAUTION]
> On **Ubuntu**, make sure you follow the instructions here: https://docs.docker.com/engine/install/ubuntu/. If you do not follow the instructions, Ubuntu will try to install Docker using snap, which will cause many problems.

</details>

<details>
<summary>
Podman Setup Instructions
</summary>

Rootless Podman is supported. Podman must be version 5.4 or above: the Dockerfiles bind-mount `pyproject.toml` and `uv.lock` but older Podman reuses a stale cached layer after they change, so a rebuild would silently keep the old dependencies. `just build`, and the benchmark recipes that build, refuse older Podman. You must install and configure Podman to use `docker-compose`, _not_ `podman-compose`. `podman-compose` is missing features, see issues [#575](https://github.com/containers/podman-compose/issues/575) and [#866](https://github.com/containers/podman-compose/issues/866).

A Podman daemon must be running, because _ChRIS_ runs containers of its own. To start the Podman daemon on Linux, run

```shell
systemctl --user start podman.service
```

If both Podman and Docker are installed, Podman will be used by default. A preference to use either Podman or Docker can be set by running

```shell
just prefer podman  # or
just prefer docker
```

With Podman, DragonflyDB might fail to start. Simply retry the command. See https://github.com/HARBOR-Medical-AI/ChRIS_ultron_backEnd/issues/573

On Ubuntu 26.04, `just down` or `just nuke` can fail with "rootless netns: kill network process: permission denied": the AppArmor profile for `pasta` does not let it receive a signal from Podman ([Debian bug 1100135](https://bugs.debian.org/1100135)). Add the missing rule (running this again changes nothing):

```shell
profile=/etc/apparmor.d/usr.bin.pasta
if ! grep -q 'peer=podman' "$profile"; then
  sudo sed -i 's|^}|  signal (receive) peer=podman,\n}|' "$profile"
  sudo apparmor_parser -r "$profile"
fi
```

The profile is a configuration file of the `passt` package: a later update may ask whether to keep your edit, and taking the package's version drops the rule again.

Under rootless Podman, `just` runs the containers that otherwise run as your UID and GID (_CUBE_, its workers, the tool containers such as `uv`, and the plugin containers pfcon starts) as `root` inside the container, which Podman maps to your own user on the host. Any other user inside the container would map to a subordinate UID that cannot write into your checkout, so `just lock` or `just makemigrations` would fail with "Permission denied". With Docker, or rootful Podman, those containers run as your own UID and GID.

</details>

### Just Commands

Development is handled by [`just`](https://just.systems).
Running _CUBE_ in development mode is as-simple-as running the command:

```shell
just
```

The first run of `just` will take 5-20 minutes because it needs to pull and build container images. Subsequent runs should only take 1-5 minutes.

_CUBE_ is now running at http://localhost:8000/api/v1/. You can click around in the web browser. Alternatively, check out [chrs](https://chrisproject.org/docs/chrs) and/or [ChRIS\_ui](https://github.com/FNNDSC/ChRIS_ui).

Run tests:

```shell
just test-all                       # run all tests
just test-unit                      # run unit tests
just test-integration               # run integration tests
just test feeds.tests.test_views    # run chris_backend/feeds/tests/test_views.py
```

Shut down and clean up:

```shell
just nuke
```

List all `just` commands:

```shell
just --list --unsorted
```

### Just Commands for Object Storage Backend Environments (s3, swift)

Just use the same commands as explained above but explicitly set the storage env at the beginning of the session. For instance running _CUBE_ in development mode with S3 storage is as-simple-as running the command:

```shell
just set-storage s3 
just
```

You can later unset the storage env with:

```shell
just unset-storage
```

### Development Tips and Tricks

#### Recreate containers after changing `docker-compose.yml`

If you modify `docker-compose.yml` (or the override compose files when developing for other storage envs), you need to recreate/restart the affected services.

```shell
just up
```

#### Rebuild the image after changing package dependencies

If you modify `Dockerfile`, `pyproject.toml` or `uv.lock`, you need to rebuild the image and recreate your containers.

```shell
just build && just up
```

Dependencies are managed with [uv](https://docs.astral.sh/uv/) and the resolved versions
are committed in `uv.lock`. Change a version constraint in `pyproject.toml`, then refresh
the lockfiles:

```shell
just lock                    # re-resolve both CUBE and the benchmark harness
just lock-upgrade django     # bump a single dependency
just lock-check              # fail if a lockfile is stale (also runs in CI)
```

These run uv in a throwaway container, so no local uv installation is required. The
benchmark harness has its own `benchmarks/pyproject.toml` and `benchmarks/uv.lock`,
because `docker-compose.benchmark.yml` builds it with `context: ./benchmarks`.

#### Check for Codacy issues before pushing

[Codacy](https://app.codacy.com/gh/HARBOR-Medical-AI/ChRIS_ultron_backEnd/dashboard) fails a
pull request on any issue its merge-base (`git merge-base HEAD origin/master`) does not have.
`just lint` runs the same Python checks in the dev image (Bandit, pyflakes, dodgy and Pylint
with Codacy's rules, and `uv audit` for the lockfiles) on the files changed since the merge-base
with `origin/master` and lists only the new issues:

```shell
just lint                 # new issues on this branch, uncommitted changes included
just lint --all           # every issue in the changed files
just lint-fix             # let Ruff fix new unused imports and f-strings without placeholders
just lint-fix --unsafe    # ... and unused variables
just lint-test            # the tests of scripts/lint.py
```

An image built before these tools were added says so and needs `just build` first. `just lint`
finds nearly all of Codacy's issues but not those of Opengrep (unpinned GitHub
Actions, a Dockerfile without `USER`), ESLint, ShellCheck, Hadolint, markdownlint, PMD,
Agentlinter (which reads `CLAUDE.md`), Jackson Linter or Lizard. An advisory of a changed
lockfile that the merge-base has too is listed with a `?`: Codacy compares with its stored
analysis of master, which can predate the advisory, so it may still call it new; upgrading the
package clears it. Silence a false positive as the code does, e.g.
`# nosec B105 - a label, not a password`.

#### Trying HTTP requests from the CLI

For CLI tools, I recommend [xh](https://github.com/ducaale/xh) and [jnv](https://github.com/ynqa/jnv) or [jq](https://jqlang.github.io/jq/). Example:

```shell
xh -a chris:chris1234 :8000/api/v1/ | jnv
```

#### Interactive shell

It is often easiest to debug things using a shell.

```shell
just bash    # run bash in a container
# -- or --
just shell   # run a Python REPL
```

In the Python REPL, you can import models and interact with them. Here is some common starter code:

```python
from django.conf import settings
from django.contrib.auth.models import User, Group
from plugins.models import *
from plugininstances.models import *
from core.storage import connect_storage

storage = connect_storage(settings)
```

### IDE Setup

Visual Studio Code and PyCharm both support using Docker containers to run the Python interpreter. Run `just build`, then point your IDE to use the container image `localhost/fnndsc/cube:dev`.

Not all text editors support using Docker, or configuring the LSP might be inconvenient. In these cases, you need to install Python and the dependencies on-the-metal.

#### Installing Python Dependencies On-The-Metal

[Install uv](https://docs.astral.sh/uv/getting-started/installation/), then run

```shell
uv sync
```

`uv sync` creates `.venv/`, fetches Python 3.12 if you do not have it, and installs the
exact versions recorded in `uv.lock` — including the `dev` dependency group, which is
what an LSP wants.

Some dependencies (such as `python-ldap`) build C code during installation, requiring
`clang` and the OpenLDAP headers regardless of which installer you use. If that is
inconvenient, use `micromamba` to provide Python 3.12 and a prebuilt `python-ldap`, and
point uv at that environment for everything else.
[Install micromamba](https://mamba.readthedocs.io/en/latest/installation/micromamba-installation.html), then run

```shell
micromamba create -p ./.mambaenv -c conda-forge -y python=3.12 python-ldap=3.4
UV_PROJECT_ENVIRONMENT=./.mambaenv uv sync --inexact
```

`--inexact` leaves the packages micromamba installed alone instead of pruning them.
Now the environment is ready. Activate the environment

```shell
micromamba activate -p ./.mambaenv
```

And you are ready to run your LSP + text editor.

### OpenAPI Compatibility

[drf-spectacular](https://drf-spectacular.readthedocs.io/) is used to generate an OpenAPI schema.
Generally, its introspection is capable and accurate, but some special care is necessary to make
everything work. See:

- https://github.com/HARBOR-Medical-AI/ChRIS_ultron_backEnd/pull/584
- https://github.com/tfranzel/drf-spectacular/issues/1303#issuecomment-2387220962
- https://drf-spectacular.readthedocs.io/en/latest/faq.html#my-get-queryset-depends-on-some-attributes-not-available-at-schema-generation-time

Be sure to lint your OpenAPI annotations for errors and warnings by running:

```shell
just openapi > /dev/null
just openapi-split > /dev/null
```

## Production Deployment

See https://chrisproject.org/docs/run/helm

### Shared cache: `CACHE_URL` is optional for now

CUBE's shared cache (`core/cache.py`) runs on a Redis-protocol server of its own, whose URL the
API server, the Celery workers and beat all read from `CACHE_URL`. Nothing uses the cache yet,
so a deployment can leave `CACHE_URL` unset or empty: nothing is cached, no process connects to
a cache server, and the helpers would go ahead without their locks, markers and bounded
counters, logging one warning per process the first time something calls them. It will become
required with the first feature that uses the cache, so a deployment whose stack has no such
server, e.g. one with RabbitMQ as the Celery broker, should plan to add one.

When `CACHE_URL` is set, the server needs to be:

- **Its own server.** Not the Celery broker's, whatever the database: one server has one
  memory limit and one eviction policy, so a full cache would refuse the broker's writes and
  evicting would drop its queues. CUBE refuses to start on a URL with the broker's host and
  port; it compares them as written, so an IP address or an alias for the same server gets
  past this check.
- **Evicting and keeping nothing.** Anything in it may vanish. Dragonfly with
  `--cache_mode=true --dbfilename=` (as `chris-cache` in `docker-compose.yml`), or Redis 7+ or
  Valkey 8 with a `--maxmemory`, an `allkeys-*` eviction policy and no persistence
  (`--save '' --appendonly no`).
- **The URL.** `redis://[[user]:password@]host:6379/<db>`, `rediss://` for TLS, or
  `unix:///path/to.sock?db=<db>`, with a logical database. TLS options and socket timeouts
  (above 0, at most 5 s; 0.5 s by default) can go in its query string, retry options cannot.
  Percent-encode a `/`, `#`, `?` or `@` in the credentials.
- **Its permissions.** A user the server's ACL limits needs the commands the helpers send
  (`PING`, `SET`, `DEL`, `ZREM`, `SCRIPT LOAD`, `SCRIPT EXISTS` and `EVALSHA`, and `SELECT` when
  the database is not 0) and those their scripts run (`GET`, `DEL`, `TIME`, `PEXPIREAT` and the
  sorted-set commands), on the keys under CUBE's prefix (`cube:*`).

At start-up CUBE checks the server and refuses to start on a wrong configuration: another
cache backend, the broker's host and port, a URL it cannot parse or use or without a database,
retries or a timeout out of bounds, a refused password, or a server that refuses its scripts. A
server that does not answer, or a full Dragonfly, only gets a warning: CUBE starts without it,
and the helpers go ahead without their locks, markers and bounded counters until it answers.
Most TLS mistakes are in that case (a refused certificate, or `redis://` against `rediss://`), so
read that warning if the server should be up; a TLS-only Dragonfly answers a plain-text URL's
password with an error, which is refused.

## GitHub Actions

This repository can also be used as a GitHub Actions step for running _CUBE_ integration tests, e.g.

```yaml
name: CI

on:
  push:
    branches: [ master ]
  pull_request:
    branches: [ master ]

jobs:
  test:
    runs-on: ubuntu-26.04
    steps:
      - name: Run ChRIS backend integration tests
        uses: HARBOR-Medical-AI/ChRIS_ultron_backEnd@master
        # all inputs are optional
        with:
          engine: docker  # or podman
          command: test-integration  # or test-unit, ...
        # optionally change image used for pfcon, or cube
        env:
          CUBE_IMAGE: localhost/fnndsc/cube:dev
          PFCON_IMAGE: localhost/fnndsc/pfcon:dev
```

With `engine: podman`, start the Podman service in a step before this one (`systemctl --user start podman.service`). On Ubuntu 26.04 the action also adds the AppArmor rule for `pasta` described under the Podman setup instructions, when `sudo` needs no password, as on GitHub-hosted runners.

## Load & Scalability Benchmarks

A repo-native load & scalability benchmark harness lives under [`benchmarks/`](./benchmarks). It drives real plugin-instance DAGs end-to-end against the dev stack and **auto-escalates one factor at a time** (file count, concurrent feeds, chain depth, branching/merging) until _CUBE_ breaks, then writes a ticket-ready report with the breaking points and approach-to-failure curves.

```shell
just bench-start              # bring up the fslink stack with the uvicorn envelope + plugins
just bench-run --tier smoke   # ~2 min sanity sweep (also: default, full, aging-grow, aging-probe)
just bench-down               # stop the stack
```

See [`benchmarks/README.md`](./benchmarks/README.md) for usage and [`benchmarks/STRATEGY.md`](./benchmarks/STRATEGY.md) for the design.

## Documentation

> [!CAUTION]
> Everything below in this section is outdated.

#### REST API reference

Available [here](https://harbor-medical-ai.github.io/ChRIS_ultron_backEnd/).

Install Sphinx and the http extension (useful to document the REST API)
```
pip install Sphinx
pip install sphinxcontrib-httpdomain
```

Build the html documentation
```
cd docs/
make html
```

#### ChRIS REST API design.

Available [here](https://github.com/HARBOR-Medical-AI/ChRIS_ultron_backEnd/wiki/ChRIS-REST-API-design).

#### ChRIS backend database design.

Available [here](https://github.com/HARBOR-Medical-AI/ChRIS_ultron_backEnd/wiki/ChRIS-backend-database-design).
