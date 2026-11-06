# This is a `justfile` for the development of ChRIS backend. For the most part,
# it is a wrapper around the `docker compose` or `podman compose` command.
# Use the `just` program to execute `justfile` commands. Run
#
#     just -l
#
# to list commands and their descriptions.
#
# The syntax and usage of `justfile` is similar to Makefile of GNU Make.
# For more information, see https://just.systems/man/en/chapter_1.html

# Storage mode: "fslink" (default), "swift" (Swift object storage), or "s3" (S3-compatible storage).
# Usage: just storage=swift dev
# Or persist the preference: just set-storage swift
storage := `if [ -f '.storage' ]; then cat .storage; else echo fslink; fi`

# Whether `compose run` should allocate a TTY, auto-detected from stdin.
# Interactive recipes (`just bash`, `just shell`) need one; non-interactive
# callers such as CI must not have one -- under `podman compose` on the GitHub
# Actions runner, allocating a TTY makes `compose run` hang forever after the
# image build. Force it off with `just tty=false ...`.
tty := `if [ -t 0 ]; then echo true; else echo false; fi`

# Start the ChRIS backend in development mode and then attach to the live-reloading server.
[group('(1) start-up')]
dev: chrisomatic attach

# Start the ChRIS backend in development mode.
[group('(1) start-up')]
start: start-ancillary migrate up

# Start services (without running database migrations).
[group('(1) start-up')]
up: (docker-compose '--profile=cube up -d')

# Attach to the chris container.
[group('(3) development')]
attach: (docker-compose '--profile=cube attach chris | grep -Fv "\"GET /api/v1/users/ HTTP/1.1\" 200"')

# `runserver` is WSGI here — channels is wired up in config/asgi.py, not INSTALLED_APPS —
# so it cannot serve WebSockets at all, and `channels.testing` exercises the consumers
# in-process. Neither reaches the uvicorn protocol stack production uses
# (websockets-sansio since uvicorn 0.50); this recipe does on the dev stack and dev
# settings. `just bench-start` also runs uvicorn but under the benchmark envelope.
# Publishes host port 8000 therefore stop the dev server (`just down`) before running it.
#
# Serve CUBE with uvicorn (ASGI) on port 8000 instead of the dev `runserver`.
[group('(3) development')]
dev-uvicorn *args: start-ancillary
    @just storage={{ storage }} docker-compose --profile=cube run --rm --service-ports \
        chris python3 -m uvicorn --host 0.0.0.0 --port 8000 config.asgi:application {{ args }}

# Open a Python shell.
[group('(3) development')]
shell: (run 'python manage.py shell')

# Open a Bash shell.
[group('(3) development')]
bash: (run 'bash')

# Run chrisomatic, a tool which adds plugins and users to CUBE.
[group('(1) start-up')]
chrisomatic *args: start
    @just storage={{ storage }} docker-compose --profile=cube run --rm chrisomatic chrisomatic {{ args }}

# Run chrisomatic with the contents of chrisomatic/postscript.yml
[group('(1) start-up')]
postscript: (chrisomatic 'postscript.yml')

# Perform database migrations.
[group('(1) start-up')]
migrate: (run 'python manage.py migrate --noinput')

# Create database migrations.
[group('(3) development')]
makemigrations: (run 'python manage.py makemigrations')

# Run tests, e.g. `just test pacsfiles`
[group('(3) development')]
test *args:
    @just storage={{ storage }} run python manage.py test --force-color {{ args }}

# Run all tests.
[group('(3) development')]
test-all: test-unit test-integration

# Run unit tests.
[group('(3) development')]
test-unit: start-ancillary (run 'python manage.py test --force-color --exclude-tag integration')

# Run integration tests.
[group('(3) development')]
test-integration: start-ancillary (run 'python manage.py test --force-color --tag integration')

# Run the full test suite under coverage and print a report.
[group('(3) development')]
test-coverage: start-ancillary
    @just storage={{ storage }} run coverage erase
    @just storage={{ storage }} run coverage run manage.py test --force-color
    @just storage={{ storage }} run coverage report

# Run unit tests under coverage and write a Cobertura XML report to chris_backend/coverage.xml.
[group('(3) development')]
test-unit-coverage: start-ancillary
    @just storage={{ storage }} run coverage erase
    @just storage={{ storage }} run coverage run manage.py test --force-color --exclude-tag integration
    @just storage={{ storage }} run coverage xml

