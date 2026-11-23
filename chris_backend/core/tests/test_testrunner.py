import io
import weakref
from contextlib import redirect_stderr
from unittest import mock

from celery.signals import before_task_publish
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase
from django.test.runner import DiscoverRunner

from core.celery import app as celery_app, task_routes
from core.testrunner import TEST_QUEUE, TerminateConnectionsTestRunner, run_cache_settings


class RunIsolationTests(SimpleTestCase):
    """
    What keeps a test run apart from a dev stack that shares its broker and cache.
    """

    def test_each_cache_gets_a_prefix_of_the_runs_own_after_its_configured_one(self):
        caches = {'default': {'BACKEND': 'x', 'KEY_PREFIX': 'cube'}, 'other': {}}

        out = run_cache_settings(caches, 'abc')

        self.assertEqual(out, {'default': {'BACKEND': 'x', 'KEY_PREFIX': 'cube-test-abc'},
                               'other': {'KEY_PREFIX': 'cube-test-abc'}})
        self.assertEqual(caches['default']['KEY_PREFIX'], 'cube')

    def test_this_run_has_its_own_prefix_and_no_routes(self):
        self.assertRegex(settings.CACHES['default']['KEY_PREFIX'], r'^cube-test-[0-9a-f]{8}$')
        self.assertIsNone(celery_app.conf.task_routes)

    def test_every_task_goes_to_the_test_queue_and_the_guard_listens(self):
        # routing creates no queue in the broker; a second runner is not set up here: its
        # receiver would replace this run's (Celery keys a bound method by its function)
        for task in task_routes:
            with self.subTest(task=task):
                route = celery_app.amqp.router.route({}, task)
                self.assertEqual(route['queue'].name, TEST_QUEUE)

        # a receiver connected with weak=False is kept as it is, the others as weakrefs
        live = [r() if isinstance(r, weakref.ReferenceType) else r
                for _, r in before_task_publish.receivers]
        guard = TerminateConnectionsTestRunner._check_queue
        self.assertTrue(any(getattr(r, '__func__', None) is guard for r in live))

    def test_the_run_consumes_and_purges_the_test_queue_alone(self):
        self.assertEqual(list(celery_app.amqp.queues.consume_from), [TEST_QUEUE])

    def test_a_run_split_over_processes_is_refused(self):
        with self.assertRaisesRegex(ImproperlyConfigured, 'cannot run in parallel'):
            TerminateConnectionsTestRunner(parallel=2).setup_test_environment()

    def test_a_task_published_to_another_queue_fails_the_run_and_is_named(self):
        runner = TerminateConnectionsTestRunner()
        runner.stray_publishes = []
        runner._check_queue(sender='tasks.a', routing_key='celery')
        runner._check_queue(sender='tasks.b', routing_key='status')
        err = io.StringIO()

        with mock.patch.object(DiscoverRunner, 'suite_result', return_value=2), \
                redirect_stderr(err):
            self.assertEqual(runner.suite_result(None, None), 3)

        self.assertIn("FAILED (stray publishes=1)", err.getvalue())
        self.assertIn("tasks.b to 'status'", err.getvalue())
        self.assertNotIn('tasks.a', err.getvalue())


@mock.patch('django.test.runner.DiscoverRunner.teardown_databases')
@mock.patch('core.testrunner.connection')
class TerminateConnectionsTestRunnerTests(SimpleTestCase):
    """
    Test which sessions the test runner terminates before dropping the test database.
    """

    def test_terminates_nothing_when_no_test_database_was_created(self, connection,
                                                                 teardown):
        TerminateConnectionsTestRunner().teardown_databases([])

        connection.cursor.assert_not_called()
        teardown.assert_called_once_with([])

    def test_terminates_the_other_sessions_on_the_test_database_before_dropping_it(
            self, connection, teardown):
        connection.settings_dict = {'NAME': 'test_chris_dev'}
        old_config = [(mock.Mock(), 'chris_dev', True)]
        cursor = connection.cursor.return_value.__enter__.return_value
        order = []
        cursor.execute.side_effect = lambda *args: order.append('terminate')
        teardown.side_effect = lambda *args, **kwargs: order.append('drop')

        TerminateConnectionsTestRunner().teardown_databases(old_config)

        sql, params = cursor.execute.call_args.args
        self.assertRegex(' '.join(sql.split()),
                         r'pg_terminate_backend\(pid\) FROM pg_stat_activity '
                         r'WHERE datname = %s AND pid <> pg_backend_pid\(\)$')
        self.assertEqual(params, ['test_chris_dev'])
        self.assertEqual(order, ['terminate', 'drop'])
        teardown.assert_called_once_with(old_config)
