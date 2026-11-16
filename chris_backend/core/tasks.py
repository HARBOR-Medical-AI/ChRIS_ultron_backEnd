"""
What the Django apps' Celery tasks share: a task class whose ``enqueue_once`` method
publishes a task only while no other with the same arguments is pending, and a decorator
that keeps a task from running twice at once.

Both coordinate through the shared cache's helpers (core/cache.py), which fail open: while
the cache does not answer a task may be published or run twice, which the tasks they guard
tolerate (their writes of a plugin instance's status are transitions).
"""

import logging
import os
from functools import wraps

from celery import Task
from celery.utils import uuid
from django.core.exceptions import ImproperlyConfigured

from .cache import MAX_TTL, acquire_lock, clear_mark, mark_once, release_lock


logger = logging.getLogger(__name__)

#: how long the message of a task ``enqueue_once`` published and its marker live, in
#: seconds: a task not run by then is dropped by the worker and published again
PENDING_TTL = float(os.getenv('CUBE_CELERY_PENDING_TTL', '60'))

if not 0 < PENDING_TTL <= MAX_TTL:
    # zero or less would fail every marker, and so every poll, with no more than an ERROR
    raise ImproperlyConfigured(f'CUBE_CELERY_PENDING_TTL must be a number of seconds in '
                               f'(0, {MAX_TTL}], not {PENDING_TTL:g}')


class DedupedTask(Task):
    """
    A task that ``enqueue_once`` publishes once per set of arguments while one is
    pending: the status checks that the beat task publishes every poll interval, which
    used to pile up behind slow workers. A marker in the shared cache says one is pending,
    from its publication until it returns, or for PENDING_TTL seconds at most, which is
    also how long its message lives: a message never outlives its marker and a check
    dropped unrun or lost is published again once its marker expires. The marker is its
    message's (the task id): a run that outlived its marker leaves a newer one in place.

    ``dedupe_args``, a task option, keys the marker by the first arguments alone (all of
    them by default): one status check per plugin instance whatever its job.
    """

    dedupe_args = None

    def pending_key(self, args):
        return ':'.join(['pending', self.name, *map(str, args[:self.dedupe_args])])

    def enqueue_once(self, *args):
        """
        Publish this task with ``args`` unless one with the same arguments is pending.
        Returns its ``AsyncResult`` or None when it was not published.
        """
        key, task_id = self.pending_key(args), uuid()

        if not mark_once(key, PENDING_TTL, token=task_id):
            return None

        try:
            return self.apply_async(args, expires=PENDING_TTL, task_id=task_id)
        except Exception:
            clear_mark(key, task_id)
            raise

    def after_return(self, status, retval, task_id, args, kwargs, einfo):
        clear_mark(self.pending_key(args), task_id)


def single_instance(ttl, key_args=0):
    """
    Decorate a task so that it skips a run while another one runs, anywhere: a lock in the
    shared cache for ``ttl`` seconds at most (a holder killed without releasing it blocks
    the runs for that long), which is released when the run ends. With ``key_args`` the
    lock is that of the task's first ``key_args`` arguments: one run per plugin instance,
    say, rather than one in all.
    """
    def decorate(f):
        task_name = f'{f.__module__}.{f.__name__}'

        @wraps(f)
        def wrapped(*args, **kwargs):
            name = task_name
            if key_args:
                name += ':' + ','.join(str(arg) for arg in args[:key_args])
            lease = acquire_lock(name, ttl)

            if lease is None:
                logger.info('%s is running elsewhere: skipping this run', name)
                return None
            try:
                return f(*args, **kwargs)
            finally:
                release_lock(lease)
        return wrapped
    return decorate
