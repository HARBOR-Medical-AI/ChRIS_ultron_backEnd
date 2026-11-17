"""
Tests for the Celery configuration (core/celery.py): one queue per class of task, the
options of each class, and the beat's entries. The tables are read directly: routing a
task through the app's router would declare the real queues in this process, and the test
runner routes every task to the 'celery' queue.
"""

import logging

from celery.app.task import Context
from django.conf import settings
from django.test import SimpleTestCase, TransactionTestCase
from django_celery_beat.models import PeriodicTask
from django_celery_beat.schedulers import DatabaseScheduler, ModelEntry

from core import celery as core_celery
from core.celery import (LATE_ACK_QUEUES, PERIODIC, QUEUES, TASK_QUEUES, VISIBILITY_TIMEOUT,
                         app, register_fast_max_files, task_options, task_routes,
                         time_limits)


# the apps whose tasks CUBE runs
APPS = ('core.', 'plugininstances.', 'feeds.', 'filebrowser.', 'pacsfiles.', 'dicomweb.')


class QueueTests(SimpleTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # the apps' task modules, which a worker imports when it starts
        app.loader.import_default_modules()

    def test_every_task_of_cube_s_has_a_queue_of_the_table(self):
        cube_tasks = {name for name in app.tasks if name.startswith(APPS)}

        self.assertEqual(cube_tasks - set(TASK_QUEUES), set())
        self.assertEqual(set(TASK_QUEUES) - set(app.tasks), set())  # no misspelt name
        # Celery's own, which the beat's scheduler publishes daily, has a consumer too
        self.assertEqual(TASK_QUEUES['celery.backend_cleanup'], PERIODIC)
        self.assertLessEqual(set(TASK_QUEUES.values()), set(QUEUES))
        self.assertNotIn('celery', QUEUES)  # the tests' own
        self.assertEqual(task_routes, {t: {'queue': q} for t, q in TASK_QUEUES.items()})

    def test_the_beat_tasks_are_periodic_and_a_late_tick_is_dropped(self):
        for entry in app.conf.beat_schedule.values():
            with self.subTest(task=entry['task']):
                self.assertEqual(TASK_QUEUES[entry['task']], PERIODIC)
                # django-celery-beat keeps expire_seconds, a whole number, and drops expires
                expire = entry['options']['expire_seconds']
                self.assertIsInstance(expire, int)
                self.assertGreaterEqual(expire, 1)

    def test_the_options_took_effect_on_the_tasks(self):
        delete = app.tasks['feeds.tasks.delete_feed']
        check = app.tasks['plugininstances.tasks.check_plugin_instance_job_exec_status']

        for name, limits in (('register_plugin_instance_output', (300, 360)),
                             ('register_large_plugin_instance_output', (3000, 3300))):
            register = app.tasks[f'plugininstances.tasks.{name}']
            with self.subTest(name=name):
                self.assertTrue(register.acks_late and register.reject_on_worker_lost)
                self.assertEqual((register.soft_time_limit, register.time_limit), limits)
        # the deletions can not resume yet (HARBOR-planning #157)
        self.assertFalse(delete.acks_late)
        self.assertEqual((delete.soft_time_limit, delete.time_limit), (None, None))
        self.assertFalse(check.acks_late)
        self.assertEqual((check.soft_time_limit, check.time_limit), (300, 360))

    def test_a_late_acknowledged_task_ends_before_the_broker_gives_it_out_again(self):
        self.assertEqual(app.conf.broker_transport_options['visibility_timeout'],
                         VISIBILITY_TIMEOUT)
        for queue in LATE_ACK_QUEUES:
            self.assertLess(time_limits(queue)[1], VISIBILITY_TIMEOUT)

        env = {'CUBE_CELERY_TIME_LIMITS_REGISTER': f'60,{VISIBILITY_TIMEOUT:.0f}'}
        with self.assertRaisesRegex(ValueError, 'under the visibility timeout'):
            task_options('register', env)

    def test_a_queue_s_time_limits_can_be_replaced(self):
        self.assertEqual(time_limits('status', {'CUBE_CELERY_TIME_LIMITS_STATUS': '10,20'}),
                         (10.0, 20.0))
        self.assertEqual(task_options('delete', {'CUBE_CELERY_TIME_LIMITS_DELETE': '5,6'}),
                         {'soft_time_limit': 5.0, 'time_limit': 6.0})

        for value in ('20,10', '0,5', '5', 'a,b', '1,2,3'):
            with self.subTest(value=value), \
                    self.assertRaisesRegex(ValueError, 'CUBE_CELERY_TIME_LIMITS_STATUS'):
                time_limits('status', {'CUBE_CELERY_TIME_LIMITS_STATUS': value})

    def test_the_worker_settings_take_effect(self):
        # the prefetch setting this issue started from had a name Celery ignores
        self.assertEqual(app.conf.worker_prefetch_multiplier, 2)
        self.assertIs(app.conf.worker_cancel_long_running_tasks_on_connection_loss, False)
        self.assertEqual([name for name in dir(settings) if name.startswith('CELERYD_')], [])

    def test_the_most_files_of_a_short_registration_can_be_replaced(self):
        name = 'CUBE_CELERY_REGISTER_FAST_MAX_FILES'

        self.assertEqual(register_fast_max_files({}), 100)
        self.assertEqual(register_fast_max_files({name: ''}), 100)
        for value, files in (('0', 0), ('250', 250), ('9999', 9999)):
            with self.subTest(value=value):
                self.assertEqual(register_fast_max_files({name: value}), files)

        # Swift refuses a listing limit above 10,000
        for value in ('-1', '10000', '1e3', 'many'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, name):
                register_fast_max_files({name: value})

    def test_a_published_message_is_stamped_with_when(self):
        headers = {}

        core_celery.stamp_published_at(headers=headers)

        self.assertIsInstance(headers[core_celery.PUBLISHED_AT_HEADER], float)

    def test_a_retried_message_is_stamped_anew(self):
        # Celery gives a retry's message the custom headers of the message it replaces
        task = app.tasks['plugininstances.tasks.register_large_plugin_instance_output']
        request = Context(id='x', args=[1], kwargs={},
                          **{core_celery.PUBLISHED_AT_HEADER: 1.0})
        headers = task.signature_from_request(request).options['headers']
        self.assertEqual(headers[core_celery.PUBLISHED_AT_HEADER], 1.0)

        core_celery.stamp_published_at(headers=headers)

        self.assertGreater(headers[core_celery.PUBLISHED_AT_HEADER], 1.0)


class BeatSchedulerTests(TransactionTestCase):
    """
    What django-celery-beat's DatabaseScheduler, which celery-scheduler runs, makes of the
    beat's entries.
    """

    def test_each_entry_s_expiry_reaches_the_messages_it_sends(self):
        scheduler = DatabaseScheduler(app=app, lazy=True)
        logging.disable(logging.INFO)  # its "Schedule changed." line
        self.addCleanup(logging.disable, logging.NOTSET)
        scheduler.setup_schedule()  # the entries written into the database

        for name, entry in app.conf.beat_schedule.items():
            with self.subTest(name=name):
                model = PeriodicTask.objects.get(name=name)
                expire = entry['options']['expire_seconds']

                self.assertEqual(model.expire_seconds, expire)
                self.assertEqual(ModelEntry(model, app=app).options['expires'], expire)
