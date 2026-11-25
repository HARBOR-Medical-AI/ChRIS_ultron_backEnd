"""
Tests for the conditional writes of plugin instances (HARBOR-planning #152): the status
transitions, what follows them only for the task that won, and the tasks that race.
"""

import ast
import contextlib
import io
import logging
import threading
import zipfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from celery.exceptions import SoftTimeLimitExceeded
from kombu.exceptions import OperationalError
from pfconclient.exceptions import PfconRequestException

from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core.cache import acquire_lock, release_lock
from core.celery import app
from core.models import ChrisFolder, ChrisInstance
from plugins.models import PluginMeta, Plugin, PluginParameter, ComputeResource
from plugininstances import tasks
from plugininstances.models import (PluginInstance, PluginInstanceLock, StatusWrite,
                                    StrParameter)
from plugininstances.services.copyjobs import PluginInstanceCopyJob
from plugininstances.services.pluginjobs import PluginInstanceAppJob
from plugininstances.services.uploadjobs import PluginInstanceUploadJob
from plugininstances.utils import cancel_plugin_instances, run_if_ready


COMPUTE_RESOURCE_URL = settings.COMPUTE_RESOURCE_URL

FINISHED = {'compute': {'status': 'finishedSuccessfully', 'logs': '', 'jid': 'x'}}

# the claim of a newer run of a registration
NEWER_RUN = 'f' * 32


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
                ('plugininstances.tasks.register_plugin_instance_output', (inst.pk,), 1),
                ('plugininstances.tasks.register_large_plugin_instance_output',
                 (inst.pk,), 1),
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

    def test_the_stuck_instance_cancellers_pass_their_code_and_the_status_they_found(self):
        long_ago = timezone.now() - timedelta(hours=5)
        sweeps = (
            ('scheduled', 'start_date', 'stuck in scheduled status',
             tasks.cancel_plugin_instances_stuck_in_scheduled_status),
            ('registeringFiles', 'end_date', 'stuck registering its files',
             tasks.cancel_plugin_instances_stuck_in_lock))

        for status, since, line, sweep in sweeps:
            with self.subTest(status=status):
                inst = self.instance(status)
                PluginInstance.objects.filter(pk=inst.pk).update(**{since: long_ago})

                with mock.patch.object(tasks.cancel_plugin_instance_job, 'delay') as cancel, \
                        self.assertLogs('plugininstances.tasks', 'ERROR') as logs:
                    sweep()

                cancel.assert_called_once_with(inst.pk, error_code='CODE18', source=status)
                self.assertIn(line, logs.output[0])

                # its job went out (or its registration ended) before the cancel ran
                PluginInstance.objects.filter(pk=inst.pk).update(status='started')
                tasks.cancel_plugin_instance_job(*cancel.call_args.args,
                                                 **cancel.call_args.kwargs)
                inst.refresh_from_db()
                self.assertEqual((inst.status, inst.error_code), ('started', ''))

    def test_cancelling_many_instances_reads_the_job_id_prefix_once(self):
        root = self.instance('cancelled')
        children = [self.instance('waiting', previous=root) for _ in range(3)]
        started = [self.instance('started') for _ in range(3)]
        load = mock.patch.object(ChrisInstance, 'load', wraps=ChrisInstance.load)

        for name, cancel in (
                ('the beat', tasks.cancel_waiting_plugin_instances),
                ('a tree', lambda: self.assertEqual(cancel_plugin_instances(started), 3))):
            with self.subTest(name=name), load as loaded:
                cancel()
                loaded.assert_called_once_with()

        self.assertEqual(
            {PluginInstance.objects.get(pk=inst.pk).status for inst in children + started},
            {'cancelled'})

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

    def test_the_cancel_that_wins_logs_the_final_status_once(self):
        inst = self.instance('started')
        twin = PluginInstance.objects.get(pk=inst.pk)
        job_id = ChrisInstance.load().job_id_prefix + str(inst.pk)
        logging.disable(logging.NOTSET)

        with self.assertLogs('plugininstances', 'INFO') as logs:
            self.assertTrue(inst.cancel(error_code='CODE19'))
            self.assertFalse(twin.cancel())  # cancelled already: logs nothing

        self.assertEqual(logs.output, [
            f"INFO:plugininstances.models:Saved plugin instance final status for job "
            f"{job_id} as 'cancelled' with error code CODE19"])

    def test_the_periodic_cancel_of_waiting_children_logs_each_final_status(self):
        failed = self.instance('finishedWithError')
        orphans = [self.instance('waiting', previous=failed) for _ in range(2)]
        logging.disable(logging.NOTSET)

        with self.assertLogs('plugininstances', 'INFO') as logs:
            tasks.cancel_waiting_plugin_instances()

        final = [line for line in logs.output if 'final status' in line]
        self.assertEqual(len(final), 2)
        for orphan in orphans:
            self.assertEqual(PluginInstance.objects.get(pk=orphan.pk).status, 'cancelled')

    def test_a_waiting_ts_instance_with_a_failed_parent_is_cancelled(self):
        (meta, _) = PluginMeta.objects.get_or_create(name='pl-topologicalcopy', type='ts')
        (ts_plugin, _) = Plugin.objects.get_or_create(meta=meta, version='0.1')
        ts_plugin.compute_resources.set([self.compute_resource])
        parent = self.instance('finishedSuccessfully')
        failed = self.instance('cancelled')
        inst = PluginInstance.objects.create(plugin=ts_plugin, owner=self.user,
                                             previous=parent,
                                             compute_resource=self.compute_resource)
        PluginInstance.objects.filter(pk=inst.pk).update(status='waiting')
        plugin_param = PluginParameter.objects.create(plugin=ts_plugin,
                                                      name='plugininstances',
                                                      flag='--plugininstances')
        StrParameter.objects.create(plugin_inst=inst, plugin_param=plugin_param,
                                    value=f'{parent.pk},{failed.pk}')

        tasks.cancel_waiting_plugin_instances()

        self.assertEqual(PluginInstance.objects.get(pk=inst.pk).status, 'cancelled')

    def test_a_new_instance_whose_parent_failed_is_cancelled_with_its_final_status(self):
        failed = self.instance('finishedWithError')
        child = PluginInstance.objects.create(plugin=self.plugins['ds'], owner=self.user,
                                              previous=failed,
                                              compute_resource=self.compute_resource)
        logging.disable(logging.NOTSET)

        with self.assertLogs('plugininstances', 'INFO') as logs:
            run_if_ready(child, failed)

        self.assertEqual(PluginInstance.objects.get(pk=child.pk).status, 'cancelled')
        self.assertIn("as 'cancelled'", logs.output[0])

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