# Codacy fails a pull request on any issue its merge-base (git) does not have.
# `lint` runs the checks behind nearly all of Codacy's findings (Bandit, pyflakes, dodgy,
# Pylint, and `uv audit` for Trivy) on the files changed since the merge-base with
# origin/master and lists the issues only the working tree has, with the advisories of a
# changed lockfile that Codacy may call new all the same (marked ?); `--all` lists the
# older ones too, `--base` names another branch. See scripts/lint.py.
#
# List the issues Codacy would report as new on this branch.
[group('(3) development')]
lint *args: (lint-run 'check' args)

# Ruff fixes unused imports and f-strings without placeholders, with `--unsafe` unused
# variables too, only where `lint` reports them as new and never on an older issue's line.
#
# Fix the new issues Ruff can fix then list what is left.
[group('(3) development')]
lint-fix *args: (lint-run 'fix' args)

# Test scripts/lint.py (scripts/test_lint.py) in the dev image.
[group('(3) development')]
lint-test:
    @just storage={{ storage }} docker-compose --profile=cube run --rm --no-deps -T \
        -v '{{ justfile_directory() }}:/w:z' -w /w -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
        chris python -m unittest discover -s scripts -p 'test_*.py'

# Run scripts/lint.py in the dev image with the repository mounted at /w, and the host's
# global git excludes (core.excludesFile) so that the files they ignore stay ignored.
[group('helper function')]
lint-run command *args:
    @excludes="$(git config --path --get core.excludesFile || echo "${XDG_CONFIG_HOME:-$HOME/.config}/git/ignore")"; \
    just storage={{ storage }} docker-compose --profile=cube run --rm --no-deps -T \
        -v '{{ justfile_directory() }}:/w:z' -w /w -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
        $([ -f "$excludes" ] && echo "-v $excludes:/etc/git-excludes:ro -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=core.excludesFile -e GIT_CONFIG_VALUE_0=/etc/git-excludes") \
        chris python scripts/lint.py {{ command }} {{ args }}

# Start dependency services.
[group('(1) start-up')]
start-ancillary: (docker-compose 'up -d')

# Stop services.
[group('(2) shutdown')]
down: (docker-compose '--profile=cube --profile=tools down')

# Stop services and remove all data.
[group('(2) shutdown')]
nuke: reap-plugin-instances (docker-compose '--profile=cube --profile=tools down -v --remove-orphans')

# Remove all plugin instance containers.
[group('(2) shutdown')]
reap-plugin-instances: (docker-compose 'run --rm pfcon python -c' '''
        '
        import os
        import docker
        d = docker.from_env()
        filters = {"label": os.getenv("JOB_LABELS")}
        containers = d.containers.list(all=True, filters=filters)
        for container in containers:
            print(f"Removing container: {container.name} ({container.image})", flush=True)
            container.remove(force=True)
        '
    ''')

# (Re-)build the container image.
[group('(4) docker-compose')]
build: check-builder (docker-compose '--profile=cube build')

# Pull container images.
[group('(4) docker-compose')]
pull: (docker-compose 'pull')

# Get container logs.
[group('(4) docker-compose')]
logs *args:
    @just storage={{ storage }} docker-compose --profile=cube logs {{ args }}

# docker-compose ... run helper function.
[group('(4) docker-compose')]
run +command:
    @just storage={{ storage }} docker-compose --profile=cube run --rm {{ if tty == "false" { "-T" } else { "" } }} chris {{ command }}

# docker-compose ... helper function.
[group('(4) docker-compose')]
docker-compose +command:
    env $(just get-user) DOCKER_SOCK="$(just get-socket)" $(just get-engine) compose {{ if storage == "swift" { "-f docker-compose.yml -f docker-compose_swift.yml" } else if storage == "s3" { "-f docker-compose.yml -f docker-compose_s3.yml" } else { "" } }} {{ command }}

# Get the container engine to use (docker or podman)
[group('helper function')]
get-engine:
    @if [ -f '.preference' ]; then           \
      cat .preference && exit 0;             \
    elif type podman > /dev/null 2>&1; then  \
      echo podman;                           \
    else                                     \
      echo docker;                           \
    fi                                       \

