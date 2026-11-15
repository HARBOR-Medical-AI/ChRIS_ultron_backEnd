"""
Tests for the conditional writes of plugin instances (HARBOR-planning #152): the status
transitions, what follows them only for the task that won, and the tasks that race.
"""

import ast
import logging
import threading
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from plugins.models import PluginMeta, Plugin, ComputeResource
from plugininstances import tasks
from plugininstances.models import PluginInstance, StatusWrite
from plugininstances.services.copyjobs import PluginInstanceCopyJob
from plugininstances.services.pluginjobs import PluginInstanceAppJob


COMPUTE_RESOURCE_URL = settings.COMPUTE_RESOURCE_URL

FINISHED = {'compute': {'status': 'finishedSuccessfully', 'logs': '', 'jid': 'x'}}


class PluginInstanceFixture:
    """
    A user, an fs and a ds plugin on a compute resource, and ``instance(...)`` to create
    instances in a given status.
    """

    def setUp(self):
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)
        (self.compute_resource, _) = ComputeResource.objects.get_or_create(
            name='host', compute_url=COMPUTE_RESOURCE_URL, compute_user='pfcon',
            compute_password='pfcon1234',  # nosec B106 - the dev pfcon's test user
            compute_requires_copy_job=True)
        self.user = User.objects.create_user(username='foo',
                                             password='bar')  # nosec B106 - a test user
        self.plugins = {}

        for name, plugin_type in (('pacspull', 'fs'), ('mri_convert', 'ds')):
            (meta, _) = PluginMeta.objects.get_or_create(name=name, type=plugin_type)
            (plugin, _) = Plugin.objects.get_or_create(meta=meta, version='0.1')
            plugin.compute_resources.set([self.compute_resource])
            self.plugins[plugin_type] = plugin

    def instance(self, status, previous=None):
        plugin = self.plugins['ds' if previous else 'fs']
        inst = PluginInstance.objects.create(plugin=plugin, owner=self.user,
                                             previous=previous,
                                             compute_resource=self.compute_resource)
        PluginInstance.objects.filter(pk=inst.pk).update(status=status)
        return PluginInstance.objects.get(pk=inst.pk)