class RegistrationTaskTests(PluginInstanceFixture, TestCase):

    def setUp(self):
        super().setUp()
        self.task = app.tasks['plugininstances.tasks.register_plugin_instance_output']
        self.large = app.tasks['plugininstances.tasks.register_large_plugin_instance_output']

    def test_an_instance_no_longer_registering_files_or_gone_is_left_alone(self):
        inst = self.instance('cancelled')

        with mock.patch.object(tasks.PluginInstanceAppJob,
                               'registers_at_most') as count, \
                mock.patch.object(tasks.PluginInstanceAppJob,
                                  'register_output') as register, \
                mock.patch.object(tasks.register_large_plugin_instance_output,
                                  'delay') as large:
            tasks.register_plugin_instance_output(inst.pk)
            tasks.register_large_plugin_instance_output(inst.pk)
            logging.disable(logging.NOTSET)  # the line asserted below
            with self.assertLogs('plugininstances.tasks', 'INFO') as gone:
                tasks.register_large_plugin_instance_output(inst.pk + 1000)

        count.assert_not_called()
        register.assert_not_called()
        large.assert_not_called()
        # deleted with its feed while the task waited, say: no error
        self.assertEqual([record.levelname for record in gone.records], ['INFO'])
        self.assertIn('not found when registering its output files', gone.output[0])

    def test_a_soft_time_limit_runs_it_again_then_cancels_with_code13(self):
        inst = self.instance('registeringFiles')

        with mock.patch.object(tasks.PluginInstanceAppJob,
                               'registers_at_most') as count, \
                mock.patch.object(tasks.PluginInstanceAppJob, 'register_output',
                                  side_effect=SoftTimeLimitExceeded()), \
                mock.patch.object(tasks.PluginInstanceAppJob, 'enqueue_run'), \
                mock.patch.object(type(self.large), 'retry') as retry, \
                mock.patch.object(type(self.large), 'apply_async') as again:
            with self.assertLogs('plugininstances.tasks', 'ERROR') as first:
                tasks.register_large_plugin_instance_output(inst.pk)
            self.assertEqual(PluginInstance.objects.get(pk=inst.pk).status,
                             'registeringFiles')

            last = SimpleNamespace(retries=tasks.REGISTRATION_RETRIES)
            with mock.patch.object(type(self.large), 'request',
                                   new_callable=mock.PropertyMock, return_value=last), \
                    self.assertLogs('plugininstances.tasks', 'ERROR') as final:
                tasks.register_large_plugin_instance_output(inst.pk)

        count.assert_not_called()  # the large queue's task counts nothing
        self.assertIn('reached its time limit (attempt 1)', first.output[0])
        self.assertIn(f'(attempt {tasks.REGISTRATION_RETRIES + 1})', final.output[0])
        # no Task.retry, whose failed publish would be a Reject that no handler sees
        retry.assert_not_called()
        again.assert_called_once_with((inst.pk,), retries=1)
        inst.refresh_from_db()
        self.assertEqual((inst.status, inst.error_code), ('cancelled', 'CODE13'))

    def test_a_soft_time_limit_on_the_register_queue_sends_it_on_as_its_next_attempt(self):
        inst = self.instance('registeringFiles')

        with mock.patch.object(tasks.PluginInstanceAppJob, 'registers_at_most',
                               return_value=True), \
                mock.patch.object(tasks.PluginInstanceAppJob, 'register_output',
                                  side_effect=SoftTimeLimitExceeded()), \
                mock.patch.object(type(self.task), 'retry') as retry, \
                mock.patch.object(tasks.register_large_plugin_instance_output,
                                  'apply_async') as large, \
                self.assertLogs('plugininstances.tasks', 'ERROR') as logs:
            tasks.register_plugin_instance_output(inst.pk)

        retry.assert_not_called()
        large.assert_called_once_with((inst.pk,), retries=1)
        self.assertIn('reached its time limit (attempt 1)', logs.output[0])
        self.assertEqual(PluginInstance.objects.get(pk=inst.pk).status, 'registeringFiles')

    def test_a_registration_of_few_files_runs_in_the_task_it_was_sent_to(self):
        inst = self.instance('registeringFiles')

        with mock.patch.object(tasks.PluginInstanceAppJob, 'registers_at_most',
                               return_value=True) as count, \
                mock.patch.object(tasks.PluginInstanceAppJob,
                                  'register_output') as register, \
                mock.patch.object(tasks.register_large_plugin_instance_output,
                                  'delay') as large:
            tasks.register_plugin_instance_output(inst.pk)

        count.assert_called_once_with(tasks.REGISTER_FAST_MAX_FILES)
        register.assert_called_once_with()
        large.assert_not_called()

    def test_any_other_registration_goes_on_and_the_log_says_why(self):
        inst = self.instance('registeringFiles')
        logging.disable(logging.NOTSET)  # the lines asserted below
        cases = (
            ({'return_value': False}, ['INFO'],
             f'more than {tasks.REGISTER_FAST_MAX_FILES} files'),
            ({'side_effect': OSError('no storage')}, ['WARNING', 'INFO'],
             'its files could not be counted'),
            ({'side_effect': SoftTimeLimitExceeded()}, ['WARNING', 'INFO'],
             'its files could not be counted'),
            (None, ['INFO'], 'the remote sends its output as a zip'))

        for answer, levels, why in cases:
            if answer is None:  # the real count, of an out-of-network remote's output
                ComputeResource.objects.filter(pk=self.compute_resource.pk).update(
                    compute_innetwork=False)
                count = contextlib.nullcontext()
            else:
                count = mock.patch.object(tasks.PluginInstanceAppJob, 'registers_at_most',
                                          **answer)
            with self.subTest(why=why, levels=levels), count, \
                    mock.patch.object(tasks.PluginInstanceAppJob,
                                      'register_output') as register, \
                    mock.patch.object(tasks.register_large_plugin_instance_output,
                                      'delay') as large, \
                    self.assertLogs('plugininstances.tasks', 'INFO') as logs:
                tasks.register_plugin_instance_output(inst.pk)

            register.assert_not_called()
            large.assert_called_once_with(inst.pk)
            self.assertEqual([record.levelname for record in logs.records], levels)
            self.assertIn(f'on to the register_large queue: {why}', logs.output[-1])

    def test_a_run_a_newer_one_took_over_gives_up_nothing(self):
        inst = self.instance('registeringFiles')
        job = PluginInstanceAppJob(inst)
        job.enqueue_run = mock.Mock()
        job._registration_token = job._claim_registration()
        PluginInstanceLock.objects.filter(plugin_inst_id=inst.pk).update(owner_token=NEWER_RUN)

        self.assertIsNone(job.give_up_registration('CODE13'))
        self.assertEqual(PluginInstance.objects.get(pk=inst.pk).status, 'registeringFiles')

        job._registration_token = NEWER_RUN  # the run that owns it gives up
        self.assertTrue(job.give_up_registration('CODE13'))
        inst.refresh_from_db()
        self.assertEqual((inst.status, inst.error_code), ('cancelled', 'CODE13'))

    def test_a_run_a_newer_one_took_over_while_it_asked_the_remote_fetches_nothing(self):
        inst = self.instance('registeringFiles')
        job = PluginInstanceAppJob(inst)
        job.enqueue_run = mock.Mock()
        job._get_job_json_data = mock.Mock()

        def claimed_meanwhile(job_type, job_id):
            PluginInstanceLock.objects.filter(plugin_inst_id=inst.pk).update(
                owner_token=NEWER_RUN)
            return {'compute': {'status': 'finishedSuccessfully'}}

        job._get_status = mock.Mock(side_effect=claimed_meanwhile)
        logging.disable(logging.NOTSET)

        with self.assertLogs('plugininstances.services', 'INFO') as logs:
            job.register_output()

        job._get_job_json_data.assert_not_called()
        self.assertEqual(PluginInstance.objects.get(pk=inst.pk).status, 'registeringFiles')
        self.assertIn('Registered 0 output files', logs.output[-1])
        self.assertIn('a newer run owns it', logs.output[-1])

    def overtaken(self, inst):
        """
        An app job of ``inst`` whose registration a newer run claimed after this one's.
        """
        job = PluginInstanceAppJob(inst)
        job._registration_token = job._claim_registration()
        PluginInstanceLock.objects.filter(plugin_inst_id=inst.pk).update(
            owner_token=NEWER_RUN)
        return job

    def test_an_overtaken_run_renames_no_object_and_creates_no_folder_or_row(self):
        inst = self.instance('registeringFiles')
        job = self.overtaken(inst)
        job.storage_manager = mock.Mock()
        job._expect('sub/a,b.txt')
        folders = ChrisFolder.objects.count()

        self.assertTrue(job.registration_overtaken())
        job._register_output_files()

        job.storage_manager.walk.assert_not_called()
        job.storage_manager.sanitize_obj_names.assert_not_called()
        self.assertEqual(ChrisFolder.objects.count(), folders)
        self.assertEqual(job._files_registered, 0)

    def test_a_run_overtaken_after_its_files_were_checked_writes_no_link_or_copy(self):
        inst = self.instance('registeringFiles')
        job = PluginInstanceAppJob(inst)
        job._registration_token = job._claim_registration()
        job.pfcon_client.pfcon_innetwork = True
        job._get_job_json_data = mock.Mock(return_value={})
        job.expect_json_files = mock.Mock(
            side_effect=lambda content: PluginInstanceLock.objects.filter(
                plugin_inst_id=inst.pk).update(owner_token=NEWER_RUN))
        writers = ('_handle_unextpath_parameters',
                   '_handle_unextpath_parameters_innetwork_filesystem',
                   '_handle_ts_unextracted_input_objs', '_register_output_files')
        for name in writers:
            setattr(job, name, mock.Mock())

        self.assertEqual(job.register_output_files_on_success(), 'cancelled')

        for name in writers:
            getattr(job, name).assert_not_called()

    def test_a_soft_time_limit_of_an_overtaken_run_tries_nothing_again(self):
        inst = self.instance('registeringFiles')

        def overtaken(job):
            job._registration_token = job._claim_registration()
            PluginInstanceLock.objects.filter(plugin_inst_id=inst.pk).update(
                owner_token=NEWER_RUN)
            raise SoftTimeLimitExceeded()

        for task in (self.task, self.large):
            with self.subTest(task=task.name), \
                    mock.patch.object(tasks.PluginInstanceAppJob, 'registers_at_most',
                                      return_value=True), \
                    mock.patch.object(tasks.PluginInstanceAppJob, 'register_output',
                                      autospec=True, side_effect=overtaken), \
                    mock.patch.object(tasks.register_large_plugin_instance_output,
                                      'apply_async') as large, \
                    self.assertLogs('plugininstances.tasks', 'ERROR'):
                task(inst.pk)

            large.assert_not_called()
            inst.refresh_from_db()
            self.assertEqual((inst.status, inst.error_code), ('registeringFiles', ''))

    def test_the_soft_time_limit_in_a_step_is_no_failure_of_that_step(self):
        inst = self.instance('registeringFiles')
        output = inst.get_output_path()
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as zf:
            zf.writestr('out.txt', 'data')

        link = f'{output}/in.chrislink'

        for step, call, storage in (
                ('walk', lambda job: job.expect_json_files(
                    {'job_output_path': output, 'rel_file_paths': ['out.txt']})
                 or job._register_output_files(), {}),
                ('upload_obj', lambda job: job.unpack_zip_file(archive.getvalue()), {}),
                ('ls of a ts input', lambda job: job.find_all_storage_object_paths(
                    output, set(), set()), {'ls.side_effect': SoftTimeLimitExceeded()}),
                ('download_obj of a ts input', lambda job: job.find_all_storage_object_paths(
                    output, set(), set()), {'ls.return_value': [link]})):
            with self.subTest(step=step):
                job = PluginInstanceAppJob(PluginInstance.objects.get(pk=inst.pk))
                job.storage_manager = mock.Mock(**(storage or {
                    f'{step}.side_effect': SoftTimeLimitExceeded()}))
                job.storage_manager.download_obj.side_effect = SoftTimeLimitExceeded()
                logging.disable(logging.NOTSET)

                with self.assertRaises(SoftTimeLimitExceeded), \
                        self.assertNoLogs('plugininstances.services', 'ERROR'):
                    call(job)
                self.assertEqual(job.c_plugin_inst.error_code, '')


