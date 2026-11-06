"""
The shared cache and the coordination helpers built on it.

``CACHES['default']`` is Django's ``RedisCache`` on a Redis-protocol server of its own
(``chris-cache`` in the compose stacks, a Dragonfly) apart from the Celery broker's: it
evicts when it is full and starts empty after a restart so whatever it holds may go
missing at any time. All coordination keys created by these helpers expire automatically.
Do not call cache.clear(): it deletes every key in this cache's Redis logical database,
not just keys with this application's prefix (Redis/Dragonfly FLUSHDB). A production
deployment without ``CACHE_URL`` has a ``DummyCache`` instead: nothing is cached, and the
helpers go ahead without a server.

The helpers, all safe to call from any thread or process:

- ``acquire_lock`` / ``release_lock``: a lock held until released or for ``ttl`` seconds
- ``mark_once`` / ``clear_mark``: true for the first caller in a window of ``ttl`` seconds
- ``acquire_slot`` / ``release_slot``: a bounded counter, at most ``ceiling`` leases of
  ``ttl`` seconds each, so a lease its holder never releases frees itself

They fail open. When the cache does not answer or refuses a command they go ahead as if
it had granted it, so whatever a lock or marker guards must stay correct when it runs
twice. When it does not answer they also log the degraded mode once and stop trying for a
few seconds, after which one call tries it while the others go on skipping it. A
release or a clear that cannot reach the cache is kept and the process's next helper calls
after the cache answers again replay it, a bounded batch each; a process that makes no
further call leaves it to its TTL.
"""

import contextlib
import logging
import math
import os
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass

import redis
from redis.commands.core import Script
from django.core.cache import caches
from django.core.cache.backends.dummy import DummyCache
from django.core.cache.backends.redis import RedisCache, RedisCacheClient
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver
from django.utils.module_loading import import_string


logger = logging.getLogger(__name__)

#: the longest lease, lock or marker the helpers accept, in seconds
MAX_TTL = 7 * 24 * 3600

#: the longest socket timeout the start-up check accepts in a cache URL, in seconds (the
#: settings set 0.5 s; a query string overrides them)
MAX_SOCKET_TIMEOUT = 5.0

# The scripts return integers only and stay within Lua 5.1 (real Redis) as well as the
# 5.4 that Dragonfly runs. Dragonfly runs a script only on the keys it declares.
_RELEASE_LOCK = Script(None, b"""
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
""")

# A sorted set of leases scored by their expiry in server milliseconds: drop the expired
# ones, admit when fewer than the ceiling (ARGV[1]) are left and keep the set as long as
# its longest lease.
_ACQUIRE_SLOT = Script(None, b"""
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then
  return 0
end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[3])
local last = redis.call('ZRANGE', KEYS[1], -1, -1, 'WITHSCORES')
redis.call('PEXPIREAT', KEYS[1], last[2])
return 1
""")

# both of them, for the start-up check to load whatever replaces the names above in a test
_SCRIPTS = (_RELEASE_LOCK, _ACQUIRE_SLOT)

# the cache does not answer: a connection or a reply failed, or timed out
_UNREACHABLE = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)

# what _call returns instead of a reply: nothing was sent or no usable reply came back
_SKIPPED = object()
_FAILED = object()

# the operation a kept release replays, by its kind, as the live call names it
_RELEASE_OPS = {'lock': 'release_lock', 'slot': 'release_slot', 'mark': 'clear_mark'}


@dataclass(frozen=True)
class Lease:
    """
    A lock or slot this process acquired.

    ``degraded`` is true when the cache did not answer the acquire or refused it so
    the caller goes ahead without the guarantee. ``token`` is None when the acquire
    sent nothing, the helpers being in their pause after a failure or no cache being
    configured; otherwise the release removes the lease by its token which also covers
    a write the server applied after its reply timed out.
    """
    kind: str  # 'lock' or 'slot'
    key: str
    token: int | None
    degraded: bool = False


def acquire_lock(name, ttl):
    """
    Take the lock ``name`` for ``ttl`` seconds (at most, ``release_lock`` frees it
    sooner).

    Returns a ``Lease`` or None when another holder has the lock. When the cache does
    not answer or refuses the command returns a degraded ``Lease``: the caller goes ahead
    as if it held the lock.
    """
    px = _milliseconds(ttl)
    key = _cache().make_key(f'lock:{name}')
    token = _new_token()
    reply = _call('acquire_lock', lambda client: client.set(key, token, nx=True, px=px))
    return _lease('lock', key, token, reply)