class TransitionTests(PluginInstanceFixture, TestCase):

    def test_a_transition_from_the_status_the_row_has_wins_and_updates_the_object(self):
        inst = self.instance('copying')

        moved = inst.transition('scheduled', ['waiting', 'copying'], error_code='CODE01')

        self.assertTrue(moved)
        self.assertEqual((moved.won, moved.status), (True, 'scheduled'))
        self.assertEqual((inst.status, inst.error_code), ('scheduled', 'CODE01'))
        inst.refresh_from_db()
        self.assertEqual((inst.status, inst.error_code), ('scheduled', 'CODE01'))

    def test_a_transition_from_another_status_loses_writes_nothing_and_says_why(self):
        inst = self.instance('copying')
        PluginInstance.objects.filter(pk=inst.pk).update(status='cancelled')

        moved = inst.transition('scheduled', 'copying', error_code='CODE01')

        self.assertFalse(moved)
        self.assertEqual(moved.status, 'cancelled')
        self.assertEqual((inst.status, inst.error_code), ('copying', ''))  # untouched
        inst.refresh_from_db()
        self.assertEqual((inst.status, inst.error_code), ('cancelled', ''))

    def test_a_transition_of_a_row_that_is_gone_says_none(self):
        inst = self.instance('started')
        PluginInstance.objects.filter(pk=inst.pk).delete()

        moved = inst.transition('cancelled', 'started')

        self.assertEqual((moved.won, moved.status), (False, None))

    def test_a_name_that_is_not_a_status_is_refused_before_any_write(self):
        inst = self.instance('started')

        for to, source in (('canceled', 'started'), ('cancelled', ['started', 'strated']),
                           ('cancelled', [])):
            with self.subTest(to=to, source=source), self.assertRaises(ValueError):
                inst.transition(to, source)

        with self.assertRaises(ValueError):
            PluginInstance.objects.all().transition('done', 'started')
        self.assertEqual(PluginInstance.objects.get(pk=inst.pk).status, 'started')

    def test_only_a_transition_writes_the_status(self):
        inst = self.instance('started')

        with self.assertRaises(ValueError):
            inst.update_if_status('started', status='cancelled')
        with self.assertRaises(ValueError):
            inst.move_remote_cleanup('deletingData', 'notStarted', status='cancelled')

    def test_update_if_status_writes_only_while_the_status_holds(self):
        inst = self.instance('started')

        self.assertTrue(inst.update_if_status('started', raw='a'))
        PluginInstance.objects.filter(pk=inst.pk).update(status='cancelled')
        written = inst.update_if_status('started', raw='b')

        self.assertEqual((written.won, written.status), (False, 'cancelled'))
        self.assertEqual(PluginInstance.objects.get(pk=inst.pk).raw, 'a')

    def test_the_queryset_form_checks_each_row_s_status_even_through_a_relation(self):
        parent = self.instance('cancelled')
        waiting = self.instance('waiting', previous=parent)
        started = self.instance('started', previous=parent)

        moved = PluginInstance.objects.filter(previous__status='cancelled').transition(
            'cancelled', 'waiting')

        self.assertEqual(moved, 1)
        self.assertEqual(PluginInstance.objects.get(pk=waiting.pk).status, 'cancelled')
        self.assertEqual(PluginInstance.objects.get(pk=started.pk).status, 'started')
        self.assertEqual(PluginInstance.objects.none().transition('cancelled', 'waiting'), 0)

    def test_arming_the_remote_cleanup_is_idempotent_and_rearming_restarts_an_ended_one(self):
        inst = self.instance('cancelled')
        active = self.instance('started')

        self.assertFalse(active.arm_remote_cleanup())  # only once it has ended
        self.assertTrue(inst.arm_remote_cleanup())
        inst.increment('remote_cleanup_retry_count')
        self.assertFalse(inst.arm_remote_cleanup())
        self.assertFalse(inst.arm_remote_cleanup(rearm=True))  # not while it runs

        PluginInstance.objects.filter(pk=inst.pk).update(remote_cleanup_status='complete')
        self.assertTrue(inst.arm_remote_cleanup(rearm=True))
        inst.refresh_from_db()
        self.assertEqual((inst.remote_cleanup_status, inst.remote_cleanup_retry_count),
                         ('deletingData', 0))

    def test_increment_adds_one_in_the_database_whatever_the_object_holds(self):
        inst = self.instance('copying')
        other = PluginInstance.objects.get(pk=inst.pk)

        self.assertEqual(inst.increment('copy_retry_count'), 1)
        self.assertEqual(other.increment('copy_retry_count'), 2)
        self.assertEqual(inst.copy_retry_count, 1)

    def test_scheduling_moves_a_waiting_instance_once(self):
        inst = self.instance('waiting')
        twin = PluginInstance.objects.get(pk=inst.pk)

        with mock.patch.object(tasks.run_plugin_instance_job, 'delay') as delay:
            self.assertTrue(tasks.schedule_plugin_instance(inst, 'waiting'))
            self.assertFalse(tasks.schedule_plugin_instance(twin, 'waiting'))

        delay.assert_called_once_with(inst.pk, 'PluginInstanceCopyJob')
        self.assertEqual(inst.status, 'copying')

    def test_waiting_children_of_failed_parents_are_cancelled_and_nothing_else(self):
        failed = self.instance('finishedWithError')
        finished = self.instance('finishedSuccessfully')
        orphan = self.instance('waiting', previous=failed)
        healthy = self.instance('waiting', previous=finished)

        tasks.cancel_waiting_plugin_instances()

        self.assertEqual(PluginInstance.objects.get(pk=orphan.pk).status, 'cancelled')
        self.assertEqual(PluginInstance.objects.get(pk=healthy.pk).status, 'waiting')

    def test_a_crashed_task_only_sends_a_cancel_with_code19(self):
        inst = self.instance('started')
        sender = mock.Mock()

        for name, args, calls in (
                ('plugininstances.tasks.run_plugin_instance_job',
                 (inst.pk, 'PluginInstanceAppJob'), 1),
                ('plugininstances.tasks.run_plugin_instance_job',
                 (inst.pk, 'PluginInstanceDeleteJob'), 0),
                ('plugininstances.tasks.check_plugin_instance_job_exec_status',
                 (inst.pk, 'PluginInstanceAppJob'), 0),
                # a failed cancel tries again by itself, a few times
                ('plugininstances.tasks.cancel_plugin_instance_job', (inst.pk,), 0)):
            sender.name = name
            with self.subTest(name=name, args=args), \
                    mock.patch.object(tasks.cancel_plugin_instance_job, 'delay') as cancel:
                tasks.cancel_plugin_inst_on_task_failure(sender=sender, args=args)

                self.assertEqual(cancel.call_count, calls)
                if calls:
                    cancel.assert_called_once_with(inst.pk, error_code='CODE19')

        self.assertEqual(PluginInstance.objects.get(pk=inst.pk).status, 'started')

    def test_the_stuck_instance_cancellers_pass_their_code_to_the_cancel(self):
        inst = self.instance('scheduled')
        PluginInstance.objects.filter(pk=inst.pk).update(
            start_date=inst.start_date.replace(year=2000))

        with mock.patch.object(tasks.cancel_plugin_instance_job, 'delay') as cancel, \
                self.assertLogs('plugininstances.tasks', 'ERROR') as logs:
            tasks.cancel_plugin_instances_stuck_in_scheduled_status()

        cancel.assert_called_once_with(inst.pk, error_code='CODE18')
        self.assertIn('stuck in scheduled status', logs.output[0])

    def test_a_cancel_arms_the_cleanup_of_an_instance_with_a_remote_job_alone(self):
        for status, cleanup in (('waiting', 'notStarted'), ('copying', 'deletingData'),
                                ('started', 'deletingData')):
            with self.subTest(status=status):
                inst = self.instance(status)

                self.assertTrue(inst.cancel())
                inst.refresh_from_db()
                self.assertEqual((inst.status, inst.remote_cleanup_status),
                                 ('cancelled', cleanup))

    def test_a_cancel_follows_a_row_that_moved_on_since_it_was_read(self):
        inst = self.instance('waiting')
        original = PluginInstance.transition
        calls = []

        def submitted_after_the_first_update(obj, to, source, **fields):
            written = original(obj, to, source, **fields)
            calls.append(written.won)
            if len(calls) == 1:  # a task submits its copy job meanwhile
                PluginInstance.objects.filter(pk=obj.pk).update(status='copying')
            return written

        with mock.patch.object(PluginInstance, 'transition',
                               submitted_after_the_first_update):
            cancelled = inst.cancel()

        self.assertTrue(cancelled)
        self.assertEqual(calls, [False, False, True])
        inst.refresh_from_db()
        self.assertEqual((inst.status, inst.remote_cleanup_status),
                         ('cancelled', 'deletingData'))

    def test_a_cancel_whose_row_moved_between_its_update_and_its_read_still_cancels_it(self):
        inst = self.instance('waiting')
        original = PluginInstance.transition
        calls = []

        def submitted_before_the_read(obj, to, source, **fields):
            written = original(obj, to, source, **fields)
            calls.append(written.won)
            if len(calls) == 1:
                # a task submits the copy job after the UPDATE missed the row and before
                # the read of its status
                PluginInstance.objects.filter(pk=obj.pk).update(status='copying')
                return StatusWrite(False, 'copying')
            return written

        with mock.patch.object(PluginInstance, 'transition', submitted_before_the_read):
            cancelled = inst.cancel()

        self.assertTrue(cancelled)
        self.assertEqual(calls, [False, False, True])
        inst.refresh_from_db()
        self.assertEqual((inst.status, inst.remote_cleanup_status),
                         ('cancelled', 'deletingData'))

    def test_a_cancel_from_statuses_the_row_has_left_loses_with_the_row_s_status(self):
        inst = self.instance('waiting')
        PluginInstance.objects.filter(pk=inst.pk).update(status='started')

        cancelled = inst.cancel(['created', 'waiting'])

        self.assertEqual(cancelled, StatusWrite(False, 'started'))
        self.assertEqual(PluginInstance.objects.get(pk=inst.pk).status, 'started')

    def test_a_job_s_cancel_arms_and_submits_the_cleanup_only_with_a_remote_job(self):
        for status, armed in (('created', False), ('waiting', False), ('started', True)):
            with self.subTest(status=status):
                inst = self.instance(status)
                job = PluginInstanceAppJob(inst)
                job.enqueue_run = mock.Mock()

                self.assertTrue(job.cancel_exec('CODE19'))

                inst.refresh_from_db()
                self.assertEqual((inst.status, inst.error_code), ('cancelled', 'CODE19'))
                self.assertEqual(inst.remote_cleanup_status,
                                 'deletingData' if armed else 'notStarted')
                self.assertEqual(job.enqueue_run.call_args_list,
                                 [mock.call('PluginInstanceDeleteJob')] if armed else [])

    def test_a_job_s_cancel_another_writer_beat_schedules_the_cleanup_if_nothing_did(self):
        inst = self.instance('started')
        job = PluginInstanceAppJob(inst)
        job.enqueue_run = mock.Mock()
        # its job ended meanwhile, by a write that left the cleanup unarmed
        PluginInstance.objects.filter(pk=inst.pk).update(status='finishedSuccessfully')

        self.assertEqual(job.cancel_exec('CODE19', 'started'),
                         StatusWrite(False, 'finishedSuccessfully'))
        job.cancel_exec('CODE19', 'started')  # its cleanup armed: nothing more

        inst.refresh_from_db()
        self.assertEqual((inst.status, inst.error_code, inst.remote_cleanup_status),
                         ('finishedSuccessfully', '', 'deletingData'))
        job.enqueue_run.assert_called_once_with('PluginInstanceDeleteJob')


