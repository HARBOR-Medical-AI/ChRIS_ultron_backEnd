"""
``manage.py queuestats``: provides how many tasks wait in each of CUBE's Celery queues
and for how long the next one to be taken has waited.
"""

import contextlib
import json
import time

from django.core.management.base import BaseCommand

from core.celery import PUBLISHED_AT_HEADER, QUEUES, app


def queue_stats(celery_app=app, queues=QUEUES, now=None):
    """
    Per queue its depth and how many seconds its next message has waited since it was
    published (None when it has none, or the broker cannot tell), and how many messages
    the workers hold unacknowledged (reserved or running a late-acknowledged task; None
    when the broker cannot tell). Message ages and unacknowledged messages are read on
    Redis-protocol brokers only, the depth on any.
    """
    now = time.time() if now is None else now

    with celery_app.connection_for_read() as conn:
        redis = conn.transport.driver_type == 'redis'
        channel = conn.default_channel
        client = getattr(channel, 'client', None) if redis else None
        rows = []

        for queue in queues:
            depth = _depth(conn, channel if redis else None, queue)
            waited = _waited(client, queue, now) if depth else None
            rows.append({'queue': queue, 'depth': depth, 'waited_s': waited})

        unacked = client.hlen(channel.unacked_key) if client is not None else None
    return {'queues': rows, 'unacknowledged': unacked}


def _depth(conn, channel, queue):
    """
    How many messages wait in ``queue``, 0 for one never declared (or an empty Redis
    list). Without a ``channel`` (an AMQP broker, where a passive declare of a missing
    queue closes its channel) the queue gets a channel of its own.
    """
    try:
        if channel is not None:
            return channel.queue_declare(queue, passive=True).message_count

        own = conn.channel()
        try:
            return own.queue_declare(queue, passive=True).message_count
        finally:
            with contextlib.suppress(Exception):  # closed by the broker already
                own.close()
    except conn.channel_errors:
        return 0


def _waited(client, queue, now):
    """
    How long the message a worker would take next from ``queue`` (the end of its Redis
    list that BRPOP pops) has waited, from the header CUBE stamps it with when published.
    """
    if client is None:
        return None

    raw = client.lindex(queue, -1)
    if raw is None:
        return None

    try:
        published = json.loads(raw)['headers'][PUBLISHED_AT_HEADER]
    except (ValueError, KeyError, TypeError):
        return None  # published by something else (oxidicom) or before the stamp
    return round(now - float(published), 1)


class Command(BaseCommand):
    help = ("Print how many tasks wait in each Celery queue, how long the next one has "
            "waited, and how many messages the workers hold unacknowledged.")

    def add_arguments(self, parser):
        parser.add_argument('--json', action='store_true', help='print JSON')

    def handle(self, *args, **options):
        stats = queue_stats()

        if options['json']:
            self.stdout.write(json.dumps(stats))
            return

        self.stdout.write(f"{'queue':<10} {'depth':>7}  next waited")
        for row in stats['queues']:
            waited = '-' if row['waited_s'] is None else f"{row['waited_s']} s"
            self.stdout.write(f"{row['queue']:<10} {row['depth']:>7}  {waited}")

        unacked = stats['unacknowledged']
        self.stdout.write(f"unacknowledged: {'-' if unacked is None else unacked}")
