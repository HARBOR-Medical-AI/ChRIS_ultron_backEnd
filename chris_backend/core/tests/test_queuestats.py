"""
Tests for the queuestats management command.
"""

import io
import json
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase

from core.celery import PUBLISHED_AT_HEADER, app
from core.management.commands import queuestats
from plugininstances.tasks import sum as sum_task


class FakeRedisChannel:
    """
    A kombu Redis channel with queues of messages, given newest first as in a Redis list.
    """
    unacked_key = 'unacked'

    def __init__(self, queues, unacked):
        self.queues = queues
        self.client = SimpleNamespace(
            lindex=lambda queue, index: self.queues[queue][index],
            hlen=lambda key: unacked)

    def queue_declare(self, queue, passive=False):
        if not self.queues.get(queue):
            raise KeyError(queue)  # what the fake connection's channel_errors names
        return SimpleNamespace(message_count=len(self.queues[queue]))


class FakeAmqpChannel(FakeRedisChannel):
    """
    An AMQP channel: a passive declare of a missing queue closes it.
    """

    def __init__(self, queues):
        super().__init__(queues, None)
        self.closed = False

    def queue_declare(self, queue, passive=False):
        if self.closed:
            raise ConnectionError('channel closed')
        try:
            return super().queue_declare(queue, passive)
        except KeyError:
            self.closed = True
            raise

    def close(self):
        self.closed = True


def _fake_app(channel, driver='redis'):
    conn = SimpleNamespace(default_channel=channel, channel_errors=(KeyError,),
                           transport=SimpleNamespace(driver_type=driver),
                           channel=lambda: FakeAmqpChannel(channel.queues))

    @contextmanager
    def connection_for_read():
        yield conn

    return SimpleNamespace(connection_for_read=connection_for_read)


def _message(published_at=None):
    headers = {} if published_at is None else {PUBLISHED_AT_HEADER: published_at}
    return json.dumps({'headers': headers, 'body': ''})


class QueueStatsTests(SimpleTestCase):

    def test_depths_the_wait_of_the_next_message_and_the_unacknowledged(self):
        channel = FakeRedisChannel({'submit': [_message(95.0), _message(90.0)],
                                    'pacs': [_message()]}, unacked=3)

        stats = queuestats.queue_stats(_fake_app(channel), ('submit', 'status', 'pacs'),
                                       now=100.0)

        self.assertEqual(stats, {'queues': [
            {'queue': 'submit', 'depth': 2, 'waited_s': 10.0},  # the oldest, -1
            {'queue': 'status', 'depth': 0, 'waited_s': None},
            {'queue': 'pacs', 'depth': 1, 'waited_s': None},    # oxidicom's: no stamp
        ], 'unacknowledged': 3})

    def test_another_broker_gives_the_depths_only(self):
        channel = FakeRedisChannel({'submit': [_message(95.0)]}, unacked=3)

        stats = queuestats.queue_stats(_fake_app(channel, 'amqp'), ('submit',), now=100.0)

        self.assertEqual(stats, {'queues': [{'queue': 'submit', 'depth': 1,
                                             'waited_s': None}], 'unacknowledged': None})

    def test_on_amqp_a_missing_queue_closes_its_own_channel_only(self):
        channel = FakeAmqpChannel({'submit': [_message()], 'register': [_message()] * 2})

        stats = queuestats.queue_stats(_fake_app(channel, 'amqp'),
                                       ('submit', 'status', 'register'), now=100.0)

        self.assertEqual([row['depth'] for row in stats['queues']], [1, 0, 2])

    def test_the_command_prints_a_table_or_json(self):
        stats = {'queues': [{'queue': 'status', 'depth': 4, 'waited_s': 1.5}],
                 'unacknowledged': 2}

        with mock.patch.object(queuestats, 'queue_stats', return_value=stats):
            table, as_json = io.StringIO(), io.StringIO()
            call_command('queuestats', stdout=table)
            call_command('queuestats', '--json', stdout=as_json)

        self.assertIn('status           4  1.5 s', table.getvalue())
        self.assertIn('unacknowledged: 2', table.getvalue())
        self.assertEqual(json.loads(as_json.getvalue()), stats)

    def test_on_the_stack_s_broker_a_message_published_waits_in_its_queue(self):
        with app.connection_for_write() as conn:
            conn.default_channel.queue_purge('celery')
        self.addCleanup(self.purge_the_tests_queue)

        sum_task.apply_async((1, 2))  # the test runner routes it to 'celery'
        stats = queuestats.queue_stats(app, ('celery',))

        row = stats['queues'][0]
        self.assertEqual(row['depth'], 1)
        self.assertGreaterEqual(row['waited_s'], 0)
        self.assertLess(row['waited_s'], 60)
        self.assertIsInstance(stats['unacknowledged'], int)

    def test_on_the_stack_s_broker_a_queue_never_declared_is_empty(self):
        queue = f'cube-test-never-declared-{uuid.uuid4().hex}'

        stats = queuestats.queue_stats(app, (queue,))

        self.assertEqual(stats['queues'], [{'queue': queue, 'depth': 0, 'waited_s': None}])
        self.assertIsInstance(stats['unacknowledged'], int)

    @staticmethod
    def purge_the_tests_queue():
        with app.connection_for_write() as conn:
            conn.default_channel.queue_purge('celery')