class TwoStatusChecksTests(PluginInstanceFixture, TestCase):
    """
    Two status checks of a copy job that has finished, each holding the instance as it
    was when it started: the overlap that submitted the app job twice.
    """

    def checks(self):
        inst = self.instance('copying')
        jobs = []

        for _ in range(2):
            job = PluginInstanceCopyJob(PluginInstance.objects.get(pk=inst.pk))
            job._get_status = mock.Mock(return_value=FINISHED)
            job.enqueue_run = mock.Mock()
            jobs.append(job)
        return inst, jobs

    def test_two_checks_in_turn_submit_the_app_job_once(self):
        inst, jobs = self.checks()

        for job in jobs:
            job.check_exec_status()

        submissions = [c for job in jobs for c in job.enqueue_run.call_args_list]
        self.assertEqual(submissions, [mock.call('PluginInstanceAppJob')])
        self.assertEqual(PluginInstance.objects.get(pk=inst.pk).status, 'scheduled')

    def test_two_checks_that_both_found_the_copy_finished_submit_the_app_job_once(self):
        inst, jobs = self.checks()

        # both read 'copying' and the remote's status before either moved the instance
        for job in jobs:
            job.handle_finished_successfully_status()

        submissions = [c for job in jobs for c in job.enqueue_run.call_args_list]
        self.assertEqual(submissions, [mock.call('PluginInstanceAppJob')])
        self.assertEqual(PluginInstance.objects.get(pk=inst.pk).status, 'scheduled')