def release_lock(lease):
    """
    Release a lock ``acquire_lock`` returned unless it has expired and changed hands
    since.

    Returns whether this call freed the lock.
    """
    return _release(lease, 'lock')


def mark_once(key, ttl):
    """
    Whether this is the first call for ``key`` in the last ``ttl`` seconds (since a
    ``clear_mark``). True when the cache does not answer or refuses the command.
    """
    px = _milliseconds(ttl)
    cache_key = _cache().make_key(f'mark:{key}')
    reply = _call('mark_once', lambda client: client.set(cache_key, 1, nx=True, px=px))
    return reply is _SKIPPED or reply is _FAILED or bool(reply)


def clear_mark(key):
    """
    Let the next ``mark_once`` for ``key`` return true.

    A clear that an outage kept for later is replayed without a token so it can end a
    window that another caller opened once the cache answered again: that caller's work
    then runs twice at worst.
    """
    cache_key = _cache().make_key(f'mark:{key}')
    _call('clear_mark', lambda client: client.delete(cache_key),
          defer=('mark', cache_key, None))


def acquire_slot(name, ceiling, ttl):
    """
    Take one of the ``ceiling`` slots of ``name`` for ``ttl`` seconds (at most,
    ``release_slot`` frees it sooner).

    Returns a ``Lease``, or None when all of them are taken. When the cache does not
    answer or refuses the script returns a degraded ``Lease``: the caller goes ahead
    without the bound.
    """
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling < 1:
        raise ValueError(f'ceiling must be a positive integer, not {ceiling!r}')

    px = _milliseconds(ttl)
    key = _cache().make_key(f'slots:{name}')
    token = _new_token()

    reply = _call('acquire_slot', lambda client: _ACQUIRE_SLOT(
        keys=[key], args=[ceiling, px, token], client=client))
    return _lease('slot', key, token, reply)


def release_slot(lease):
    """
    Release a slot ``acquire_slot`` returned. Returns whether this call freed it.
    """
    return _release(lease, 'slot')


