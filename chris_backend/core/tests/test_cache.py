"""
Tests for the shared cache's helpers (core/cache.py) against the stack's cache server.

They share it with a running dev stack so every test uses names of its own, every key
expires and nothing calls cache.clear(), which is FLUSHDB of the whole database, or SCRIPT
FLUSH, which can wedge a Dragonfly that is running scripts at the time.
"""

import json
import logging
import math
import os
import selectors
import signal
import socket
import struct
import subprocess  # nosec B404 - runs this interpreter on a literal program
import sys
import tempfile
import threading
import time
import uuid
from unittest import mock
from urllib.parse import urlsplit

import redis
from redis.commands.core import Script
from django.conf import settings
from django.core.cache import caches
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from core import cache as core_cache
from core.cache import (MAX_TTL, acquire_lock, acquire_slot, clear_mark, mark_once,
                        release_lock, release_slot, verify_cache_connection)


class CacheProxy:
    """
    A TCP proxy between the cache client and its server so that a test can take the server
    away mid-test.

    ``stop()`` closes the listener and resets every live connection, as a killed server does
    (pooled connections see a reset, then a refused reconnect). ``pause()`` forwards nothing
    while the kernel still completes new connections, as a hung server does (calls run into
    the socket timeout); ``resume()`` forwards what was sent meanwhile. ``start()`` serves
    again on the same port.
    """

    def __init__(self, upstream):
        self.upstream = upstream
        self.port = 0
        self._paused = threading.Event()
        self._peers = {}
        self._thread = None

    def start(self):
        self._listener = socket.create_server(('127.0.0.1', self.port))
        self.port = self._listener.getsockname()[1]
        self._running = True
        self._paused.clear()
        self._thread = threading.Thread(target=self._serve, name='cache-proxy', daemon=True)
        self._thread.start()
        return self

    def pause(self):
        self._paused.set()

    def resume(self):
        self._paused.clear()

    def stop(self):
        self._running = False
        self._thread.join(5)

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    @property
    def open_connections(self):
        return len(self._peers) // 2

    def _serve(self):
        with selectors.DefaultSelector() as selector:
            selector.register(self._listener, selectors.EVENT_READ)
            while self._running:
                if self._paused.is_set():
                    time.sleep(0.01)
                    continue
                for key, _ in selector.select(0.01):
                    sock = key.fileobj
                    if sock is self._listener:
                        client, _ = sock.accept()
                        server = socket.create_connection(self.upstream, timeout=2)
                        self._peers[client], self._peers[server] = server, client
                        selector.register(client, selectors.EVENT_READ)
                        selector.register(server, selectors.EVENT_READ)
                    elif sock in self._peers:
                        try:
                            data = sock.recv(65536)
                            if data:
                                self._peers[sock].sendall(data)
                        except OSError:
                            data = b''
                        if not data:
                            self._drop(selector, sock, reset=False)
            while self._peers:
                self._drop(selector, next(iter(self._peers)), reset=True)
            self._listener.close()

    def _drop(self, selector, sock, reset):
        other = self._peers.pop(sock)
        self._peers.pop(other, None)
        for s in (sock, other):
            selector.unregister(s)
            if reset:  # RST instead of FIN, as from a killed server
                s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
            s.close()


def _name():
    return f'test-{uuid.uuid4().hex}'


def _raw():
    """
    The redis-py client the helpers use, for checks behind their back.
    """
    return caches['default']._cache.get_client(write=True)


def _key(namespace, name):
    return caches['default'].make_key(f'{namespace}:{name}')


def _unseen(script):
    """
    ``script`` under a SHA of its own, which the server does not hold, as after its restart.
    """
    return Script(None, script.script + f'\n-- {uuid.uuid4().hex}'.encode())


def _run_threads(count, target):
    """
    Run ``target(i)`` in ``count`` threads and re-raise the first exception one raised.
    """
    errors = []

    def run(i):
        try:
            target(i)
        except BaseException as e:  # noqa: B036 - re-raised in the test's thread
            errors.append(e)

    threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    stuck = [thread.name for thread in threads if thread.is_alive()]
    if stuck:
        raise AssertionError(f'threads still running after 30 s: {stuck}')
    if errors:
        raise errors[0]


def _contend(threads, rounds, attempt):
    """
    Call ``attempt(round)`` from ``threads`` threads released together, ``rounds`` times;
    returns each round's results.
    """
    barrier = threading.Barrier(threads)
    results = [[None] * threads for _ in range(rounds)]

    def run(i):
        try:
            for r in range(rounds):
                barrier.wait(10)
                results[r][i] = attempt(r)
        except BaseException:  # noqa: B036 - _run_threads re-raises it
            barrier.abort()
            raise

    _run_threads(threads, run)
    return results


def _successes_while(duration, attempt, threads=2):
    """
    The (before, after) times of the calls to ``attempt()`` that returned true, in order,
    from ``threads`` threads calling it every 5 ms for ``duration`` seconds.
    """
    successes, lock = [], threading.Lock()
    end = time.monotonic() + duration

    def run(_):
        while time.monotonic() < end:
            before = time.monotonic()
            if attempt():
                after = time.monotonic()
                with lock:
                    successes.append((before, after))
            time.sleep(0.005)

    _run_threads(threads, run)
    return sorted(successes)


class CacheTestCase(SimpleTestCase):
    """
    Each test starts with this process's view of the cache reset, and a short pause after a
    failure.
    """
    RETRY_AFTER = 0.3

    def setUp(self):
        logging.disable(logging.NOTSET)
        core_cache._health.reset()
        self.addCleanup(core_cache._health.reset)
        patcher = mock.patch.object(core_cache._CacheHealth, 'RETRY_AFTER', self.RETRY_AFTER)
        patcher.start()
        self.addCleanup(patcher.stop)

    def wait_for_retry(self):
        time.sleep(self.RETRY_AFTER + 0.05)

    def acl_user(self, keys, commands=()):
        """
        The cache's URL for a user of its own whom its ACL limits to the keys ``keys``
        (and denies the ``commands`` given as ``-name``).
        """
        user, password = _name(), _name()
        _raw().acl_setuser(user, enabled=True, passwords=[f'+{password}'], keys=[keys],
                           categories=['+@all'], commands=list(commands))
        self.addCleanup(_raw().acl_deluser, user)
        url = urlsplit(settings.CACHES['default']['LOCATION'])
        return url._replace(
            netloc=f'{user}:{password}@{url.hostname}:{url.port or 6379}').geturl()

    @staticmethod
    def cache_at(location):
        return override_settings(CACHES={'default': {**settings.CACHES['default'],
                                                     'LOCATION': location}})

    def assertOncePerTtl(self, successes, ttl, duration):
        """
        The successes of ``_successes_while`` are one per ``ttl`` seconds: at least two in
        ``duration``, and none sooner than ``ttl`` after the one before.
        """
        self.assertGreaterEqual(len(successes), 2)
        self.assertLessEqual(len(successes), math.floor(duration / ttl) + 1)
        for (before, _), (_, after) in zip(successes, successes[1:]):
            self.assertGreaterEqual(after - before, ttl - 0.01)