class ConcurrentStatusChecksTests(PluginInstanceFixture, TransactionTestCase):
    """
    The same two checks in two threads, both of which read the remote's status before
    either writes anything.
    """

    def test_two_concurrent_checks_submit_the_app_job_once(self):
        inst = self.instance('copying')
        both_fetched = threading.Barrier(2, timeout=10)
        jobs, errors = [], []

        def fetched(*args, **kwargs):
            both_fetched.wait()
            return FINISHED

        for _ in range(2):
            job = PluginInstanceCopyJob(PluginInstance.objects.get(pk=inst.pk))
            job._get_status = mock.Mock(side_effect=fetched)
            job.enqueue_run = mock.Mock()
            jobs.append(job)

        def check(job):
            try:
                job.check_exec_status()
            except Exception as e:  # noqa: BLE001 - reported below
                errors.append(e)
            finally:
                connection.close()

        # the pool holds two connections a process: each thread takes one
        connection.close()
        threads = [threading.Thread(target=check, args=(job,)) for job in jobs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)

        self.assertEqual(errors, [])
        submissions = [c for job in jobs for c in job.enqueue_run.call_args_list]
        self.assertEqual(submissions, [mock.call('PluginInstanceAppJob')])
        self.assertEqual(PluginInstance.objects.get(pk=inst.pk).status, 'scheduled')