class FollowerSchedulingTests(PluginInstanceFixture, TestCase):
    """
    A registration that ends its instance 'finishedSuccessfully' schedules the instances of
    its feed that are ready at once, rather than leaving them to the beat's next pass.
    """

    def ts_instance(self, previous, parents):
        """
        A waiting 'ts' instance after ``previous`` whose 'plugininstances' are ``parents``.
        """
        (meta, _) = PluginMeta.objects.get_or_create(name='pl-topologicalcopy', type='ts')
        (ts_plugin, _) = Plugin.objects.get_or_create(meta=meta, version='0.1')
        ts_plugin.compute_resources.set([self.compute_resource])
        inst = PluginInstance.objects.create(plugin=ts_plugin, owner=self.user,
                                             previous=previous,
                                             compute_resource=self.compute_resource)
        (param, _) = PluginParameter.objects.get_or_create(
            plugin=ts_plugin, name='plugininstances', flag='--plugininstances')
        StrParameter.objects.create(plugin_inst=inst, plugin_param=param,
                                    value=','.join(str(p.pk) for p in parents))
        PluginInstance.objects.filter(pk=inst.pk).update(status='waiting')
        return inst

    def register(self, inst, final):
        """
        Run the register queue's task for ``inst``, whose registration ends it ``final``
        (None: a newer run owns it), and return the mocked submission of jobs.
        """
        def register_output(job):
            if final:
                job.c_plugin_inst.transition(final, 'registeringFiles')

        with mock.patch.object(tasks.PluginInstanceAppJob, 'registers_at_most',
                               return_value=True), \
                mock.patch.object(tasks.PluginInstanceAppJob, 'register_output',
                                  autospec=True, side_effect=register_output), \
                mock.patch.object(tasks.run_plugin_instance_job, 'delay') as submit:
            tasks.register_plugin_instance_output(inst.pk)
        return submit

    def status(self, inst):
        return PluginInstance.objects.get(pk=inst.pk).status

    def test_a_successful_registration_schedules_the_ready_instances_of_its_feed(self):
        root = self.instance('finishedSuccessfully')
        parent = self.instance('registeringFiles', previous=root)
        child = self.instance('waiting', previous=parent)
        sibling = self.instance('started', previous=root)
        merge = self.ts_instance(parent, (parent, sibling))  # waits on the sibling
        elsewhere = self.instance('waiting', previous=self.instance('finishedSuccessfully'))

        submit = self.register(parent, 'finishedSuccessfully')

        submit.assert_called_once_with(child.pk, 'PluginInstanceCopyJob')
        self.assertEqual(self.status(child), 'copying')
        self.assertEqual(self.status(merge), 'waiting')
        self.assertEqual(self.status(elsewhere), 'waiting')  # another feed's: the beat's

    def test_the_last_parent_of_a_merge_to_finish_schedules_it_whichever_it_is(self):
        root = self.instance('finishedSuccessfully')
        first = self.instance('finishedSuccessfully', previous=root)
        last = self.instance('registeringFiles', previous=root)
        merge = self.ts_instance(first, (first, last))  # not after the one that finishes

        submit = self.register(last, 'finishedSuccessfully')

        submit.assert_called_once_with(merge.pk, 'PluginInstanceCopyJob')

    def test_any_other_end_of_a_registration_schedules_nothing(self):
        for final in ('finishedWithError', 'cancelled', None):
            with self.subTest(final=final):
                root = self.instance('finishedSuccessfully')
                parent = self.instance('registeringFiles', previous=root)
                ready = self.instance('waiting', previous=root)  # the beat's to schedule

                submit = self.register(parent, final)

                submit.assert_not_called()
                self.assertEqual(self.status(ready), 'waiting')

    def test_a_real_successful_registration_schedules_the_child(self):
        parent = self.instance('registeringFiles')
        child = self.instance('waiting', previous=parent)
        storage = PluginInstanceAppJob(parent).storage_manager
        name = f'{parent.get_output_path()}/a.txt'
        self.addCleanup(storage.delete_path, parent.get_output_path())
        storage.upload_obj(name, b'x')

        job_class = tasks.PluginInstanceAppJob
        found = {'job_output_path': parent.get_output_path(), 'rel_file_paths': ['a.txt']}
        with mock.patch.object(job_class, '_get_status', return_value=FINISHED), \
                mock.patch.object(job_class, '_get_job_json_data', return_value=found), \
                mock.patch.object(job_class, 'enqueue_run'), \
                mock.patch.object(tasks.run_plugin_instance_job, 'delay') as submit:
            tasks.register_plugin_instance_output(parent.pk)

        self.assertEqual(self.status(parent), 'finishedSuccessfully')
        submit.assert_called_once_with(child.pk, 'PluginInstanceCopyJob')

    def test_a_failure_to_schedule_them_is_logged_and_left_to_the_beat(self):
        parent = self.instance('registeringFiles')
        child = self.instance('waiting', previous=parent)

        with mock.patch.object(tasks, 'schedule_ready_plugin_instances',
                               side_effect=RuntimeError('no database')), \
                self.assertLogs('plugininstances.tasks', 'ERROR') as logs:
            self.register(parent, 'finishedSuccessfully')

        self.assertIn('the beat schedules those still waiting', logs.output[0])
        self.assertEqual(self.status(parent), 'finishedSuccessfully')
        self.assertEqual(self.status(child), 'waiting')
        with mock.patch.object(tasks.run_plugin_instance_job, 'delay') as submit:
            tasks.schedule_waiting_plugin_instances()
        submit.assert_called_once_with(child.pk, 'PluginInstanceCopyJob')

    def test_an_instance_that_cannot_be_scheduled_stops_no_other(self):
        root = self.instance('finishedSuccessfully')
        broken, ready = (self.instance('waiting', previous=root) for _ in range(2))
        PluginInstance.objects.filter(pk=broken.pk).update(compute_resource=None)

        with mock.patch.object(tasks.run_plugin_instance_job, 'delay') as submit, \
                self.assertLogs('plugininstances.tasks', 'ERROR') as logs:
            tasks.schedule_ready_plugin_instances(feed_id=root.feed_id)

        submit.assert_called_once_with(ready.pk, 'PluginInstanceCopyJob')
        self.assertIn(f'Could not schedule plugin instance {broken.pk}', logs.output[0])
        self.assertEqual(self.status(broken), 'waiting')

    def test_a_run_task_that_cannot_be_published_cancels_its_instance_and_ends_the_pass(self):
        root = self.instance('finishedSuccessfully')
        ready = {self.instance('waiting', previous=root).pk for _ in range(2)}

        with mock.patch.object(tasks.run_plugin_instance_job, 'delay',
                               side_effect=OperationalError('broker gone')) as submit, \
                self.assertLogs('plugininstances.tasks', 'ERROR') as logs, \
                self.assertRaises(tasks.PublishError):
            tasks.schedule_ready_plugin_instances(feed_id=root.feed_id)

        submit.assert_called_once()  # the pass ended at the first that failed
        (failed_id, _), = [c.args for c in submit.call_args_list]
        (left_id,) = ready - {failed_id}
        failed = PluginInstance.objects.get(pk=failed_id)
        self.assertEqual((failed.status, failed.error_code, failed.remote_cleanup_status),
                         ('cancelled', 'CODE01', 'deletingData'))
        self.assertEqual(self.status(PluginInstance(pk=left_id)), 'waiting')  # the next pass's
        self.assertIn('[CODE01,', logs.output[0])

    def test_scheduling_an_instance_starts_the_clock_of_its_job_s_timeout(self):
        root = self.instance('finishedSuccessfully')
        child = self.instance('waiting', previous=root)
        long_ago = timezone.now() - timedelta(hours=2)  # it waited that long for its parent

        for requires_copy, status in ((True, 'copying'), (False, 'scheduled')):
            with self.subTest(status=status):
                ComputeResource.objects.filter(pk=self.compute_resource.pk).update(
                    compute_requires_copy_job=requires_copy)
                PluginInstance.objects.filter(pk=child.pk).update(
                    status='waiting', start_date=long_ago, end_date=long_ago)

                with mock.patch.object(tasks.run_plugin_instance_job, 'delay'):
                    tasks.schedule_ready_plugin_instances(feed_id=root.feed_id)

                child = PluginInstance.objects.get(pk=child.pk)
                self.assertEqual(child.status, status)
                self.assertGreater(child.start_date, long_ago + timedelta(hours=1))

    def test_the_beat_schedules_the_ready_instances_of_every_feed(self):
        children = [self.instance('waiting', previous=self.instance('finishedSuccessfully'))
                    for _ in range(2)]
        merge = self.ts_instance(children[0].previous, (children[0].previous,))

        with mock.patch.object(tasks.run_plugin_instance_job, 'delay') as submit:
            tasks.schedule_waiting_plugin_instances()

        self.assertEqual(sorted(c.args[0] for c in submit.call_args_list),
                         sorted([children[0].pk, children[1].pk, merge.pk]))