def verify_cache_connection(cache_settings, broker_url=None):
    """
    Check ``CACHES['default']`` at start-up like ``verify_storage_connection`` checks the
    storage.

    Raises ``ImproperlyConfigured`` when the configuration is wrong: another backend than
    Django's ``RedisCache``, no server or an empty one, a URL redis-py cannot parse or use,
    no logical database in it, retries or a socket timeout outside (0, MAX_SOCKET_TIMEOUT]
    in its query string, the Celery broker's host and port as written (names are not
    resolved, so an IP address or an alias gets past this), a refused password, a database
    the server does not have, or a server that refuses the helpers' scripts. Only logs a
    warning when the server does not answer, so that CUBE starts while it is down and the
    helpers fail open, or when it refuses the scripts because it is full. That warning
    covers most TLS mistakes (a refused certificate, or a scheme the server does not
    speak), but a TLS-only Dragonfly answers the password of a plain-text URL with an
    error, which raises. Says nothing otherwise.
    """
    backend = cache_settings.get('BACKEND', '')

    try:
        backend_class = import_string(backend)
    except ImportError as e:
        raise ImproperlyConfigured(f"CACHES['default'] BACKEND {backend!r}: {e}")

    if not (isinstance(backend_class, type) and issubclass(backend_class, RedisCache)):
        raise ImproperlyConfigured(f"CACHES['default'] BACKEND is {backend}: core.cache "
                                   f"needs django.core.cache.backends.redis.RedisCache")

    location = cache_settings.get('LOCATION', '')

    # split as RedisCache does; the helpers write to the first server
    servers = (location if isinstance(location, (list, tuple))
               else re.split('[;,]', location))

    if not servers or not all(servers):
        raise ImproperlyConfigured(
            "CACHES['default'] LOCATION names no server, or an empty one")

    try:
        params = redis.connection.parse_url(servers[0])
    except ValueError:
        # not chained: redis-py's error can quote part of a password
        raise ImproperlyConfigured(
            "CACHES['default'] LOCATION is no Redis URL redis-py can parse (a / # ? or @ "
            "in its credentials must be percent-encoded)") from None

    if 'db' not in params:
        # names no server: a / # or , in the credentials ends the URL's host early, so
        # what would be named here can be part of a password
        raise ImproperlyConfigured(
            "CACHES['default'] LOCATION names no logical database, e.g. "
            "redis://chris-cache:6379/0 (a / # ? or @ in its credentials must be "
            "percent-encoded)")

    where = _describe(params)

    # the settings' OPTIONS hold the retries at 0 under retry_on_timeout, but a retry here
    # replaces them, and redis-py parses retry_on_error into a list of characters with
    # which a failed call raises TypeError
    if ('retry' in params or params.get('retry_on_timeout')
            or params.get('retry_on_error')):
        raise ImproperlyConfigured(
            f"CACHES['default'] LOCATION ({where}) asks for retries: a SET NX retried "
            f"after its reply was lost finds its own key, and the helpers would wait out "
            f"a TTL")

    # a query string overrides the settings' bounded timeouts
    for name in ('socket_timeout', 'socket_connect_timeout'):
        if name in params and not 0 < params[name] <= MAX_SOCKET_TIMEOUT:
            raise ImproperlyConfigured(
                f"CACHES['default'] LOCATION ({where}) sets {name}={params[name]}: the "
                f"helpers need a timeout above 0 and at most {MAX_SOCKET_TIMEOUT} s")

    if broker_url and broker_url.startswith(('redis://', 'rediss://', 'unix://')):
        try:
            broker = redis.connection.parse_url(broker_url)
        except ValueError:
            broker = None  # Celery reports its own URL

        if broker is not None and _server(params) == _server(broker):
            raise ImproperlyConfigured(
                f"CACHES['default'] LOCATION ({where}) is on the Celery broker's "
                f"server: one server has one memory limit and eviction policy, so a "
                f"full cache would refuse the broker's writes, evicting would drop its "
                f"queues, and clearing its database would delete them")

    options = cache_settings.get('OPTIONS', {})
    client = RedisCacheClient(servers, **options)
    connection = client.get_client(write=True)

    # KEY_PREFIX, VERSION and KEY_FUNCTION make the check's keys as they make the helpers'
    make_key = backend_class(location, cache_settings).make_key

    try:
        connection.ping()
        _check_scripts(connection, make_key, where)
    except (redis.exceptions.AuthenticationError, redis.exceptions.ResponseError) as e:
        # AuthenticationError is a ConnectionError, so it goes first
        raise ImproperlyConfigured(f"CACHES['default'] ({where}): {e}")
    except _UNREACHABLE as e:
        # logged before LOGGING is configured, so the message says what it is about
        logger.warning('CUBE shared cache: %s does not answer (%s); starting without it, '
                       'and the helpers in core.cache go ahead without it until it does. '
                       'If it is up check redis:// against rediss:// and its TLS options',
                       where, e)
    except (redis.exceptions.RedisError, TypeError) as e:
        # what redis-py makes of a query string it cannot use (?nonsense=1 or
        # ?ssl_cert_reqs=bad), or of a server that does not speak its protocol
        raise ImproperlyConfigured(f"CACHES['default'] LOCATION ({where}): {e}")
    finally:
        # no socket for a forked worker to inherit
        connection.connection_pool.disconnect()


def _check_scripts(client, make_key, where):
    """
    Load both scripts and run them once, as the helpers would, on keys of the check's own
    that expire within a second and sit among the helpers' keys: a server that refuses
    them (an ACL without SCRIPT LOAD, SCRIPT EXISTS, which a replay's pipeline asks,
    EVALSHA, TIME or the helpers' keys, a provider that blocks scripts) stops start-up
    here rather than leaving every helper to fail open later. The explicit load matters:
    a server that holds the scripts already runs them without one, until its next restart
    empties its script cache. A Dragonfly that is full refuses them too and only warns:
    it may evict, or empty, by the time the helpers need it. Redis and Valkey run them
    even when full, since a script that has written may go on writing and the first write
    here only deletes.
    """
    key = make_key(f'core.cache:start-up-check:{_new_token()}')
    token = _new_token()

    try:
        for script in _SCRIPTS:
            client.script_load(script.script)
            client.script_exists(script.sha)
        _ACQUIRE_SLOT(keys=[key], args=[1, 1000, token], client=client)
        _RELEASE_LOCK(keys=[f'{key}:lock'], args=[token], client=client)
        client.zrem(key, token)
    except redis.exceptions.ResponseError as e:
        # a script refused between its ZADD and PEXPIREAT leaves a key that never expires
        with contextlib.suppress(redis.exceptions.RedisError):
            client.zrem(key, token)

        if not _out_of_memory(e):
            raise ImproperlyConfigured(
                f"CACHES['default'] ({where}) refuses the scripts the helpers in "
                f"core.cache run: {e}")

        logger.warning('CUBE shared cache: %s is full (%s); starting all the same, and '
                       'the helpers go ahead without it while it refuses writes',
                       where, e)


