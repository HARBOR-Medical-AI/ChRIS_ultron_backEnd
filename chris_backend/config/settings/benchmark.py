"""
Benchmark settings: the development stack with a production-like, *recorded* envelope.

Identical to ``local`` except that the artificially tiny dev DB connection pool is made an
explicit, env-driven knob, ``DEBUG`` is off unless ``CUBE_DEBUG`` asks for it, as in
production (which also drops the debug toolbar and serves static files without the
collectstatic manifest), and ``CUBE_DB_POOL_STATS_INTERVAL`` can make the API workers log
their pool counters. The benchmark measures CUBE's architecture capacity, so the pool,
``DEBUG``, the number of uvicorn workers (set on the ``chris`` service's command), and
Postgres ``max_connections`` (set on the ``db`` service's command) are all pinned in
``docker-compose.benchmark.yml`` and recorded in every report.

Normal development is unaffected — only the benchmark compose profile selects this module.
"""

import os

from .local import *  # noqa: F401,F403


options = DATABASES['default'].setdefault('OPTIONS', {})  # noqa: F405
options['pool'] = {
    'min_size': int(os.getenv('CUBE_DB_POOL_MIN_SIZE', '2')),
    'max_size': int(os.getenv('CUBE_DB_POOL_MAX_SIZE', '10')),
    'timeout': int(os.getenv('CUBE_DB_POOL_TIMEOUT', '10')),
}

# DEBUG wraps every SQL statement in a recorder, runs the debug toolbar's middleware on
# every request and renders a 500 page that re-runs the QuerySets in each stack frame: none
# of that is CUBE's production behaviour.
DEBUG = os.getenv('CUBE_DEBUG', 'false').lower() in ('1', 'true', 'yes')
if not DEBUG:
    INSTALLED_APPS = [app for app in INSTALLED_APPS if app != 'debug_toolbar']  # noqa: F405
    MIDDLEWARE = [m for m in MIDDLEWARE if not m.startswith('debug_toolbar.')]  # noqa: F405
    # Development images skip collectstatic, so the manifest storage would fail every
    # HTML page (browsable API, admin); serve static files from the apps instead.
    STORAGES = {  # noqa: F405
        **STORAGES,  # noqa: F405
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    }
    WHITENOISE_USE_FINDERS = True

# log each API worker's connection pool counters (see core.utils.db_pool_stats)
DB_POOL_STATS_INTERVAL = float(os.getenv('CUBE_DB_POOL_STATS_INTERVAL', '0'))

# The dev settings log the apps at DEBUG to the console and to /tmp/debug.log, a file that
# nothing rotates (916 MB inside worker-mains in one diamond · layers = 8 scenario): the
# benchmark logs them at INFO, to the console only, as production does.
LOGGING['handlers'].pop('file', None)  # noqa: F405
for _name, _logger in LOGGING['loggers'].items():  # noqa: F405
    if _name:  # the apps', not the root logger
        _logger.update(level='INFO', handlers=['console_verbose'])