class CleanupTaskTests(PluginInstanceFixture, TestCase):
    """
    The remote cleanup's tasks: the delete job of its data and the deletion of the
    containers, which the periodic task drives phase by phase.
    """

    def cleaning(self, phase):
        inst = self.instance('cancelled')
        PluginInstance.objects.filter(pk=inst.pk).update(remote_cleanup_status=phase)
        return PluginInstance.objects.get(pk=inst.pk)

    def test_the_periodic_task_publishes_one_check_of_each_phase(self):
        data, containers = self.cleaning('deletingData'), self.cleaning('deletingContainers')
        self.cleaning('complete')

        with mock.patch.object(tasks.check_plugin_instance_cleanup_job,
                               'enqueue_once') as check, \
                mock.patch.object(tasks.delete_plugin_instance_containers_from_remote,
                                  'enqueue_once') as delete:
            tasks.handle_remote_cleanup()

        check.assert_called_once_with(data.pk)
        delete.assert_called_once_with(containers.pk)

    def test_the_cleanup_s_first_job_goes_out_from_a_task_of_its_own(self):
        inst = self.cleaning('deletingData')
        job = PluginInstanceCopyJob(inst)

        with mock.patch.object(tasks.run_plugin_instance_cleanup_job, 'delay') as cleanup, \
                mock.patch.object(tasks.run_plugin_instance_job, 'delay') as run:
            job.enqueue_run('PluginInstanceDeleteJob')

        cleanup.assert_called_once_with(inst.pk)
        run.assert_not_called()

    def test_the_cleanup_tasks_run_the_delete_job(self):
        inst = self.cleaning('deletingData')

        with mock.patch.object(tasks.PluginInstanceDeleteJob, 'run') as run, \
                mock.patch.object(tasks.PluginInstanceDeleteJob,
                                  'check_exec_status') as check:
            tasks.run_plugin_instance_cleanup_job(inst.pk)
            tasks.check_plugin_instance_cleanup_job(inst.pk)
            logging.disable(logging.NOTSET)  # the line asserted below
            with self.assertLogs('plugininstances.tasks', 'INFO') as logs:
                tasks.run_plugin_instance_cleanup_job(10 ** 9)  # gone: logged, nothing run

        run.assert_called_once_with()
        check.assert_called_once_with()
        self.assertEqual([record.levelname for record in logs.records], ['INFO'])
        self.assertIn('not found when running its remote cleanup', logs.output[0])

    def test_the_cleanup_of_an_instance_cancelled_before_its_job_ran_completes(self):
        # its output folder never reached storage, and pfcon skipped the delete job
        inst = self.cleaning('deletingContainers')
        storage = PluginInstanceCopyJob(inst).storage_manager
        self.assertFalse(storage.path_exists(inst.output_folder.path))
        job_class = tasks.PluginInstanceDeleteJob

        with mock.patch.object(job_class, 'delete_all_remote_containers',
                               return_value=True), \
                mock.patch.object(job_class, '_get_status',
                                  side_effect=PfconRequestException('gone', code=404)):
            tasks.delete_plugin_instance_containers_from_remote(inst.pk)

            inst.refresh_from_db()
            self.assertEqual(inst.remote_cleanup_status, 'complete')
            # its work done again, as when a run is stopped after it: no error either
            job_class(inst)._cleanup_plugin_instance_output_dir()

    def test_deleting_the_containers_counts_its_failures_and_fails_past_the_retries(self):
        inst = self.cleaning('deletingContainers')
        delete = mock.patch.object(tasks.PluginInstanceDeleteJob,
                                   'delete_all_remote_containers', return_value=False)

        with delete, mock.patch.object(tasks.PluginInstanceDeleteJob, 'complete') as complete:
            tasks.delete_plugin_instance_containers_from_remote(inst.pk)
            complete.assert_not_called()
            self.assertEqual(PluginInstance.objects.get(pk=inst.pk)
                             .remote_cleanup_retry_count, 1)

            PluginInstance.objects.filter(pk=inst.pk).update(
                remote_cleanup_retry_count=PluginInstance.MAX_REMOTE_CLEANUP_RETRIES)
            tasks.delete_plugin_instance_containers_from_remote(inst.pk)
            complete.assert_called_once_with('failed')

    def test_deleting_the_containers_skips_while_another_run_deletes_them(self):
        inst, other = self.cleaning('deletingContainers'), self.cleaning('deletingContainers')
        name = 'plugininstances.tasks.delete_plugin_instance_containers_from_remote'
        held = acquire_lock(f'{name}:{inst.pk}', 60)
        self.addCleanup(release_lock, held)

        with mock.patch.object(tasks.PluginInstanceDeleteJob,
                               'delete_all_remote_containers',
                               return_value=False) as delete:
            tasks.delete_plugin_instance_containers_from_remote(inst.pk)
            tasks.delete_plugin_instance_containers_from_remote(other.pk)

        delete.assert_called_once_with()  # the other instance's alone
        self.assertEqual(PluginInstance.objects.get(pk=inst.pk).remote_cleanup_retry_count, 0)
        self.assertEqual(tasks.CONTAINER_DELETION_LOCK_TTL,
                         app.tasks[name].time_limit)

    def test_deleting_the_containers_of_a_cleanup_in_another_phase_does_nothing(self):
        inst = self.cleaning('deletingData')

        with mock.patch.object(tasks.PluginInstanceDeleteJob,
                               'delete_all_remote_containers') as delete:
            tasks.delete_plugin_instance_containers_from_remote(inst.pk)

        delete.assert_not_called()


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
        self.assertEqual(submissions,
                         [mock.call('PluginInstanceAppJob', moved_to='scheduled')])
        self.assertEqual(PluginInstance.objects.get(pk=inst.pk).status, 'scheduled')

    def test_two_checks_that_both_found_the_copy_finished_submit_the_app_job_once(self):
        inst, jobs = self.checks()

        # both read 'copying' and the remote's status before either moved the instance
        for job in jobs:
            job.handle_finished_successfully_status()

        submissions = [c for job in jobs for c in job.enqueue_run.call_args_list]
        self.assertEqual(submissions,
                         [mock.call('PluginInstanceAppJob', moved_to='scheduled')])
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
        self.assertEqual(submissions,
                         [mock.call('PluginInstanceAppJob', moved_to='scheduled')])
        self.assertEqual(PluginInstance.objects.get(pk=inst.pk).status, 'scheduled')