def _out_of_memory(error):
    """
    Whether an error reply refused a write for want of memory: Dragonfly answers
    ``-ERR Out of memory``, a plain ``ResponseError`` (inside the script's error when a
    script ran the write); redis-py raises Redis's ``-OOM`` as ``OutOfMemoryError``.
    """
    return (isinstance(error, redis.exceptions.OutOfMemoryError)
            or 'out of memory' in str(error).lower())


class _CacheHealth:
    """
    What this process knows about the shared cache. When the cache stops answering the
    helpers skip it for RETRY_AFTER seconds. Then one call tries it while the others go on
    skipping it until that call's outcome is known (or another RETRY_AFTER has passed), so
    a hung cache costs one socket timeout per pause rather than one per caller. The
    releases and clears that could not be sent wait here and go out once the cache answers
    again.
    """

    #: seconds the helpers stop trying after the cache failed to answer
    RETRY_AFTER = 5.0
    #: releases and clears kept for replay; beyond that the oldest are left to their TTL
    MAX_PENDING = 4096
    #: releases and clears one helper call replays, in one pipeline
    REPLAY_BATCH = 256
    #: seconds between two ERROR lines for refusals of one operation
    REFUSAL_LOG_INTERVAL = 60.0

    def __init__(self):
        self.reset()

    def reset(self):
        self._lock = threading.Lock()
        self._pending = deque(maxlen=self.MAX_PENDING)
        self._down_since = None
        self._tripped_at = 0.0
        self._failed_open = 0
        self._refusals_logged = {}
        self._no_server_logged = False

    def available(self):
        """
        Whether a call may try the cache: any call while it answers, one call per
        RETRY_AFTER while it does not.
        """
        if self._down_since is None:
            return True

        with self._lock:
            now = time.monotonic()

            if self._down_since is None:
                return True

            if now < self._tripped_at + self.RETRY_AFTER:
                return False

            # this call is the trial: the pause starts over for the others until it reports
            self._tripped_at = now
            return True

    def skipped(self):
        with self._lock:
            self._failed_open += 1

    def failed(self, op, error):
        with self._lock:
            now = time.monotonic()
            self._tripped_at = now
            self._failed_open += 1
            first = self._down_since is None
            if first:
                self._down_since = now

        if first:
            logger.warning('The shared cache does not answer (%s during %s): CUBE goes '
                           'ahead without its locks, markers and slots until it does',
                           error, op)

    def succeeded(self, started):
        if self._down_since is None:
            return

        with self._lock:
            # a call that started before the last failure says nothing about now
            if self._down_since is None or started < self._tripped_at:
                return

            outage = time.monotonic() - self._down_since
            failed_open = self._failed_open
            self._down_since = None
            self._failed_open = 0

        logger.info('The shared cache answers again after %.1f s; calls that went ahead '
                    'without it: %d', outage, failed_open)

    def refused(self, op, error, started=None):
        """
        The cache answered with an error (for example out of memory or a key of another
        type): the call went ahead without it but the cache is not down, so a call that
        ``started`` after the last failure ends an outage as an answer would.
        """
        now = time.monotonic()

        with self._lock:
            last = self._refusals_logged.get(op)
            log = last is None or now - last >= self.REFUSAL_LOG_INTERVAL

            if log:
                self._refusals_logged[op] = now

        if log:
            logger.error('The shared cache refused %s (%s); it went ahead without it',
                         op, error)

        if started is not None:
            self.succeeded(started)

    def no_server(self, op):
        """
        No cache is configured (no ``CACHE_URL``): no outage, and nothing will answer, so
        this says so once per process and only when something first asks for the cache.
        """
        if self._no_server_logged:
            return

        with self._lock:
            first = not self._no_server_logged
            self._no_server_logged = True

        if first:
            logger.warning('No shared cache is configured (no CACHE_URL): %s and the '
                           'calls after it go ahead without its locks, markers and slots',
                           op)

    def defer(self, release):
        self._pending.append(release)

    def replay(self, client):
        """
        Apply up to REPLAY_BATCH of the releases and clears that could not reach the
        cache, oldest first and in one pipeline, before the call that found the cache
        answering again; the next calls send the rest. Raises when the cache stops
        answering again, keeping them all for later: a release sent twice finds its token
        gone, and a clear sent twice at worst runs a caller's work twice (see
        ``clear_mark``).
        """
        batch = self._next_batch()

        if not batch:
            return

        try:
            with client.pipeline(transaction=False) as pipeline:
                for release in batch:
                    _apply(pipeline, *release)
                replies = pipeline.execute(raise_on_error=False)
        except _UNREACHABLE:
            # back at the front, so the order holds; were the queue full meanwhile this
            # would drop its newest entries rather than its oldest, releases that the TTLs
            # bound all the same
            self._pending.extendleft(reversed(batch))
            raise
        except redis.exceptions.RedisError as e:  # SCRIPT EXISTS, sent ahead of them
            replies = [e] * len(batch)

        for release, reply in zip(batch, replies):
            if isinstance(reply, redis.exceptions.RedisError):
                self.refused(_RELEASE_OPS[release[0]], reply)

    def _next_batch(self):
        batch = []

        try:
            while len(batch) < self.REPLAY_BATCH:
                batch.append(self._pending.popleft())
        except IndexError:  # no more, or another thread took the last one
            pass

        return batch


