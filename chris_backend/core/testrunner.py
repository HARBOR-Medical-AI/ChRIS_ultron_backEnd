"""
Custom Django test runner.

It keeps a test run apart from a dev stack running beside it, which shares the stack's
broker and cache: every Celery task a test publishes goes to the default 'celery' queue,
which the application never uses (see core/celery.py), and the cache's keys get a prefix of
the run's own, so that the helpers' markers and locks, named after plugin instance ids and
task names, never meet the stack's. A task published to another queue fails the run, and
the run's in-process workers consume, and its purges empty, the 'celery' queue alone. The
run cannot be split over processes (--parallel): its workers would share that one queue.

It also terminates open database connections before dropping the test database. This
prevents the "database is being accessed by other users" error that occurs when Celery
workers (started inside TransactionTestCase setUpClass) still hold connections when the test
suite tries to tear down.
"""

import secrets
import sys

from celery.signals import before_task_publish
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test.runner import DiscoverRunner
from django.test.utils import override_settings

from core.celery import app as celery_app


#: the only queue tests publish to
TEST_QUEUE = 'celery'


def run_cache_settings(caches, run_id):
    """
    ``caches`` (a ``CACHES`` setting) with a key prefix of a test run's own: the
    configured one (``cube``) followed by ``-test-`` and ``run_id``.
    """
    return {alias: {**conf, 'KEY_PREFIX': f"{conf.get('KEY_PREFIX') or 'cube'}-test-{run_id}"}
            for alias, conf in caches.items()}


class TerminateConnectionsTestRunner(DiscoverRunner):

    def setup_test_environment(self, **kwargs):
        if self.parallel > 1:
            raise ImproperlyConfigured(
                "CUBE's tests cannot run in parallel: the Celery workers they start would "
                f"share the {TEST_QUEUE!r} queue, and the stray publishes of a forked "
                "process would not fail the run")

        super().setup_test_environment(**kwargs)
        self._cache_settings = override_settings(
            CACHES=run_cache_settings(settings.CACHES, secrets.token_hex(4)))
        self._cache_settings.enable()

        self._routes = celery_app.conf.task_routes
        celery_app.conf.update(task_routes=None)
        # a stray publish declares its queue in this process: the workers the tests start,
        # and the purges in their tear-downs, still keep to the test queue alone
        queues = celery_app.amqp.queues
        self._consumed = list(queues.consume_from)
        queues.select([TEST_QUEUE])

        self.stray_publishes = []
        before_task_publish.connect(self._check_queue, weak=False)

    def teardown_test_environment(self, **kwargs):
        before_task_publish.disconnect(self._check_queue)
        celery_app.amqp.queues.select(self._consumed)
        celery_app.conf.update(task_routes=self._routes)
        self._cache_settings.disable()
        super().teardown_test_environment(**kwargs)

    def _check_queue(self, sender=None, routing_key=None, **kwargs):
        if routing_key != TEST_QUEUE:
            self.stray_publishes.append(f'{sender} to {routing_key!r}')

    def suite_result(self, suite, result, **kwargs):
        failures = super().suite_result(suite, result, **kwargs)

        if self.stray_publishes:
            print(f'\nFAILED (stray publishes={len(self.stray_publishes)}): tasks published '
                  f'to a queue other than {TEST_QUEUE!r}, which a running stack consumes: '
                  f'{", ".join(sorted(set(self.stray_publishes)))}', file=sys.stderr)
        return failures + len(self.stray_publishes)

    def teardown_databases(self, old_config, **kwargs):
        # Terminate all other connections to the test database before dropping it
        # so that Celery worker connections don't block the DROP DATABASE command.
        # old_config is empty when no selected test needed a database: no test database
        # was created but the connection still names the development database whose
        # sessions belong to the running stack.
        if old_config:
            test_db_name = connection.settings_dict['NAME']
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    [test_db_name],
                )
        super().teardown_databases(old_config, **kwargs)