class StatusWriteGuardTests(SimpleTestCase):
    """
    Every write of a plugin instance's status goes through ``PluginInstance.transition``
    or its queryset's: a status assigned and saved, or set by a plain UPDATE, is the lost
    update #152 removed. What it cannot see is a whole-row ``save()`` of an instance read
    before: the job classes and the views write named fields only.
    """

    # the implementation, and the PACS queries and retrieves, which have a status of
    # their own
    ALLOWED = {'plugininstances/models.py', 'pacsfiles/models.py'}
    UPDATES = {'update', 'bulk_update', 'update_or_create'}

    @classmethod
    def status_writes(cls, tree):
        """
        The lines of ``tree`` that write a ``status``: an assignment to an attribute of
        that name, ``setattr``, an UPDATE that names it (a keyword, a key of ``**`` or
        ``defaults``, a field of ``bulk_update``) and a ``save`` whose ``update_fields``
        does.
        """
        def names_status(nodes):
            return any(isinstance(n, ast.Constant) and n.value == 'status'
                       for node in nodes for n in ast.walk(node))

        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(t, ast.Attribute) and t.attr == 'status' for t in targets):
                    yield node.lineno
            elif isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id',
                                                                                None)
                values = node.args + [k.value for k in node.keywords]

                if ((name in cls.UPDATES and (any(k.arg == 'status' for k in node.keywords)
                                              or names_status(values)))
                        or (name == 'setattr' and names_status(node.args[1:2]))
                        or (name == 'save' and names_status(
                            [k.value for k in node.keywords if k.arg == 'update_fields']))):
                    yield node.lineno

    def test_only_the_transition_functions_write_a_plugin_instance_s_status(self):
        root = Path(settings.BASE_DIR)
        found = []

        for path in sorted(root.rglob('*.py')):
            name = path.relative_to(root).as_posix()

            if '/tests/' in name or '/migrations/' in name or name in self.ALLOWED:
                continue

            tree = ast.parse(path.read_text(), name)
            found += [f'{name}:{line}' for line in self.status_writes(tree)]

        self.assertEqual(found, [], 'write the status with PluginInstance.transition')

    def test_the_guard_sees_the_writes_a_pattern_would_miss(self):
        code = """
inst.status = 'started'
qs.update(end_date=timezone.now(), status='cancelled')
qs.update(**{'status': 'cancelled'})
setattr(inst, 'status', 'started')
PluginInstance.objects.bulk_update(insts, ['status'])
inst.save(update_fields=['status', 'end_date'])
qs.update(remote_cleanup_status='complete')
inst.save(update_fields=['end_date'])
Response(data, status=status.HTTP_200_OK)
"""
        self.assertEqual(list(self.status_writes(ast.parse(code))), [2, 3, 4, 5, 6, 7])