class ProxiedCacheTestCase(CacheTestCase):
    """
    Tests whose cache goes through a ``CacheProxy`` in front of its server, with a 0.2 s
    socket timeout.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        conf = settings.CACHES['default']
        url = urlsplit(conf['LOCATION'])
        cls.proxy = CacheProxy((url.hostname, url.port or 6379)).start()
        cls.addClassCleanup(lambda: cls.proxy.running and cls.proxy.stop())
        cls.enterClassContext(override_settings(CACHES={'default': {
            **conf,
            'LOCATION': url._replace(netloc=f'127.0.0.1:{cls.proxy.port}').geturl(),
            'OPTIONS': {**conf['OPTIONS'],
                        'socket_connect_timeout': 0.2, 'socket_timeout': 0.2},
        }}))

    def setUp(self):
        super().setUp()
        if not self.proxy.running:
            self.proxy.start()
        self.proxy.resume()


class LockTests(CacheTestCase):

    def test_exactly_one_of_two_contending_threads_takes_the_lock(self):
        names = [_name() for _ in range(100)]

        for leases in _contend(2, 100, lambda r: acquire_lock(names[r], 10)):
            taken = [lease for lease in leases if lease is not None]
            self.assertEqual(len(taken), 1)
            self.assertFalse(taken[0].degraded)

    def test_the_lock_is_taken_once_per_ttl(self):
        name = _name()

        successes = _successes_while(1.6, lambda: acquire_lock(name, 0.5) is not None)

        self.assertOncePerTtl(successes, 0.5, 1.6)

    def test_release_frees_the_lock(self):
        name = _name()
        lease = acquire_lock(name, 30)
        self.assertIsNone(acquire_lock(name, 30))

        self.assertTrue(release_lock(lease))

        again = acquire_lock(name, 30)
        self.assertIsNotNone(again)
        release_lock(again)

    def test_a_late_release_leaves_the_next_holders_lock_alone(self):
        name = _name()
        first = acquire_lock(name, 30)
        _raw().delete(first.key)  # as if it had expired
        second = acquire_lock(name, 30)

        self.assertFalse(release_lock(first))

        self.assertIsNone(acquire_lock(name, 30))
        release_lock(second)

    def test_the_lock_expires(self):
        lease = acquire_lock(_name(), 1.2345)

        self.assertTrue(1000 < _raw().pttl(lease.key) <= 1235)
        release_lock(lease)


class MarkOnceTests(CacheTestCase):

    def test_exactly_one_of_eight_threads_marks_first(self):
        keys = [_name() for _ in range(50)]

        for marks in _contend(8, 50, lambda r: mark_once(keys[r], 10)):
            self.assertEqual(sum(marks), 1)

    def test_marks_once_per_ttl(self):
        key = _name()

        successes = _successes_while(1.6, lambda: mark_once(key, 0.5))

        self.assertOncePerTtl(successes, 0.5, 1.6)

    def test_the_mark_expires(self):
        key = _name()
        self.assertTrue(mark_once(key, 1.2345))

        self.assertTrue(1000 < _raw().pttl(_key('mark', key)) <= 1235)

    def test_clear_mark_lets_the_next_call_mark(self):
        key = _name()
        self.assertTrue(mark_once(key, 30))
        self.assertFalse(mark_once(key, 30))

        clear_mark(key)

        self.assertTrue(mark_once(key, 30))
        clear_mark(key)


class SlotTests(CacheTestCase):

    def test_admits_exactly_the_ceiling_under_concurrent_acquires(self):
        name = _name()

        rounds = _contend(16, 20, lambda r: acquire_slot(name, 25, 30))

        leases = [lease for leases in rounds for lease in leases if lease is not None]
        self.assertEqual(len(leases), 25)
        self.assertFalse(any(lease.degraded for lease in leases))
        key = leases[0].key
        self.assertEqual(_raw().zcard(key), 25)
        self.assertGreater(_raw().pttl(key), 0)
        for lease in leases:
            self.assertTrue(release_slot(lease))
        self.assertEqual(_raw().exists(key), 0)

    def test_never_exceeds_the_ceiling_while_slots_turn_over(self):
        name, ceiling = _name(), 4
        key = _key('slots', name)
        lock, holders, most, stored = threading.Lock(), [0], [0], []
        done = threading.Event()

        def sample():
            while not done.is_set():
                stored.append(_raw().zcard(key))
                time.sleep(0.001)

        def work(_):
            for _ in range(25):
                lease = acquire_slot(name, ceiling, 30)
                if lease is None:
                    continue
                with lock:
                    holders[0] += 1
                    most[0] = max(most[0], holders[0])
                time.sleep(0.002)
                with lock:
                    holders[0] -= 1
                release_slot(lease)

        sampler = threading.Thread(target=sample)
        sampler.start()
        try:
            _run_threads(16, work)
        finally:
            done.set()
            sampler.join(5)

        self.assertLessEqual(most[0], ceiling)
        self.assertLessEqual(max(stored), ceiling)
        self.assertEqual(_raw().exists(key), 0)

    def test_a_slot_never_released_frees_itself_after_its_ttl(self):
        name = _name()
        self.assertIsNotNone(acquire_slot(name, 1, 0.5))
        self.assertIsNone(acquire_slot(name, 1, 0.5))

        deadline = time.monotonic() + 3
        while (lease := acquire_slot(name, 1, 0.5)) is None:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)

        release_slot(lease)

    def test_an_expired_lease_frees_its_slot_while_a_longer_one_is_held(self):
        # the longer lease keeps the key alive, so only the purge frees the expired slot
        name = _name()
        longer = acquire_slot(name, 2, 30)
        self.assertIsNotNone(acquire_slot(name, 2, 0.2))  # never released
        self.assertIsNone(acquire_slot(name, 2, 30))

        deadline = time.monotonic() + 3
        while (lease := acquire_slot(name, 2, 30)) is None:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)

        self.assertEqual(_raw().zcard(longer.key), 2)
        release_slot(lease)
        release_slot(longer)

    def test_a_lease_lasts_its_ttl_in_server_time(self):
        lease = acquire_slot(_name(), 1, 1.2345)
        seconds, micros = _raw().time()
        left = _raw().zscore(lease.key, lease.token) - (seconds * 1000 + micros // 1000)

        self.assertTrue(1000 < left <= 1235, left)
        self.assertTrue(1000 < _raw().pttl(lease.key) <= 1235)
        release_slot(lease)

    def test_the_slots_last_as_long_as_their_longest_lease(self):
        name = _name()
        longer = acquire_slot(name, 2, 5)
        shorter = acquire_slot(name, 2, 1)

        self.assertGreater(_raw().pttl(longer.key), 4000)
        release_slot(shorter)
        release_slot(longer)

    def test_the_scripts_work_on_a_server_that_does_not_hold_them(self):
        slot_script = _unseen(core_cache._ACQUIRE_SLOT)
        lock_script = _unseen(core_cache._RELEASE_LOCK)

        with mock.patch.object(core_cache, '_ACQUIRE_SLOT', slot_script), \
                mock.patch.object(core_cache, '_RELEASE_LOCK', lock_script):
            self.assertEqual(_raw().script_exists(slot_script.sha, lock_script.sha),
                             [False, False])
            slot = acquire_slot(_name(), 1, 5)
            lock = acquire_lock(_name(), 5)

            self.assertFalse(slot.degraded)
            self.assertTrue(release_slot(slot))
            self.assertTrue(release_lock(lock))

    def test_a_late_release_of_a_slot_says_it_freed_nothing(self):
        lease = acquire_slot(_name(), 1, 30)
        _raw().delete(lease.key)  # as if it had expired

        self.assertFalse(release_slot(lease))


class ValidationTests(CacheTestCase):

    def test_a_bad_ttl_raises_before_anything_is_written(self):
        for ttl in (0, -1, None, True, '1', float('inf'), float('nan'), MAX_TTL + 1):
            name = _name()
            for helper in (lambda: acquire_lock(name, ttl), lambda: mark_once(name, ttl),
                           lambda: acquire_slot(name, 1, ttl)):
                with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                    helper()
            self.assertEqual(_raw().exists(_key('lock', name), _key('mark', name),
                                           _key('slots', name)), 0)

    def test_a_ttl_below_a_millisecond_rounds_up(self):
        with self.assertNoLogs('core.cache', 'DEBUG'):
            lock = acquire_lock(_name(), 0.0001)
            self.assertTrue(mark_once(_name(), 0.0001))
            slot = acquire_slot(_name(), 1, 0.0001)

        self.assertFalse(lock.degraded)
        self.assertFalse(slot.degraded)

    def test_another_cache_backend_is_named_rather_than_failed_open(self):
        locmem = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
        with override_settings(CACHES=locmem):
            for helper in (lambda: acquire_lock(_name(), 5), lambda: mark_once(_name(), 5),
                           lambda: acquire_slot(_name(), 1, 5)):
                with self.assertRaisesRegex(ImproperlyConfigured, 'RedisCache'):
                    helper()

    def test_a_bad_ceiling_raises(self):
        for ceiling in (0, -1, True, 1.5, None):
            with self.subTest(ceiling=ceiling), self.assertRaises(ValueError):
                acquire_slot(_name(), ceiling, 5)

    def test_an_unknown_release_kind_raises_rather_than_deleting(self):
        client = mock.Mock()

        with self.assertRaises(ValueError):
            core_cache._apply(client, 'slots', _key('slots', _name()), 1)
        client.delete.assert_not_called()

    def test_a_release_takes_a_lease_of_its_own_kind(self):
        lock = acquire_lock(_name(), 5)
        slot = acquire_slot(_name(), 1, 5)

        for release, lease in ((release_slot, lock), (release_lock, slot),
                               (release_lock, None)):
            with self.subTest(release=release.__name__), self.assertRaises(TypeError):
                release(lease)
        release_lock(lock)
        release_slot(slot)


class RefusalTests(CacheTestCase):

    def test_a_refusal_goes_ahead_with_one_error_and_no_degraded_mode(self):
        name = _name()
        _raw().set(_key('slots', name), 'not a sorted set', px=30000)

        with self.assertLogs('core.cache', 'INFO') as logs:
            first = acquire_slot(name, 1, 30)
            second = acquire_slot(name, 1, 30)
            lock = acquire_lock(_name(), 5)

        self.assertTrue(first.degraded)
        self.assertTrue(second.degraded)
        self.assertFalse(lock.degraded)
        self.assertEqual([r.levelname for r in logs.records], ['ERROR'])
        self.assertIn('WRONGTYPE', logs.records[0].getMessage())
        release_lock(lock)

    def test_refusals_are_rate_limited_per_operation(self):
        name = _name()
        _raw().set(_key('slots', name), 'not a sorted set', px=30000)
        lease = acquire_lock(_name(), 30)
        _raw().delete(lease.key)
        _raw().sadd(lease.key, 'x')  # the release script's GET now meets a set
        _raw().pexpire(lease.key, 30000)

        with self.assertLogs('core.cache', 'INFO') as logs:
            acquire_slot(name, 1, 30)
            acquire_slot(name, 1, 30)
            release_lock(lease)
            release_lock(lease)

        self.assertEqual([r.levelname for r in logs.records], ['ERROR', 'ERROR'])

    def test_a_refusal_logs_again_after_the_interval(self):
        name = _name()
        _raw().set(_key('slots', name), 'not a sorted set', px=30000)

        with mock.patch.object(core_cache._health, 'REFUSAL_LOG_INTERVAL', 0), \
                self.assertLogs('core.cache', 'INFO') as logs:
            acquire_slot(name, 1, 30)
            acquire_slot(name, 1, 30)

        self.assertEqual([r.levelname for r in logs.records], ['ERROR', 'ERROR'])

    def test_an_authentication_error_counts_as_an_outage(self):
        # redis-py raises NOAUTH and WRONGPASS (and LOADING) as ConnectionErrors
        def refuse(client):
            raise redis.exceptions.AuthenticationError('invalid username-password pair')

        with self.assertLogs('core.cache', 'INFO') as logs:
            reply = core_cache._call('acquire_lock', refuse)

        self.assertIs(reply, core_cache._FAILED)
        self.assertFalse(core_cache._health.available())
        self.assertEqual([r.levelname for r in logs.records], ['WARNING'])

    def test_a_refusal_after_an_outage_ends_it_since_the_cache_answered(self):
        def refuse(client):
            raise redis.exceptions.ResponseError('WRONGTYPE Operation against a key')

        with self.assertLogs('core.cache', 'INFO') as logs:
            core_cache._health.failed('acquire_lock', redis.ConnectionError('gone'))
            self.wait_for_retry()
            reply = core_cache._call('acquire_slot', refuse)

        self.assertIs(reply, core_cache._FAILED)
        self.assertEqual([r.levelname for r in logs.records], ['WARNING', 'ERROR', 'INFO'])
        self.assertEqual([core_cache._health.available() for _ in range(2)], [True, True])

    def test_a_key_the_acl_refuses_does_not_stop_the_rest_of_its_batch(self):
        # refused when queued, so a transaction would abort the whole batch
        location = self.acl_user(caches['default'].make_key('lock:*'))
        lock = acquire_lock(_name(), 30)
        mark = _key('mark', _name())
        _raw().set(mark, 1, px=30000)

        with self.cache_at(location):
            core_cache._health.defer(('mark', mark, None))
            core_cache._health.defer(('lock', lock.key, lock.token))

            with self.assertLogs('core.cache', 'ERROR') as logs:
                self.assertFalse(acquire_lock(_name(), 5).degraded)

        self.assertEqual(len(logs.records), 1)
        self.assertIn('refused clear_mark', logs.output[0])
        self.assertEqual(_raw().exists(lock.key), 0)
        _raw().delete(mark)

    def test_a_refused_script_exists_logs_its_whole_batch_and_leaves_it_to_the_ttls(self):
        # SCRIPT EXISTS goes ahead of the pipeline and raises, rather than returning
        location = self.acl_user(caches['default'].make_key('*'), commands=['-script'])
        lock = acquire_lock(_name(), 30)
        mark = _key('mark', _name())
        _raw().set(mark, 1, px=30000)

        with self.cache_at(location):
            core_cache._health.defer(('lock', lock.key, lock.token))
            core_cache._health.defer(('mark', mark, None))

            with self.assertLogs('core.cache', 'ERROR') as logs:
                self.assertTrue(mark_once(_name(), 5))

            self.assertFalse(core_cache._health._pending)
            pool = caches['default']._cache.get_client(write=True).connection_pool
            self.assertEqual(len(pool._in_use_connections), 0)

        self.assertIn('refused release_lock', logs.output[0])
        self.assertIn('refused clear_mark', logs.output[1])
        self.assertEqual(_raw().exists(lock.key, mark), 2)
        _raw().delete(lock.key, mark)


class CacheHealthTests(CacheTestCase):

    def test_a_reply_to_a_call_sent_before_the_failure_keeps_the_pause(self):
        health = core_cache._health
        started = time.monotonic() - 0.001

        with self.assertLogs('core.cache', 'DEBUG') as logs:
            health.failed('acquire_lock', redis.ConnectionError('gone'))
            health.succeeded(started)

        self.assertEqual([r.levelname for r in logs.records], ['WARNING'])
        self.assertFalse(health.available())

    def test_a_forked_child_starts_with_a_state_of_its_own(self):
        # the cache down, a release kept and the lock held by this thread at the fork
        health = core_cache._health
        with self.assertLogs('core.cache', 'WARNING'):
            health.failed('acquire_lock', redis.ConnectionError('gone'))
        health.defer(('lock', _key('lock', _name()), 1))
        self.assertFalse(health.available())
        read, write = os.pipe()

        with health._lock:
            pid = os.fork()
            if pid == 0:  # the child
                signal.alarm(5)  # stuck on the lock it inherited, it dies and the test fails
                state = (health.available(), not health._pending,
                         health._lock.acquire(blocking=False))
                os.write(write, bytes(b'01'[flag] for flag in state))
                os._exit(0)
        os.close(write)
        os.waitpid(pid, 0)

        self.assertEqual(os.read(read, 3), b'111')
        os.close(read)

    def test_after_the_pause_one_call_tries_the_cache_and_the_others_wait_for_it(self):
        health = core_cache._health
        with self.assertLogs('core.cache', 'WARNING'):
            health.failed('acquire_lock', redis.ConnectionError('gone'))
        self.wait_for_retry()

        self.assertEqual([health.available() for _ in range(3)], [True, False, False])

    def test_a_trial_that_never_reports_lets_another_call_try_after_the_pause(self):
        health = core_cache._health
        with self.assertLogs('core.cache', 'WARNING'):
            health.failed('acquire_lock', redis.ConnectionError('gone'))
        self.wait_for_retry()
        self.assertTrue(health.available())  # a trial whose caller never reports

        self.assertFalse(health.available())
        self.wait_for_retry()
        self.assertTrue(health.available())


class DegradedModeTests(ProxiedCacheTestCase):

    def test_every_helper_goes_ahead_when_the_cache_stops_mid_test(self):
        held = acquire_lock(_name(), 30)
        self.proxy.stop()

        with self.assertLogs('core.cache', 'WARNING'):
            started = time.monotonic()
            lock = acquire_lock(_name(), 30)
            marked = mark_once(_name(), 30)
            slot = acquire_slot(_name(), 1, 30)
            released = release_lock(held)
            clear_mark(_name())
            elapsed = time.monotonic() - started

        self.assertTrue(lock.degraded)
        self.assertTrue(marked)
        self.assertTrue(slot.degraded)
        self.assertFalse(released)
        self.assertLess(elapsed, 1.0)

    def test_a_hung_cache_costs_one_timeout_then_the_helpers_skip_it(self):
        acquire_lock(_name(), 5)  # a pooled connection
        self.proxy.pause()

        with self.assertLogs('core.cache', 'WARNING'):
            started = time.monotonic()
            first = acquire_lock(_name(), 5)
            first_took = time.monotonic() - started
            started = time.monotonic()
            marked = mark_once(_name(), 5)
            slot = acquire_slot(_name(), 1, 5)
            rest_took = time.monotonic() - started

        self.assertTrue(first.degraded)
        self.assertTrue(marked)
        self.assertTrue(slot.degraded)
        self.assertTrue(0.15 <= first_took < 0.6)
        self.assertLess(rest_took, 0.2)  # less than one socket timeout

    def test_after_the_pause_a_hung_cache_costs_one_caller_a_timeout_not_all(self):
        acquire_lock(_name(), 5)  # a pooled connection
        self.proxy.pause()
        with self.assertLogs('core.cache', 'WARNING'):
            self.assertTrue(acquire_lock(_name(), 5).degraded)
        self.wait_for_retry()

        def timed(_):
            started = time.monotonic()
            self.assertTrue(acquire_lock(_name(), 5).degraded)
            return time.monotonic() - started

        took = _contend(8, 1, timed)[0]

        # one trial waits out the 0.2 s socket timeout; the seven others skip the cache
        self.assertEqual(sum(t >= 0.15 for t in took), 1, took)

    def test_the_helpers_use_the_cache_again_once_it_answers(self):
        name = _name()
        self.proxy.stop()

        with self.assertLogs('core.cache', 'INFO') as logs:
            self.assertTrue(acquire_lock(name, 30).degraded)
            self.proxy.start()
            self.wait_for_retry()
            lease = acquire_lock(name, 30)

        self.assertFalse(lease.degraded)
        self.assertIsNone(acquire_lock(name, 30))
        self.assertEqual([r.levelname for r in logs.records], ['WARNING', 'INFO'])
        release_lock(lease)

    def test_a_lock_released_during_an_outage_is_free_once_the_cache_answers(self):
        name = _name()
        lease = acquire_lock(name, 30)
        self.proxy.stop()
        with self.assertLogs('core.cache', 'WARNING'):
            self.assertFalse(release_lock(lease))

        self.proxy.start()
        self.wait_for_retry()

        with self.assertLogs('core.cache', 'INFO'):
            again = acquire_lock(name, 30)
        self.assertIsNotNone(again)
        self.assertFalse(again.degraded)
        release_lock(again)

    def test_a_release_skipped_while_the_helpers_wait_to_retry_is_replayed(self):
        name = _name()
        lease = acquire_lock(name, 30)
        self.proxy.stop()
        with self.assertLogs('core.cache', 'WARNING'):
            acquire_lock(_name(), 30)
        self.proxy.start()

        self.assertFalse(release_lock(lease))  # skipped: kept for later

        self.wait_for_retry()
        with self.assertLogs('core.cache', 'INFO'):
            again = acquire_lock(name, 30)
        self.assertIsNotNone(again)
        release_lock(again)

    def test_slots_and_marks_released_during_an_outage_are_free_once_the_cache_answers(self):
        name, key = _name(), _name()
        slot = acquire_slot(name, 1, 30)
        self.assertTrue(mark_once(key, 30))
        self.proxy.stop()
        with self.assertLogs('core.cache', 'WARNING'):
            self.assertFalse(release_slot(slot))
            clear_mark(key)

        self.proxy.start()
        self.wait_for_retry()

        with self.assertLogs('core.cache', 'INFO'):
            again = acquire_slot(name, 1, 30)
        self.assertIsNotNone(again)
        self.assertFalse(again.degraded)
        self.assertTrue(mark_once(key, 30))
        release_slot(again)
        clear_mark(key)

    def test_a_release_kept_through_a_second_failure_is_still_replayed(self):
        name = _name()
        lease = acquire_lock(name, 30)
        self.proxy.stop()
        with self.assertLogs('core.cache', 'WARNING'):
            self.assertFalse(release_lock(lease))
        self.wait_for_retry()
        self.assertTrue(acquire_lock(_name(), 30).degraded)  # the replay fails again

        self.proxy.start()
        self.wait_for_retry()

        with self.assertLogs('core.cache', 'INFO'):
            again = acquire_lock(name, 30)
        self.assertIsNotNone(again)
        release_lock(again)

    def test_a_refused_replay_does_not_stop_the_call_that_replays_it(self):
        slot = acquire_slot(_name(), 1, 30)
        self.proxy.stop()
        with self.assertLogs('core.cache', 'WARNING'):
            self.assertFalse(release_slot(slot))
        self.proxy.start()
        _raw().set(slot.key, 'not a sorted set', px=30000)
        self.wait_for_retry()

        with self.assertLogs('core.cache', 'INFO') as logs:
            lock = acquire_lock(_name(), 5)

        self.assertFalse(lock.degraded)
        self.assertEqual([r.levelname for r in logs.records], ['ERROR', 'INFO'])
        release_lock(lock)

    def test_releasing_a_lease_nothing_was_sent_for_sends_nothing(self):
        self.proxy.stop()
        with self.assertLogs('core.cache', 'WARNING'):
            acquire_lock(_name(), 5)
            skipped = acquire_lock(_name(), 5)
        self.assertIsNone(skipped.token)
        self.proxy.start()
        self.wait_for_retry()

        with self.assertLogs('core.cache', 'DEBUG') as logs:
            self.assertFalse(release_lock(skipped))
            release_lock(acquire_lock(_name(), 5))

        self.assertEqual([r.levelname for r in logs.records], ['INFO'])

    def test_beyond_max_pending_the_oldest_releases_are_left_to_their_ttl(self):
        with mock.patch.object(core_cache._CacheHealth, 'MAX_PENDING', 2):
            core_cache._health.reset()
            leases = [acquire_lock(_name(), 30) for _ in range(3)]
            self.proxy.stop()
            with self.assertLogs('core.cache', 'WARNING'):
                for lease in leases:
                    release_lock(lease)
            self.proxy.start()
            self.wait_for_retry()
            with self.assertLogs('core.cache', 'INFO'):
                acquire_lock(_name(), 5)  # replays what was kept

        self.assertEqual([_raw().exists(lease.key) for lease in leases], [1, 0, 0])
        release_lock(leases[0])

    def test_a_lock_the_server_took_after_its_reply_timed_out_is_released(self):
        name = _name()
        acquire_lock(_name(), 5)  # a pooled connection for the command to wait on
        self.proxy.pause()
        with self.assertLogs('core.cache', 'WARNING'):
            lease = acquire_lock(name, 30)
        self.assertTrue(lease.degraded)

        self.proxy.resume()  # the SET NX sent meanwhile now reaches the server
        deadline = time.monotonic() + 3
        while _raw().get(lease.key) != str(lease.token).encode():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.wait_for_retry()

        with self.assertLogs('core.cache', 'INFO'):
            self.assertTrue(release_lock(lease))
        again = acquire_lock(name, 30)
        self.assertIsNotNone(again)
        release_lock(again)

    def test_a_slot_the_server_took_after_its_reply_timed_out_is_released(self):
        name = _name()
        acquire_lock(_name(), 5)  # a pooled connection for the command to wait on
        self.proxy.pause()
        with self.assertLogs('core.cache', 'WARNING'):
            lease = acquire_slot(name, 1, 30)
        self.assertTrue(lease.degraded)

        self.proxy.resume()  # the script sent meanwhile now reaches the server
        deadline = time.monotonic() + 3
        while _raw().zscore(lease.key, lease.token) is None:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.wait_for_retry()

        with self.assertLogs('core.cache', 'INFO'):
            self.assertTrue(release_slot(lease))
        again = acquire_slot(name, 1, 30)
        self.assertIsNotNone(again)
        release_slot(again)

    def test_callers_in_many_threads_survive_the_cache_stopping(self):
        done = threading.Event()

        def work(_):
            while not done.is_set():
                lock = acquire_lock(_name(), 5)
                if lock is not None:
                    release_lock(lock)
                mark_once(_name(), 5)
                slot = acquire_slot(_name(), 2, 5)
                if slot is not None:
                    release_slot(slot)

        def stop_the_cache_meanwhile(i):
            if i:
                return work(i)
            time.sleep(0.05)
            self.proxy.stop()
            time.sleep(0.2)
            done.set()

        with self.assertLogs('core.cache', 'WARNING'):
            _run_threads(9, stop_the_cache_meanwhile)

    def test_an_outage_logs_one_warning_and_one_info(self):
        self.proxy.stop()

        with self.assertLogs('core.cache', 'DEBUG') as logs:
            for _ in range(5):
                acquire_lock(_name(), 5)
                mark_once(_name(), 5)
                acquire_slot(_name(), 1, 5)
                clear_mark(_name())
            self.proxy.start()
            self.wait_for_retry()
            acquire_lock(_name(), 5)

        self.assertEqual([r.levelname for r in logs.records], ['WARNING', 'INFO'])
        self.assertTrue(logs.records[1].getMessage().endswith(': 20'))

    def test_concurrent_failures_log_one_warning(self):
        self.proxy.stop()

        with self.assertLogs('core.cache', 'DEBUG') as logs:
            _contend(8, 1, lambda r: acquire_lock(_name(), 5))

        self.assertEqual([r.levelname for r in logs.records], ['WARNING'])

    def test_a_healthy_cache_logs_nothing(self):
        with self.assertNoLogs('core.cache', 'DEBUG'):
            release_lock(acquire_lock(_name(), 5))
            key = _name()
            mark_once(key, 5)
            clear_mark(key)
            release_slot(acquire_slot(_name(), 1, 5))

    def test_new_cache_settings_end_the_degraded_mode(self):
        self.proxy.stop()
        with self.assertLogs('core.cache', 'WARNING'):
            acquire_lock(_name(), 5)
        self.assertFalse(core_cache._health.available())

        with override_settings(CACHES=settings.CACHES):
            self.assertTrue(core_cache._health.available())


class ReplayTests(ProxiedCacheTestCase):

    def kept_releases(self, count, acquire=acquire_lock, release=release_lock):
        """
        ``count`` leases, of locks by default, whose releases could not reach the cache,
        which is back and past its pause.
        """
        leases = [acquire(_name(), 30) for _ in range(count)]
        self.proxy.stop()

        with self.assertLogs('core.cache', 'WARNING'):
            for lease in leases:
                self.assertFalse(release(lease))

        self.proxy.start()
        self.wait_for_retry()
        return leases

    @staticmethod
    def held(leases):
        return [_raw().exists(lease.key) for lease in leases]

    @staticmethod
    def pipelines_sent():
        """
        A patch of ``Pipeline.execute`` and the list it adds each pipeline's size to.
        """
        sizes, execute = [], redis.client.Pipeline.execute

        def counted(pipeline, raise_on_error=True):
            sizes.append(len(pipeline.command_stack))
            return execute(pipeline, raise_on_error)

        return mock.patch.object(redis.client.Pipeline, 'execute', counted), sizes

    def test_a_backlog_of_every_kind_goes_out_in_one_pipeline(self):
        name, key = _name(), _name()
        lock, slot = acquire_lock(_name(), 30), acquire_slot(name, 1, 30)
        self.assertTrue(mark_once(key, 30))
        self.proxy.stop()
        with self.assertLogs('core.cache', 'WARNING'):
            release_lock(lock)
            release_slot(slot)
            clear_mark(key)
        self.proxy.start()
        self.wait_for_retry()
        patch, sizes = self.pipelines_sent()

        with patch, self.assertLogs('core.cache', 'INFO') as logs:
            self.assertTrue(mark_once(_name(), 5))

        self.assertEqual(sizes, [3])
        self.assertEqual([r.levelname for r in logs.records], ['INFO'])
        self.assertEqual(_raw().exists(lock.key, slot.key, _key('mark', key)), 0)

    def test_one_call_replays_one_batch_and_the_next_calls_the_rest_oldest_first(self):
        leases = self.kept_releases(5)
        patch, sizes = self.pipelines_sent()

        with patch, mock.patch.object(core_cache._CacheHealth, 'REPLAY_BATCH', 2), \
                self.assertLogs('core.cache', 'INFO'):
            mark_once(_name(), 5)
            self.assertEqual(self.held(leases), [0, 0, 1, 1, 1])
            mark_once(_name(), 5)
            self.assertEqual(self.held(leases), [0, 0, 0, 0, 1])
            mark_once(_name(), 5)
            self.assertEqual(self.held(leases), [0, 0, 0, 0, 0])
            mark_once(_name(), 5)

        self.assertEqual(sizes, [2, 2, 1])

    def test_a_failed_replay_keeps_its_batch_in_order_ahead_of_the_rest(self):
        leases = self.kept_releases(3)
        kept = list(core_cache._health._pending)
        self.proxy.stop()

        with mock.patch.object(core_cache._CacheHealth, 'REPLAY_BATCH', 2):
            self.assertTrue(acquire_lock(_name(), 5).degraded)

        self.assertEqual(list(core_cache._health._pending), kept)
        self.proxy.start()
        self.wait_for_retry()
        with self.assertLogs('core.cache', 'INFO'):
            mark_once(_name(), 5)
        self.assertEqual(self.held(leases), [0, 0, 0])

    def test_a_replay_the_server_applied_after_it_timed_out_is_safe_to_send_again(self):
        # slots: for a lock, SCRIPT EXISTS would time out with the pipeline still unsent
        leases = self.kept_releases(2, lambda name, ttl: acquire_slot(name, 1, ttl),
                                    release_slot)
        kept = list(core_cache._health._pending)
        _raw().ping()  # a pooled connection for the pipeline to wait on
        self.proxy.pause()

        self.assertTrue(acquire_lock(_name(), 5).degraded)  # the replay times out

        self.assertEqual(list(core_cache._health._pending), kept)
        self.proxy.resume()  # the pipeline sent meanwhile now reaches the server
        deadline = time.monotonic() + 3
        while any(self.held(leases)):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.wait_for_retry()

        with self.assertLogs('core.cache', 'INFO') as logs:
            lock = acquire_lock(_name(), 5)  # sends them again

        self.assertFalse(lock.degraded)
        self.assertEqual([r.levelname for r in logs.records], ['INFO'])
        self.assertFalse(core_cache._health._pending)
        release_lock(lock)

    def test_a_refused_entry_does_not_stop_the_rest_of_its_batch(self):
        first, slot, last = (acquire_lock(_name(), 30), acquire_slot(_name(), 1, 30),
                             acquire_lock(_name(), 30))
        self.proxy.stop()
        with self.assertLogs('core.cache', 'WARNING'):
            release_lock(first)
            release_slot(slot)
            release_lock(last)
        self.proxy.start()
        _raw().set(slot.key, 'not a sorted set', px=30000)
        self.wait_for_retry()

        with self.assertLogs('core.cache', 'INFO') as logs:
            self.assertTrue(mark_once(_name(), 5))

        self.assertEqual([r.levelname for r in logs.records], ['ERROR', 'INFO'])
        self.assertIn('refused release_slot', logs.output[0])
        self.assertEqual(self.held([first, last]), [0, 0])

    def test_a_replay_loads_the_script_a_restarted_server_lost(self):
        leases = self.kept_releases(2)
        script = _unseen(core_cache._RELEASE_LOCK)

        with mock.patch.object(core_cache, '_RELEASE_LOCK', script), \
                self.assertLogs('core.cache', 'INFO') as logs:
            self.assertEqual(_raw().script_exists(script.sha), [False])
            self.assertTrue(mark_once(_name(), 5))

        self.assertEqual([r.levelname for r in logs.records], ['INFO'])
        self.assertEqual(self.held(leases), [0, 0])

    def test_a_replay_that_cannot_reach_the_cache_sends_nothing_else(self):
        self.kept_releases(1)
        self.proxy.stop()
        command = mock.Mock()

        self.assertIs(core_cache._call('acquire_lock', command), core_cache._FAILED)

        command.assert_not_called()

    def test_refused_entries_of_two_kinds_each_log_their_own_error(self):
        lock, slot = acquire_lock(_name(), 30), acquire_slot(_name(), 1, 30)
        self.proxy.stop()
        with self.assertLogs('core.cache', 'WARNING'):
            release_lock(lock)
            release_slot(slot)
        self.proxy.start()
        _raw().set(slot.key, 'not a sorted set', px=30000)
        _raw().delete(lock.key)
        _raw().sadd(lock.key, 'x')  # the release script's GET now meets a set
        _raw().pexpire(lock.key, 30000)
        self.wait_for_retry()

        with self.assertLogs('core.cache', 'INFO') as logs:
            self.assertTrue(mark_once(_name(), 5))

        self.assertEqual([r.levelname for r in logs.records], ['ERROR', 'ERROR', 'INFO'])
        self.assertIn('refused release_lock', logs.output[0])
        self.assertIn('refused release_slot', logs.output[1])

    def test_the_real_batch_goes_out_in_one_pipeline_and_the_rest_stays(self):
        for _ in range(core_cache._CacheHealth.REPLAY_BATCH + 1):
            core_cache._health.defer(('lock', _key('lock', _name()), 1))
        patch, sizes = self.pipelines_sent()

        with patch, self.assertNoLogs('core.cache', 'WARNING'):
            self.assertTrue(mark_once(_name(), 5))

        self.assertEqual(sizes, [256])
        self.assertEqual(len(core_cache._health._pending), 1)

    def test_a_call_with_nothing_to_replay_opens_no_pipeline(self):
        with mock.patch.object(redis.Redis, 'pipeline') as pipeline:
            release_lock(acquire_lock(_name(), 5))
            mark_once(_name(), 5)

        pipeline.assert_not_called()


@override_settings(CACHES={'default': {
    'BACKEND': 'django.core.cache.backends.dummy.DummyCache', 'KEY_PREFIX': 'cube'}})
class NoCacheServerTests(CacheTestCase):
    """
    A production deployment without ``CACHE_URL``: its ``CACHES['default']`` is a
    ``DummyCache``.
    """

    def setUp(self):
        super().setUp()
        # whatever these tests call, nothing may ask for a client
        patcher = mock.patch.object(core_cache, '_client',
                                    side_effect=AssertionError('asked for a client'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_helpers_go_ahead_without_their_guarantees(self):
        name = _name()

        with self.assertLogs('core.cache', 'WARNING'):
            locks = [acquire_lock(name, 5), acquire_lock(name, 5)]
            slots = [acquire_slot(name, 1, 5), acquire_slot(name, 1, 5)]
            marks = [mark_once(name, 5), mark_once(name, 5)]

        for lease in locks + slots:
            self.assertTrue(lease.degraded)
            self.assertIsNone(lease.token)
        self.assertEqual(marks, [True, True])

    def test_the_releases_do_nothing_and_keep_nothing(self):
        with self.assertLogs('core.cache', 'WARNING'):
            lock, slot = acquire_lock(_name(), 5), acquire_slot(_name(), 1, 5)
            self.assertFalse(release_lock(lock))
            self.assertFalse(release_slot(slot))
            clear_mark(_name())

        self.assertFalse(core_cache._health._pending)

    def test_a_process_says_so_once_when_something_first_asks_for_the_cache(self):
        with self.assertLogs('core.cache', 'DEBUG') as logs:
            for _ in range(3):
                acquire_lock(_name(), 5)
                mark_once(_name(), 5)
                acquire_slot(_name(), 1, 5)
                clear_mark(_name())

        self.assertEqual([r.levelname for r in logs.records], ['WARNING'])
        self.assertIn('no CACHE_URL', logs.output[0])
        self.assertIn('acquire_lock', logs.output[0])

    def test_the_reset_a_fork_runs_lets_a_child_say_so_once_too(self):
        for _ in range(2):
            with self.assertLogs('core.cache', 'WARNING') as logs:
                mark_once(_name(), 5)
                mark_once(_name(), 5)

            self.assertEqual(len(logs.records), 1)
            core_cache._health.reset()

    def test_no_server_is_no_outage(self):
        with self.assertLogs('core.cache', 'WARNING'):
            acquire_lock(_name(), 5)

        self.assertIsNone(core_cache._health._down_since)
        self.assertEqual(core_cache._health._failed_open, 0)
        self.assertTrue(core_cache._health.available())

    def test_a_callers_mistake_still_raises(self):
        for helper in (lambda: acquire_lock(_name(), 0), lambda: mark_once(_name(), 0),
                       lambda: acquire_slot(_name(), 1, 0),
                       lambda: acquire_slot(_name(), 0, 5)):
            with self.assertRaises(ValueError):
                helper()

        with self.assertRaises(TypeError):
            release_lock(None)


class StartupCheckTests(CacheTestCase):

    def conf(self, location):
        return {**settings.CACHES['default'], 'LOCATION': location}

    @property
    def host(self):
        return urlsplit(settings.CACHES['default']['LOCATION']).hostname

    def test_a_cache_that_answers_passes_silently(self):
        with self.assertNoLogs('core.cache', 'DEBUG'):
            verify_cache_connection(settings.CACHES['default'], settings.CELERY_BROKER_URL)

    def test_a_cache_that_refuses_connections_only_warns_and_keeps_its_password(self):
        with self.assertLogs('core.cache', 'WARNING') as logs:
            verify_cache_connection(self.conf(f'redis://:s3cret@{self.host}:6390/2'),
                                    settings.CELERY_BROKER_URL)

        self.assertEqual(len(logs.records), 1)
        self.assertIn(f'{self.host}:6390/2', logs.output[0])
        self.assertNotIn('s3cret', logs.output[0])

    def test_a_cache_on_several_servers_is_checked_on_the_one_written_to(self):
        location = settings.CACHES['default']['LOCATION']
        with self.assertNoLogs('core.cache', 'DEBUG'):
            for servers in ([location, location], f'{location},{location}'):
                verify_cache_connection(self.conf(servers), settings.CELERY_BROKER_URL)

    def test_a_database_0_on_another_server_is_not_the_brokers(self):
        with self.assertLogs('core.cache', 'WARNING'):
            verify_cache_connection(self.conf(f'redis://{self.host}:6390/0'),
                                    settings.CELERY_BROKER_URL)

    def test_each_wrong_configuration_stops_start_up_for_its_own_reason(self):
        host, broker = self.host, settings.CELERY_BROKER_URL
        b = urlsplit(broker)
        for location, reason in (
                ('http://somewhere:6379/2', 'LOCATION'),
                (f'redis://{host}:6379', 'names no logical database'),
                (f'redis://{host}:6379/abc', 'names no logical database'),
                (f'redis://{host}:6379/0?retry_on_timeout=true', 'asks for retries'),
                (f'redis://{host}:6379/0?retry_on_error=TimeoutError', 'asks for retries'),
                (f'redis://{host}:6379/0?retry=3', 'asks for retries'),
                (f'redis://{host}:6379/0?nonsense=1', 'nonsense'),
                (f'rediss://{host}:6379/0?ssl_cert_reqs=bad', 'Certificate Requirements'),
                (f'redis://{host}:6379/0?socket_timeout=0', 'socket_timeout=0.0'),
                (f'redis://{host}:6379/0?socket_connect_timeout=3600',
                 'socket_connect_timeout=3600.0'),
                (broker, "the Celery broker's server"),
                # the same server spelt another way: host case, a trailing dot, default port
                (f'redis://{b.hostname.upper()}{b.path}', "the Celery broker's server"),
                (f'redis://{b.hostname}.{b.path}', "the Celery broker's server"),
                # another database of the broker's server
                (f'redis://{b.hostname}:{b.port or 6379}/7', "the Celery broker's server"),
                (f'redis://{host}:6379/99', 'DB index'),
                (f'redis://nobody:wrong@{host}:6379/0', '')):
            with self.subTest(location=location), \
                    self.assertRaisesRegex(ImproperlyConfigured, reason):
                verify_cache_connection(self.conf(location), broker)

    def test_another_backend_stops_start_up(self):
        conf = {**self.conf(settings.CACHES['default']['LOCATION']),
                'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}

        with self.assertRaisesRegex(ImproperlyConfigured, 'RedisCache'):
            verify_cache_connection(conf, settings.CELERY_BROKER_URL)

    def test_a_backend_that_does_not_import_stops_start_up(self):
        for backend in ('', 'no.such.Backend', 'django.core.cache.caches'):
            conf = {**self.conf(settings.CACHES['default']['LOCATION']), 'BACKEND': backend}
            with self.subTest(backend=backend), \
                    self.assertRaisesRegex(ImproperlyConfigured, 'BACKEND'):
                verify_cache_connection(conf, settings.CELERY_BROKER_URL)

    def test_no_server_or_an_empty_one_stops_start_up(self):
        url = settings.CACHES['default']['LOCATION']

        for location in ([], (), '', ',', [''], f'{url},', [url, ''], ['', url]):
            with self.subTest(location=location), \
                    self.assertRaisesRegex(ImproperlyConfigured, 'names no server'):
                verify_cache_connection(self.conf(location), settings.CELERY_BROKER_URL)

    def test_a_server_that_is_down_only_warns_quickly(self):
        for scheme in ('redis', 'rediss'):
            with self.subTest(scheme=scheme):
                started = time.monotonic()
                with self.assertLogs('core.cache', 'WARNING') as logs:
                    verify_cache_connection(self.conf(f'{scheme}://127.0.0.1:6390/0'))

                self.assertLess(time.monotonic() - started, 1.0)
                self.assertIn('does not answer', logs.output[0])

    def test_a_server_that_closes_each_connection_only_warns(self):
        listener = socket.create_server(('127.0.0.1', 0))
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]

        def serve():
            try:
                while True:
                    listener.accept()[0].close()
            except OSError:  # the listener closed
                pass

        threading.Thread(target=serve, daemon=True).start()

        for scheme in ('redis', 'rediss'):
            with self.subTest(scheme=scheme), \
                    self.assertLogs('core.cache', 'WARNING') as logs:
                verify_cache_connection(self.conf(f'{scheme}://127.0.0.1:{port}/0'))

            self.assertIn('does not answer', logs.output[0])

    def test_a_tls_url_for_the_cache_which_speaks_plain_text_only_warns(self):
        with self.assertLogs('core.cache', 'WARNING') as logs:
            verify_cache_connection(self.conf(f'rediss://{self.host}:6379/0'),
                                    settings.CELERY_BROKER_URL)

        self.assertEqual(len(logs.records), 1)
        self.assertIn('does not answer', logs.output[0])
        self.assertIn('check redis:// against rediss://', logs.output[0])

    def test_a_refused_certificate_only_warns_and_names_the_reason(self):
        client = mock.Mock()
        client.get_client.return_value.ping.side_effect = redis.ConnectionError(
            'Error 1 connecting to cache:6379. [SSL: CERTIFICATE_VERIFY_FAILED]')

        with mock.patch.object(core_cache, 'RedisCacheClient', return_value=client), \
                self.assertLogs('core.cache', 'WARNING') as logs:
            verify_cache_connection(self.conf('rediss://cache:6379/0'))

        self.assertIn('CERTIFICATE_VERIFY_FAILED', logs.output[0])

    def test_a_timeout_after_the_ping_answered_only_warns(self):
        late = redis.exceptions.TimeoutError('Timeout reading from socket')

        with mock.patch.object(core_cache, '_ACQUIRE_SLOT', side_effect=late), \
                self.assertLogs('core.cache', 'WARNING') as logs:
            verify_cache_connection(settings.CACHES['default'], settings.CELERY_BROKER_URL)

        self.assertIn('does not answer', logs.output[0])

    def test_a_url_redis_py_cannot_parse_keeps_its_password_out_of_the_error(self):
        # an unescaped / ends the netloc: redis-py's own error quotes what came before it
        location = f'redis://:Pa55/w0rd@{self.host}:6379/0'

        with self.assertRaisesRegex(ImproperlyConfigured, 'percent-encoded') as raised:
            verify_cache_connection(self.conf(location))

        self.assertNotIn('Pa55', str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)

    def test_a_password_that_cuts_the_url_short_stays_out_of_the_error(self):
        for location, secret in (('redis://:12345/secret@host:6379/0', '12345'),
                                 ('redis://me:12345#secret@host:6379/0', '12345'),
                                 ('redis://:12345,secret@host:6379/0', '12345'),
                                 ('redis://hunter2#x@host:6379/0', 'hunter2')):
            with self.subTest(location=location), self.assertRaisesRegex(
                    ImproperlyConfigured, 'percent-encoded') as raised:
                verify_cache_connection(self.conf(location))

            self.assertNotIn(secret, str(raised.exception))

    def test_a_socket_path_is_named_and_compared_with_the_brokers(self):
        path = 'unix:///run/cube-test-cache.sock'

        with self.assertRaisesRegex(ImproperlyConfigured, "the Celery broker's server"):
            verify_cache_connection(self.conf(f'{path}?db=0'), f'{path}?db=1')

        with self.assertLogs('core.cache', 'WARNING') as logs:
            verify_cache_connection(self.conf(f'{path}?db=0'))
        self.assertIn('/run/cube-test-cache.sock/0 does not answer', logs.output[0])

    def test_a_broker_url_redis_py_cannot_parse_is_left_to_celery(self):
        with self.assertNoLogs('core.cache', 'DEBUG'):
            verify_cache_connection(settings.CACHES['default'],
                                    'redis://:no/such@broker:6379/0')

    def test_names_of_one_server_spelt_differently_are_not_resolved(self):
        # as DNS compares names: localhost and 127.0.0.1 are two servers here
        with self.assertLogs('core.cache', 'WARNING'):
            verify_cache_connection(self.conf('redis://localhost:6390/0'),
                                    'redis://127.0.0.1:6390/1')

    def test_the_check_runs_both_scripts_and_leaves_no_key_behind(self):
        conf, broker = settings.CACHES['default'], settings.CELERY_BROKER_URL
        with mock.patch.object(core_cache, '_ACQUIRE_SLOT',
                               wraps=core_cache._ACQUIRE_SLOT) as slot, \
                mock.patch.object(core_cache, '_RELEASE_LOCK',
                                  wraps=core_cache._RELEASE_LOCK) as release, \
                self.assertNoLogs('core.cache', 'DEBUG'):
            verify_cache_connection(conf, broker)

        key = slot.call_args.kwargs['keys'][0]
        self.assertEqual((slot.call_count, release.call_count), (1, 1))
        self.assertTrue(key.startswith(_key('core.cache', 'start-up-check:')))
        self.assertEqual(_raw().exists(key, f'{key}:lock'), 0)

    def test_a_user_the_acl_limits_to_the_helpers_keys_passes(self):
        # KEY_PREFIX:VERSION:*, as make_key makes them
        location = self.acl_user(caches['default'].make_key('*'))

        with self.assertNoLogs('core.cache', 'DEBUG'):
            verify_cache_connection(self.conf(location))

    def test_a_user_the_acl_keeps_from_the_helpers_keys_stops_start_up(self):
        location = self.acl_user('elsewhere:*')

        with self.assertRaisesRegex(ImproperlyConfigured, 'refuses the scripts'):
            verify_cache_connection(self.conf(location))

    def test_a_user_who_may_not_load_scripts_stops_start_up_while_they_are_cached(self):
        # the server holds both scripts already, so EVALSHA alone would run them until
        # its next restart empties its script cache
        release_slot(acquire_slot(_name(), 1, 5))
        release_lock(acquire_lock(_name(), 5))
        location = self.acl_user(caches['default'].make_key('*'), commands=['-script'])

        with self.assertRaisesRegex(ImproperlyConfigured, 'refuses the scripts'):
            verify_cache_connection(self.conf(location))

    def test_a_server_that_refuses_the_scripts_stops_start_up(self):
        # e.g. an ACL without EVALSHA, or a provider that blocks scripts
        refusal = redis.exceptions.NoPermissionError(
            "this user has no permissions to run the 'evalsha' command")

        with mock.patch.object(core_cache, '_ACQUIRE_SLOT', side_effect=refusal), \
                self.assertRaisesRegex(ImproperlyConfigured, 'refuses the scripts'):
            verify_cache_connection(settings.CACHES['default'], settings.CELERY_BROKER_URL)

    def test_a_server_that_refuses_script_exists_stops_start_up(self):
        # what a replay's pipeline asks before it runs a script
        refusal = redis.exceptions.NoPermissionError(
            "this user has no permissions to run the 'script|exists' command")

        with mock.patch.object(redis.Redis, 'script_exists', side_effect=refusal), \
                self.assertRaisesRegex(ImproperlyConfigured, 'refuses the scripts'):
            verify_cache_connection(settings.CACHES['default'], settings.CELERY_BROKER_URL)

    def test_a_script_refused_halfway_leaves_no_key_that_never_expires(self):
        # its ZADD runs and its PEXPIREAT does not
        location = self.acl_user(caches['default'].make_key('*'), commands=['-pexpireat'])
        token = core_cache._new_token()

        with mock.patch.object(core_cache, '_new_token', return_value=token), \
                self.assertRaisesRegex(ImproperlyConfigured, 'refuses the scripts'):
            verify_cache_connection(self.conf(location))

        self.assertEqual(_raw().exists(_key('core.cache', f'start-up-check:{token}')), 0)

    def test_a_cache_full_at_start_up_only_warns(self):
        # Dragonfly's -ERR inside the script's error, and the -OOM that redis-py raises as
        # OutOfMemoryError (Redis and Valkey run these scripts even when full)
        for full in (redis.exceptions.ResponseError(
                         'Error running script (call to f_0123): @user_script:9: '
                         '-ERR Out of memory'),
                     redis.exceptions.OutOfMemoryError(
                         "command not allowed when used memory > 'maxmemory'.")):
            with self.subTest(full=full), \
                    mock.patch.object(core_cache, '_ACQUIRE_SLOT', side_effect=full):
                with self.assertLogs('core.cache', 'WARNING') as logs:
                    verify_cache_connection(settings.CACHES['default'],
                                            settings.CELERY_BROKER_URL)

                self.assertIn('is full', logs.output[0])



class ProxiedStartupCheckTests(ProxiedCacheTestCase):

    def test_a_hung_cache_only_warns_within_the_timeout(self):
        conf = settings.CACHES['default']
        self.proxy.pause()

        for location in (conf['LOCATION'], conf['LOCATION'].replace('redis:', 'rediss:')):
            with self.subTest(location=location):
                started = time.monotonic()

                with self.assertLogs('core.cache', 'WARNING'):
                    verify_cache_connection({**conf, 'LOCATION': location})

                self.assertLess(time.monotonic() - started, 1.0)

    def test_the_check_disconnects_the_connection_it_opened(self):
        # the client stays referenced here, so only the explicit disconnect closes its socket
        self.proxy.stop()  # drops the connections earlier tests left
        self.proxy.start()
        made, real = [], core_cache.RedisCacheClient

        def keep(*args, **kwargs):
            made.append(real(*args, **kwargs))
            return made[-1]

        with mock.patch.object(core_cache, 'RedisCacheClient', side_effect=keep), \
                self.assertNoLogs('core.cache', 'WARNING'):  # it did reach the cache
            verify_cache_connection(settings.CACHES['default'])

        self.assertEqual(len(made), 1)
        deadline = time.monotonic() + 2
        while self.proxy.open_connections:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)


class CacheSettingsTests(SimpleTestCase):

    def test_the_cache_is_a_redis_server_apart_from_the_broker_with_bounded_timeouts(self):
        conf = settings.CACHES['default']
        cache = redis.connection.parse_url(conf['LOCATION'])
        broker = redis.connection.parse_url(settings.CELERY_BROKER_URL)

        self.assertEqual(conf['BACKEND'], 'django.core.cache.backends.redis.RedisCache')
        self.assertEqual(conf['KEY_PREFIX'], 'cube')
        self.assertIn('db', cache)
        self.assertNotEqual((cache['host'], cache.get('port')),
                            (broker['host'], broker.get('port')))
        # the effective values, which a query string in the URL would override
        kwargs = caches['default']._cache.get_client().connection_pool.connection_kwargs
        self.assertEqual(kwargs['socket_connect_timeout'], 0.5)
        self.assertEqual(kwargs['socket_timeout'], 0.5)

    def test_the_helpers_connections_never_retry(self):
        # a SET NX retried after its reply was lost finds its own key; the settings say so
        # rather than count on redis-py's default
        self.assertEqual(settings.CACHES['default']['OPTIONS']['retry'].get_retries(), 0)
        pool = caches['default']._cache.get_client(write=True).connection_pool
        connection = pool.make_connection()

        self.assertEqual(connection.retry.get_retries(), 0)
        self.assertFalse(connection.retry_on_timeout)
        self.assertFalse(connection.retry_on_error)

    def test_a_url_that_asks_for_retries_gets_none(self):
        conf = settings.CACHES['default']
        client = core_cache.RedisCacheClient([f"{conf['LOCATION']}?retry_on_timeout=true"],
                                             **conf['OPTIONS'])

        connection = client.get_client(write=True).connection_pool.make_connection()

        self.assertEqual(connection.retry.get_retries(), 0)


    def test_timeouts_in_the_url_take_the_place_of_the_settings(self):
        conf = settings.CACHES['default']
        client = core_cache.RedisCacheClient([f"{conf['LOCATION']}?socket_timeout=0.1"],
                                             **conf['OPTIONS'])

        kwargs = client.get_client().connection_pool.connection_kwargs

        self.assertEqual(kwargs['socket_timeout'], 0.1)
        self.assertEqual(kwargs['socket_connect_timeout'], 0.5)


class ProductionSettingsTests(SimpleTestCase):
    """
    ``config.settings.production`` in a process of its own: importing it here would change
    the ``CACHES`` of ``common.py`` that these tests run with.
    """

    # reports what the settings made of the cache, what a helper does with it and every
    # attempt either of them made to reach a server
    PROGRAM = """