class PublishFailureTests(PluginInstanceFixture, TestCase):
    """
    A task that alone moves an instance on from the status a transition just wrote and
    cannot be published (the broker gone): the instance is cancelled with CODE01 and its
    remote cleanup armed, rather than left in that status with no task.
    """

    BROKER_GONE = OperationalError('broker gone')

    def assert_given_up(self, inst, call, *args):
        """
        ``call(*args)`` raises PublishError, logged with CODE01, and leaves ``inst``
        cancelled with CODE01 and its remote cleanup armed.
        """
        with self.assertLogs('plugininstances.tasks', 'ERROR') as logs, \
                self.assertRaises(tasks.PublishError):
            call(*args)

        inst.refresh_from_db()
        self.assertEqual((inst.status, inst.error_code, inst.remote_cleanup_status),
                         ('cancelled', 'CODE01', 'deletingData'))
        self.assertIn('[CODE01,', '\n'.join(logs.output))

    def test_a_copy_job_that_ended_cannot_hand_on_to_the_app_job(self):
        inst = self.instance('copying')
        job = PluginInstanceCopyJob(inst)

        with mock.patch.object(tasks.run_plugin_instance_job, 'delay',
                               side_effect=self.BROKER_GONE):
            self.assert_given_up(inst, job.handle_finished_successfully_status)

    def test_an_app_job_that_ended_cannot_hand_on_to_the_upload_job(self):
        ComputeResource.objects.filter(pk=self.compute_resource.pk).update(
            compute_requires_upload_job=True)
        inst = self.instance('started')
        job = PluginInstanceAppJob(inst)

        with mock.patch.object(tasks.run_plugin_instance_job, 'delay',
                               side_effect=self.BROKER_GONE):
            self.assert_given_up(inst, job.handle_finished_successfully_status)

    def test_a_job_that_ended_cannot_hand_on_to_its_registration(self):
        for status, job_class in (('started', PluginInstanceAppJob),
                                  ('uploading', PluginInstanceUploadJob)):
            with self.subTest(status=status):
                inst = self.instance(status)
                job = job_class(inst)

                with mock.patch.object(tasks.register_plugin_instance_output, 'delay',
                                       side_effect=self.BROKER_GONE):
                    self.assert_given_up(inst, job.handle_finished_successfully_status)

    def test_a_registration_cannot_be_sent_on_to_the_register_large_queue(self):
        inst = self.instance('registeringFiles')

        with mock.patch.object(tasks.PluginInstanceAppJob, 'registers_at_most',
                               return_value=False), \
                mock.patch.object(tasks.register_large_plugin_instance_output, 'delay',
                                  side_effect=self.BROKER_GONE):
            self.assert_given_up(inst, tasks.register_plugin_instance_output, inst.pk)

    def test_a_registration_s_next_attempt_cannot_be_published(self):
        task = app.tasks['plugininstances.tasks.register_large_plugin_instance_output']

        for name in ('register_plugin_instance_output',
                     'register_large_plugin_instance_output'):
            with self.subTest(task=name):
                inst = self.instance('registeringFiles')

                with mock.patch.object(tasks.PluginInstanceAppJob, 'registers_at_most',
                                       return_value=True), \
                        mock.patch.object(tasks.PluginInstanceAppJob, 'register_output',
                                          side_effect=SoftTimeLimitExceeded()), \
                        mock.patch.object(type(task), 'apply_async',
                                          side_effect=self.BROKER_GONE):
                    self.assert_given_up(inst, getattr(tasks, name), inst.pk)

    def test_a_job_submitted_again_cannot_be_published_and_the_next_check_tries_again(self):
        inst = self.instance('copying')
        job = PluginInstanceCopyJob(inst)

        with mock.patch.object(tasks.run_plugin_instance_job, 'delay',
                               side_effect=self.BROKER_GONE), \
                self.assertRaises(OperationalError):
            job.handle_undefined_status()  # not a transition: the next poll does it again

        inst.refresh_from_db()
        self.assertEqual((inst.status, inst.error_code), ('copying', ''))

    def test_a_task_the_broker_stored_after_all_and_ran_first_is_no_failure(self):
        inst = self.instance('copying')
        job = PluginInstanceCopyJob(inst)

        def stored_and_ran(*_):
            # the broker kept the message, and its task moved the instance on, before the
            # publish gave up waiting for the broker's answer
            PluginInstance.objects.filter(pk=inst.pk).update(status='started')
            raise self.BROKER_GONE

        logging.disable(logging.INFO)  # the WARNING asserted below, and nothing else
        with mock.patch.object(tasks.run_plugin_instance_job, 'delay',
                               side_effect=stored_and_ran), \
                self.assertLogs('plugininstances.tasks', 'WARNING') as logs:
            job.handle_finished_successfully_status()

        inst.refresh_from_db()
        self.assertEqual((inst.status, inst.error_code), ('started', ''))
        self.assertEqual([record.levelname for record in logs.records], ['WARNING'])
        self.assertIn('the task went out', logs.output[0])

    def test_a_hand_off_that_is_published_sends_its_job_and_changes_nothing(self):
        for status, job_class_name in (('scheduled', 'PluginInstanceAppJob'),
                                       ('uploading', 'PluginInstanceUploadJob')):
            with self.subTest(status=status):
                inst = self.instance(status)
                job = PluginInstanceCopyJob(inst)  # enqueue_run is the base class's

                with mock.patch.object(tasks.run_plugin_instance_job, 'delay') as run:
                    job.enqueue_run(job_class_name, moved_to=status)

                run.assert_called_once_with(inst.pk, job_class_name)
                inst.refresh_from_db()
                self.assertEqual((inst.status, inst.error_code), (status, ''))

    def test_the_failure_handler_leaves_alone_a_task_that_could_not_publish(self):
        inst = self.instance('cancelled')
        sender = mock.Mock()
        sender.name = 'plugininstances.tasks.register_plugin_instance_output'

        with mock.patch.object(tasks.cancel_plugin_instance_job, 'delay') as cancel:
            tasks.cancel_plugin_inst_on_task_failure(
                sender=sender, args=(inst.pk,), exception=tasks.PublishError('gone'))

        cancel.assert_not_called()