# This is every `user: ${UID}:${GID}` in the compose files and pfcon's CONTAINER_USER for
# plugin jobs. Under rootless Podman a container's root *is* the invoking user and any
# other uid maps to a subordinate uid that cannot write into the working tree (`just lock`,
# `just makemigrations`, coverage), so there it is root. Everywhere else it is the invoking
# user.
#
# Get the UID and GID that compose runs CUBE's containers as.
[group('helper function')]
get-user:
    @if [ "$(just get-engine)" = 'podman' ] \
        && [ "$(podman info --format '{{{{ .Host.Security.Rootless }}')" = 'true' ]; then \
      echo 'UID=0 GID=0';                                                                 \
    else                                                                                  \
      echo "UID=$(id -u) GID=$(id -g)";                                                   \
    fi

# Podman must be 5.4 or above (the Dockerfiles, README, CLAUDE.md and ci.yml point
# here): both Dockerfiles bind-mount pyproject.toml and uv.lock but Buildah before 1.39
# (the one in Podman before 5.4) leaves bind-mounted content out of its layer cache key,
# so a rebuild after a lock change reuses the old layer and silently keeps the old
# dependencies (containers/buildah#5400). `podman info` reports the Buildah that builds,
# the server's when Podman is remote. The recipes that rebuild an image depend on this.
# `run` and `up` only build a missing image; on older Podman that build can still reuse a
# cached layer, a corner not worth gating every recipe for.
#
# Fail if the container engine cannot rebuild CUBE's images correctly.
[group('helper function')]
check-builder:
    @if [ "$(just get-engine)" = 'podman' ]; then                                                \
      v="$(podman info --format '{{{{ .Version.Version }} {{{{ .Host.BuildahVersion }}' 2>/dev/null)"; \
      if [ -z "$v" ]; then                                                                       \
        >&2 echo "Cannot tell which Buildah builds CUBE's images: 'podman info' failed.";        \
        exit 1;                                                                                  \
      fi;                                                                                        \
      if [ "$(printf '%s\n' 1.39.0 "${v#* }" | sort -V | head -n 1)" != '1.39.0' ]; then         \
        >&2 echo "Podman ${v% *} (Buildah ${v#* }) is too old to rebuild CUBE's images:"         \
          "Podman 5.4 or above is required, or use Docker ('just prefer docker').";              \
        exit 1;                                                                                  \
      fi;                                                                                        \
    fi

# chrisomatic sees benchmarks/chrisomatic/ only (docker-compose.benchmark.yml) so a pin
# is a file name there.
#
# Fail unless a benchmark pin names a file in benchmarks/chrisomatic/.
[group('helper function')]
check-pins pins:
    @case '{{ pins }}' in ''|*/*) >&2 echo "a pin is a file name in benchmarks/chrisomatic/, not '{{ pins }}'"; exit 1;; esac
    @test -f 'benchmarks/chrisomatic/{{ pins }}' || { >&2 echo "no such pin: benchmarks/chrisomatic/{{ pins }}"; exit 1; }

# Get the docker daemon socket path.
[group('helper function')]
get-socket:
    @if [ "$(just get-engine)" = 'podman' ]; then     \
      just get-podman-socket;                         \
    else                                              \
      echo '/var/run/docker.sock';                    \
    fi

# Get the podman daemon socket path.
[group('helper function')]
get-podman-socket: check-podman-socket
    @podman info --format '{{{{ .Host.RemoteSocket.Path }}'

# Ensure that the podman daemon is running.
[group('helper function')]
check-podman-socket:
    @if [ "$(podman info --format '{{{{ .Host.RemoteSocket.Exists }}')" != 'true' ]; then   \
      cmd='systemctl --user start podman.service';                                          \
      >&2 echo "Podman daemon not running. Please run \`$(tput bold)$cmd$(tput sgr0)\`";    \
      exit 1;                                                                               \
    fi

# Set a preference for using either Docker or Podman.
[group('(5) docker/podman preference')]
prefer docker_or_podman:
    @[ '{{ docker_or_podman }}' = 'docker' ] || [ '{{ docker_or_podman }}' = 'podman' ] \
        || ( \
            >&2 echo 'argument must be either "docker" or "podman"'; \
            exit 1 \
        )
    echo '{{ docker_or_podman }}' > .preference

# Remove your preference for Docker or Podman.
[group('(5) docker/podman preference')]
unset-preference:
    rm -f .preference

# Set a preference for storage mode ("fslink", "swift", or "s3").
[group('(5) docker/podman preference')]
set-storage mode:
    @[ '{{ mode }}' = 'fslink' ] || [ '{{ mode }}' = 'swift' ] || [ '{{ mode }}' = 's3' ] \
        || ( \
            >&2 echo 'argument must be "fslink", "swift", or "s3"'; \
            exit 1 \
        )
    echo '{{ mode }}' > .storage

# Remove your preference for storage mode.
[group('(5) docker/podman preference')]
unset-storage:
    rm -f .storage

# Compose helper for the benchmark stack (fslink + uvicorn-envelope override).
[group('(6) benchmarks')]
bench-compose +command:
    env $(just get-user) DOCKER_SOCK="$(just get-socket)" \
        DOCKER_GID="$(stat -c '%g' "$(just get-socket)" 2>/dev/null || stat -f '%g' "$(just get-socket)" 2>/dev/null || echo 0)" \
        COMPOSE_PROJECT_NAME="$(basename "$(pwd)" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9_-')" \
        GIT_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)" \
        GIT_DIRTY="$(git diff --quiet HEAD 2>/dev/null && echo false || echo true)" \
        $(just get-engine) compose -f docker-compose.yml -f docker-compose.benchmark.yml {{ command }}

# The pin is a file in benchmarks/chrisomatic/: the committed benchmark.yml unless named,
# e.g. an arm64 laptop's own file (see benchmarks/envelope.md § "Pinned workload plugin
# versions"); `just bench-run --plugins` names the same file. chrisomatic fails when the
# database already holds another registration of a pinned version, e.g. from a dev `just`:
# the two stacks share their volumes.
#
# Start the benchmark stack (fslink + uvicorn envelope), migrate and then register the pinned workload plugins.
[group('(6) benchmarks')]
bench-start pins='benchmark.yml': check-builder (bench-pull-plugins pins)
    just bench-compose 'up -d --build db chris-broker chris-cache nats pfcon cube-nonroot-user-volume-fix'
    just bench-compose 'run --rm chris python manage.py migrate --noinput'
    just bench-compose 'up -d --build chris worker-mains worker-periodic celery-scheduler'
    just bench-compose '--profile cube run --rm chrisomatic chrisomatic /etc/chrisomatic-benchmark/{{ pins }}' \
        || { >&2 echo "[bench] chrisomatic failed. If a dev 'just' ran on these volumes, reset them with 'just bench-nuke' (it deletes the dev stack's data too) and run 'just bench-start {{ pins }}' again."; exit 1; }

# chrisomatic 1.0.0 cannot pull an image by digest but registers one that is present, so
# pull what the pin names first. A reference that is present is never pulled again.
# A name without a registry is pulled from docker.io explicitly (docker.io/library/ for a
# one-part name), so Podman need not resolve it. A pin keeps each `dock_image:` on a line
# of its own (chrisomatic's YAML forbids flow style); a comment after it is ignored.
#
# Pull the workload plugin images a benchmark pin names unless present.
[group('(6) benchmarks')]
bench-pull-plugins pins='benchmark.yml': (check-pins pins)
    @for ref in $(sed -n -e 's/[[:space:]]*#.*$//' -e 's/[[:space:]]*$//' \
        -e 's/^[ -]*dock_image:[[:space:]]*//p' 'benchmarks/chrisomatic/{{ pins }}' | tr -d "\"'"); do \
      $(just get-engine) image inspect "$ref" > /dev/null 2>&1 && continue; \
      case "$ref" in */*) first="${ref%%/*}";; *) first="";; esac; \
      case "$first" in \
        *.*|*:*|localhost) src="$ref";; '') src="docker.io/library/$ref";; *) src="docker.io/$ref";; \
      esac; \
      $(just get-engine) pull "$src" || exit 1; \
    done

# The dev and benchmark stacks are one compose project so this deletes the dev stack's data
# too (database, files, lldap, Orthanc, pfdcm). Stopping the stack first means no plugin-job
# container is created after `nuke` reaps them.
#
# Stop the benchmark stack, then remove all its data and plugin-job containers.
[group('(6) benchmarks')]
bench-nuke: bench-down nuke

# Run the benchmark harness, e.g. `just bench-run --tier smoke` or `just bench-run --tier full`.
[group('(6) benchmarks')]
bench-run *args: check-builder
    just bench-compose '--profile cube --profile bench build benchmark'
    just bench-compose '--profile cube --profile bench run --rm benchmark python -m benchmarks.run_bench {{ args }}'

# Run the harness unit tests inside the benchmark image.
[group('(6) benchmarks')]
bench-test *args: check-builder
    just bench-compose '--profile cube --profile bench build benchmark'
    just bench-compose '--profile cube --profile bench run --rm --no-deps benchmark python -m pytest -p no:cacheprovider benchmarks/tests {{ args }}'

# Control-plane RED load test (Locust). Args pass through, e.g.
# `just bench-locust '-u 100 -r 20 -t 3m'`. CSVs land in benchmarks/results/locust/.
[group('(6) benchmarks')]
bench-locust *args: check-builder
    just bench-compose '--profile cube --profile bench build benchmark'
    mkdir -p benchmarks/results/locust
    just bench-compose '--profile cube --profile bench run --rm benchmark locust -f benchmarks/locustfile.py --headless --csv /app/benchmarks/results/locust/run {{ args }}'

# Re-render a report from a results run id, e.g. `just bench-report 2026-06-09T153000Z`.
[group('(6) benchmarks')]
bench-report run_id:
    just bench-compose '--profile cube --profile bench run --rm --no-deps benchmark python -m benchmarks.report /app/benchmarks/results/{{ run_id }}'

# Compare runs over time: first = baseline, last = candidate, 3+ adds a trend table.
# Run ids resolve against results/ and the committed history/ archive, e.g.
# `just bench-compare 2026-06-09T153000Z 2026-06-10T214526Z --fail-on-regression`.
[group('(6) benchmarks')]
bench-compare *args:
    just bench-compose '--profile cube --profile bench run --rm --no-deps benchmark python -m benchmarks.compare {{ args }}'

# Keep a run's small artifacts (summary, levels, environment, rendered report) in
# version control so there is a human-readable history to compare against, e.g.
# `just bench-archive 2026-06-10T214526Z`.
[group('(6) benchmarks')]
bench-archive run_id:
    mkdir -p benchmarks/history/{{ run_id }}
    cp benchmarks/results/{{ run_id }}/summary.json benchmarks/history/{{ run_id }}/
    cp benchmarks/results/{{ run_id }}/environment.json benchmarks/history/{{ run_id }}/ 2>/dev/null || true
    cp benchmarks/results/{{ run_id }}/levels.jsonl benchmarks/history/{{ run_id }}/ 2>/dev/null || true
    cp benchmarks/results/{{ run_id }}/report.md benchmarks/history/{{ run_id }}/ 2>/dev/null || true
    @echo "archived to benchmarks/history/{{ run_id }} — commit it with the change it validates"

# Open a shell in the benchmark container (debugging).
[group('(6) benchmarks')]
bench-bash:
    just bench-compose '--profile cube --profile bench run --rm --no-deps benchmark bash'

# Stop the benchmark stack.
[group('(6) benchmarks')]
bench-down:
    just bench-compose '--profile cube --profile bench down'

# Print the OpenAPI schema via drf-spectacular.
[group('(3) development')]
openapi:
    @just storage={{ storage }} run python manage.py spectacular --color

# Print the OpenAPI schema using drf-spectacular, using workarounds for more
# reliable client generation.
[group('(3) development')]
openapi-split:
    env SPECTACULAR_SPLIT_REQUEST=true just storage={{ storage }} openapi

# Run uv in a throwaway container, so no local uv installation is required.
[group('helper function')]
uv +args:
    @just storage={{ storage }} docker-compose run --rm {{ if tty == "false" { "-T" } else { "" } }} uv uv {{ args }}

# Refresh both lockfiles (CUBE and the benchmark harness).
[group('(3) development')]
lock: (uv 'lock') (uv 'lock --directory benchmarks')

# Upgrade one dependency, e.g. `just lock-upgrade django`.
[group('(3) development')]
lock-upgrade package: (uv 'lock --upgrade-package' package)

# Fail if either lockfile is out of date with its pyproject.toml.
[group('(3) development')]
lock-check: (uv 'lock --check') (uv 'lock --check --directory benchmarks')