_health = _CacheHealth()

# a forked child starts with its own state, and no lock held by a thread it does not have
os.register_at_fork(after_in_child=_health.reset)


@receiver(setting_changed)
def _reset_on_new_cache_settings(setting, **kwargs):
    if setting == 'CACHES':
        _health.reset()


def _cache():
    # looked up on every call: Django keeps a cache per asgiref context (per thread, and
    # per request under ASGI) and replaces them all when the settings change
    return caches['default']


def _client():
    cache = _cache()
    if not isinstance(cache, RedisCache):
        raise ImproperlyConfigured(f"core.cache needs CACHES['default'] to be Django's "
                                   f"RedisCache, not {type(cache).__name__}")
    # Django has no public accessor for the redis-py client of its RedisCache;
    # its own tests use this one
    return cache._cache.get_client(write=True)


def _call(op, command, defer=None):
    """
    Run ``command(client)`` on the shared cache and return its reply or ``_SKIPPED`` when
    the cache is not tried and ``_FAILED`` when it does not answer or refuses. ``defer``
    is a release to replay later when it cannot reach the cache now.
    """
    # no CACHE_URL: nothing to try, and nothing to keep for later
    if isinstance(_cache(), DummyCache):
        _health.no_server(op)
        return _SKIPPED

    if not _health.available():
        _health.skipped()

        if defer:
            _health.defer(defer)
        return _SKIPPED

    started = time.monotonic()
    try:
        client = _client()
        _health.replay(client)
        reply = command(client)
    except _UNREACHABLE as e:
        _health.failed(op, e)
        if defer:
            _health.defer(defer)
        return _FAILED
    except redis.exceptions.RedisError as e:
        _health.refused(op, e, started)
        return _FAILED

    _health.succeeded(started)
    return reply


def _apply(client, kind, key, token):
    """
    Release a lock or slot by its token, or clear a mark, on a client or a pipeline. A
    client returns how many it freed, 0 or 1.
    """
    if kind == 'lock':
        return _RELEASE_LOCK(keys=[key], args=[token], client=client)

    if kind == 'slot':
        return client.zrem(key, token)

    if kind == 'mark':
        return client.delete(key)

    raise ValueError(f'unknown release kind {kind!r}')


def _lease(kind, key, token, reply):
    if reply is _SKIPPED:
        return Lease(kind, key, None, degraded=True)

    if reply is _FAILED:
        return Lease(kind, key, token, degraded=True)

    return Lease(kind, key, token) if reply else None


def _release(lease, kind):
    if not isinstance(lease, Lease) or lease.kind != kind:
        raise TypeError(f'release_{kind} takes a Lease from acquire_{kind} not {lease!r}')

    if lease.token is None:
        return False

    release = (kind, lease.key, lease.token)
    reply = _call(_RELEASE_OPS[kind], lambda client: _apply(client, *release),
                  defer=release)

    return reply == 1


def _milliseconds(ttl):
    """
    ``ttl`` seconds as whole milliseconds, rounded up. Raises for anything but a number of
    seconds in (0, MAX_TTL], before any I/O, so that a caller's mistake never fails open.
    """
    if (isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or
            not 0 < ttl <= MAX_TTL):
        raise ValueError(f'ttl must be a number of seconds in (0, {MAX_TTL}] not {ttl!r}')

    return math.ceil(ttl * 1000)


def _new_token():
    # an int is stored as its digits, which the release script compares
    return secrets.randbelow(2 ** 62) + 1


def _describe(params):
    """
    Where a parsed Redis URL points, without its credentials.
    """
    host, port = params.get('host', 'localhost'), params.get('port', 6379)
    server = params.get('path') or f'{host}:{port}'
    return f"{server}/{params['db']}"


def _server(params):
    """
    The server a parsed Redis URL names: its socket path, or its host, as DNS compares
    names, and its port.
    """
    return params.get('path') or (params.get('host', 'localhost').lower().rstrip('.'),
                                  int(params.get('port', 6379)))