class UploadTransitionTests(PluginInstanceFixture, TestCase):
    """
    The end of an upload job, which only object storages run through pfcon (the
    upload-job tests need Swift), so CI's storage never reaches it otherwise.
    """

    def test_two_checks_of_a_finished_upload_queue_one_registration(self):
        inst = self.instance('uploading')
        long_ago = timezone.now() - timedelta(hours=5)  # an upload that took that long
        PluginInstance.objects.filter(pk=inst.pk).update(end_date=long_ago)
        jobs = [PluginInstanceUploadJob(PluginInstance.objects.get(pk=inst.pk))
                for _ in range(2)]

        for job in jobs:
            job.enqueue_registration = mock.Mock()
        for job in jobs:
            job.handle_finished_successfully_status()

        self.assertEqual([job.enqueue_registration.call_count for job in jobs], [1, 0])
        inst.refresh_from_db()
        self.assertEqual(inst.status, 'registeringFiles')
        self.assertGreater(inst.end_date, long_ago)

        # the stuck registrations' sweep counts from its start: none yet
        with mock.patch.object(tasks.cancel_plugin_instance_job, 'delay') as cancel:
            tasks.cancel_plugin_instances_stuck_in_lock()
        cancel.assert_not_called()

    def test_a_timed_out_upload_is_cancelled_with_its_code_and_its_cleanup(self):
        inst = self.instance('uploading')
        job = PluginInstanceUploadJob(inst)
        job._submit_remote_cleanup = mock.Mock()

        self.assertTrue(job.cancel_exec('CODE13', 'uploading'))

        inst.refresh_from_db()
        self.assertEqual((inst.status, inst.error_code, inst.remote_cleanup_status),
                         ('cancelled', 'CODE13', 'deletingData'))
        job._submit_remote_cleanup.assert_called_once_with()


class StatusCheckBeatTests(PluginInstanceFixture, TestCase):

    def test_the_beat_walks_the_running_instances_in_random_order(self):
        self.instance('started')

        with mock.patch.object(tasks.check_plugin_instance_job_exec_status,
                               'enqueue_once') as enqueue, \
                CaptureQueriesContext(connection) as queries:
            tasks.check_running_plugin_instances_exec_status()

        enqueue.assert_called_once()
        self.assertTrue(any('ORDER BY RANDOM()' in query['sql']
                            for query in queries.captured_queries))


class StatusWriteGuardTests(SimpleTestCase):
    """
    Every write of a plugin instance's status goes through ``PluginInstance.transition``:
    a status assigned and saved, or set by a plain UPDATE, is the lost update #152
    removed. What it cannot see is a whole-row ``save()`` of an instance read before: the
    job classes and the views write named fields only.
    """

    # the PACS queries and retrieves, which have a status of their own
    ALLOWED = {'pacsfiles/models.py'}
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
