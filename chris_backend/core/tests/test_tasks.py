"""
Tests for the helpers the apps' Celery tasks share (core/tasks.py), against the stack's
cache server under the test run's own key prefix (core/testrunner.py).
"""

import logging
import os
import subprocess  # nosec B404 - runs this interpreter on a literal program
import sys
import uuid
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from core import cache as core_cache
from core.cache import MAX_TTL, acquire_lock, clear_mark, release_lock
from core.celery import app
from core.tasks import PENDING_TTL, single_instance
from plugininstances import tasks


class CacheStateMixin:

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        core_cache._health.reset()
        self.addCleanup(core_cache._health.reset)


class DedupedTaskTests(CacheStateMixin, SimpleTestCase):

    def setUp(self):
        super().setUp()
        # the task itself, not the proxy shared_task returns
        self.task = app.tasks['plugininstances.tasks.check_plugin_instance_job_exec_status']
        patcher = mock.patch.object(type(self.task), 'apply_async')
        self.apply_async = patcher.start()
        self.addCleanup(patcher.stop)
        self.args = (int(uuid.uuid4().int % 10 ** 9), 'PluginInstanceAppJob')
        self.addCleanup(clear_mark, self.task.pending_key(self.args))

    def returned(self, call, status='SUCCESS'):
        """A worker's run of the message published by ``call`` returns."""
        # the worker has the arguments from a JSON message: a list
        self.task.after_return(status, None, call.kwargs['task_id'], list(call.args[0]), {},
                               None)

    def test_a_check_pending_is_not_published_again_whatever_its_job(self):
        self.task.enqueue_once(*self.args)
        self.task.enqueue_once(*self.args)
        self.task.enqueue_once(self.args[0], 'PluginInstanceCopyJob')

        self.assertEqual(self.apply_async.call_args_list, [
            mock.call(self.args, expires=PENDING_TTL, task_id=mock.ANY)])

    def test_a_task_that_returned_is_published_again(self):
        self.task.enqueue_once(*self.args)
        self.returned(self.apply_async.call_args)
        self.task.enqueue_once(*self.args)

        self.assertEqual(self.apply_async.call_count, 2)

    def test_a_check_that_raised_is_published_again(self):
        self.task.enqueue_once(*self.args)
        self.returned(self.apply_async.call_args, 'FAILURE')
        self.task.enqueue_once(*self.args)

        self.assertEqual(self.apply_async.call_count, 2)

    def test_a_check_that_outlived_its_marker_leaves_the_next_one_pending(self):
        self.task.enqueue_once(*self.args)
        first = self.apply_async.call_args
        clear_mark(self.task.pending_key(self.args))  # its marker expired while it ran
        self.task.enqueue_once(*self.args)            # a second, with a marker of its own

        self.returned(first)                          # the first returns late
        self.task.enqueue_once(*self.args)            # the second is still pending

        self.assertEqual(self.apply_async.call_count, 2)

    def test_a_publish_that_failed_leaves_nothing_pending(self):
        self.apply_async.side_effect = [ConnectionError('broker'), mock.DEFAULT]

        with self.assertRaises(ConnectionError):
            self.task.enqueue_once(*self.args)
        self.task.enqueue_once(*self.args)

        self.assertEqual(self.apply_async.call_count, 2)

    def test_without_the_cache_it_publishes_every_time(self):
        conf = settings.CACHES['default']
        dead = {'default': {**conf, 'LOCATION': 'redis://127.0.0.1:6390/0'}}

        with override_settings(CACHES=dead):
            self.task.enqueue_once(*self.args)
            self.task.enqueue_once(*self.args)

        self.assertEqual(self.apply_async.call_count, 2)


class SingleInstanceTests(CacheStateMixin, SimpleTestCase):

    def test_a_run_is_skipped_while_another_runs_and_the_lock_goes_with_it(self):
        runs = []

        @single_instance(60)
        def beat_task(n):
            runs.append(n)
            return n

        name = f'{beat_task.__module__}.{beat_task.__name__}'
        held = acquire_lock(name, 60)
        self.assertIsNone(beat_task(1))
        release_lock(held)

        self.assertEqual(beat_task(2), 2)
        self.assertEqual(beat_task(3), 3)  # the lock went with the run before
        self.assertEqual(runs, [2, 3])

    def test_a_lock_by_arguments_skips_only_a_run_with_the_same_ones(self):
        runs = []

        @single_instance(60, key_args=1)
        def per_instance(plg_inst_id, attempt):
            runs.append((plg_inst_id, attempt))
            return plg_inst_id

        name = f'{per_instance.__module__}.{per_instance.__name__}'
        held = acquire_lock(f'{name}:7', 60)
        self.addCleanup(release_lock, held)

        self.assertIsNone(per_instance(7, 1))
        self.assertEqual(per_instance(8, 1), 8)
        self.assertEqual(runs, [(8, 1)])

    def test_a_run_that_raises_releases_the_lock(self):
        @single_instance(60)
        def failing():
            raise ValueError('boom')

        with self.assertRaises(ValueError):
            failing()
        with self.assertRaises(ValueError):  # it ran: not skipped
            failing()

    def test_the_scheduling_beat_task_keeps_its_name(self):
        self.assertEqual(tasks.schedule_waiting_plugin_instances.name,
                         'plugininstances.tasks.schedule_waiting_plugin_instances')


class PendingTtlTests(SimpleTestCase):

    def test_a_pending_ttl_out_of_its_bounds_stops_the_process(self):
        program = 'import django; django.setup(); import core.tasks'

        for value in ('0', str(MAX_TTL + 1)):
            with self.subTest(value=value):
                env = {**os.environ, 'CUBE_CELERY_PENDING_TTL': value}
                process = subprocess.run(  # nosec B603 nosemgrep - a literal program
                    [sys.executable, '-c', program], env=env, text=True,
                    capture_output=True, cwd=settings.BASE_DIR, timeout=60, check=False)

                self.assertNotEqual(process.returncode, 0)
                self.assertIn('CUBE_CELERY_PENDING_TTL must be a number of seconds',
                              process.stderr)