import json
import sys
import uuid

connects = []


def audit(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo'):
        connects.append(event)


sys.addaudithook(audit)

import django

django.setup()

from django.core.cache import caches
from core.cache import acquire_lock, release_lock

lease = acquire_lock(f'core.tests.test_cache:production-settings:{uuid.uuid4().hex}', 1)
release_lock(lease)
print(json.dumps({'backend': type(caches['default']).__name__, 'degraded': lease.degraded,
                  'connected': bool(connects)}))
"""

    def start(self, program=PROGRAM, **cache_env):
        """
        Run ``program`` under the settings with their required variables, none of which they
        connect to at import, and ``cache_env``.
        """
        with tempfile.TemporaryDirectory() as media_root:
            env = {'PATH': os.environ['PATH'], 'HOME': media_root,
                   'DJANGO_SETTINGS_MODULE': 'config.settings.production',
                   'DJANGO_SECRET_KEY': _name(), 'CHRIS_SUPERUSER_PASSWORD': _name(),
                   'DJANGO_ALLOWED_HOSTS': '*', 'POSTGRES_DB': 'x', 'POSTGRES_USER': 'x',
                   'POSTGRES_PASSWORD': _name(), 'DATABASE_HOST': 'x',
                   'DATABASE_PORT': '5432', 'DATABASE_CONN_POOL': 'false',
                   'STORAGE_ENV': 'fslink', 'MEDIA_ROOT': media_root,
                   'CHRIS_STORE_URL': 'http://x/', 'NATS_ADDRESS': 'nats://x:4222',
                   'PFDCM_ADDRESS': 'http://x:4005', 'CELERY_BROKER_URL': 'amqp://x:5672//',
                   'DJANGO_SECURE_PROXY_SSL_HEADER': '',
                   'DJANGO_USE_X_FORWARDED_HOST': 'false', 'AUTH_LDAP': 'false',
                   'DISABLE_USER_ACCOUNT_CREATION': 'false', **cache_env}
            return subprocess.run(  # nosec B603 nosemgrep - a literal program
                [sys.executable, '-c', program], env=env, text=True,
                capture_output=True, cwd=settings.BASE_DIR, timeout=60, check=False)

    def assertStarted(self, process, backend, degraded, connected):
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout.splitlines()[-1]),
                         {'backend': backend, 'degraded': degraded, 'connected': connected})

    def test_no_cache_url_starts_with_no_cache_and_connects_to_nothing(self):
        for cache_env in ({}, {'CACHE_URL': ''}):
            with self.subTest(cache_env=cache_env):
                process = self.start(**cache_env)

                self.assertStarted(process, 'DummyCache', True, False)
                self.assertIn('no CACHE_URL', process.stderr)
                self.assertNotIn('does not answer', process.stderr)

    def test_no_cache_url_says_nothing_until_a_helper_is_called(self):
        process = self.start('import django; django.setup(); print("started")')

        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertNotIn('CACHE_URL', process.stderr)

    def test_a_cache_url_starts_with_that_cache(self):
        process = self.start(CACHE_URL=settings.CACHES['default']['LOCATION'])

        self.assertStarted(process, 'RedisCache', False, True)
        self.assertNotIn('core.cache', process.stderr)
        self.assertNotIn('no CACHE_URL', process.stderr)

    def test_a_cache_url_for_a_server_that_is_down_only_warns(self):
        process = self.start(CACHE_URL='redis://127.0.0.1:6390/0')

        self.assertStarted(process, 'RedisCache', True, True)
        self.assertIn('starting without it', process.stderr)

    def test_a_cache_url_for_the_brokers_server_stops_start_up(self):
        process = self.start(CACHE_URL='redis://broker:6379/0',
                             CELERY_BROKER_URL='redis://broker:6379/1')

        self.assertNotEqual(process.returncode, 0)
        self.assertIn("the Celery broker's server", process.stderr)
