
import io
import logging
import time
import uuid
from datetime import timedelta
from unittest import mock

from django.test import TestCase, tag
from django.contrib.auth.models import User
from django.conf import settings
from django.utils import timezone

from pfconclient import client as pfcon
from pfconclient.client import JobType
from pfconclient.exceptions import PfconRequestException

from core.models import ChrisInstance
from core.storage import connect_storage
from plugins.models import PluginMeta, Plugin, PluginParameter
from plugininstances.models import PluginInstance, PathParameter, ComputeResource
from plugininstances import tasks
from plugininstances.services import uploadjobs
from plugininstances.services.abstractjobs import PluginInstanceJob
from plugininstances.services.pluginjobs import PluginInstanceAppJob


COMPUTE_RESOURCE_URL = settings.COMPUTE_RESOURCE_URL
CHRIS_SUPERUSER_PASSWORD = settings.CHRIS_SUPERUSER_PASSWORD


class PluginInstanceUploadJobTests(TestCase):

    def setUp(self):
        if settings.STORAGE_ENV != 'swift':
            self.skipTest('Upload jobs only required for swift storage')

        # avoid cluttered console output (for instance logging all the http requests)
        logging.disable(logging.WARNING)
        logging.getLogger('plugininstances.services.uploadjobs').setLevel(logging.CRITICAL)

        # use a unique job_id_prefix to avoid pfcon job ID collisions with
        # stale jobs from prior test runs
        chris_inst = ChrisInstance.load()
        chris_inst.job_id_prefix = f'test-{uuid.uuid4().hex[:8]}-'
        chris_inst.save(update_fields=['job_id_prefix'])

        # create superuser chris (owner of root folders)
        self.chris_username = 'chris'
        self.chris_password = CHRIS_SUPERUSER_PASSWORD

        self.storage_manager = connect_storage(settings)

        self.plugin_fs_name = 'simplefsapp'
        self.username = 'foo'
        self.password = 'foo-pass'

        self.plg_data = {'description': 'A simple chris fs app demo',
                         'version': '0.1',
                         'dock_image': 'fnndsc/pl-simplefsapp',
                         'execshell': 'python3',
                         'selfpath': '/usr/local/bin',
                         'selfexec': 'simplefsapp'}

        self.plg_meta_data = {'name': self.plugin_fs_name,
                              'title': 'Dir plugin',
                              'license': 'MIT',
                              'type': 'fs',
                              'icon': 'http://github.com/plugin',
                              'category': 'Dir',
                              'stars': 0,
                              'authors': 'FNNDSC (dev@babyMRI.org)'}

        token = pfcon.Client.get_auth_token(COMPUTE_RESOURCE_URL + 'auth-token/', 'pfcon',
                                            'pfcon1234')
        pfcon_client = pfcon.Client(COMPUTE_RESOURCE_URL, token)
        pfcon_client.get_server_info()

        (self.compute_resource, tf) = ComputeResource.objects.update_or_create(
            name='host',
            defaults={'compute_url': COMPUTE_RESOURCE_URL, 'compute_user': 'pfcon',
                      'compute_password': 'pfcon1234',
                      'compute_innetwork': pfcon_client.pfcon_innetwork,
                      'compute_requires_copy_job': False,
                      'compute_requires_upload_job': True})

        # create a plugin
        data = self.plg_meta_data.copy()
        (pl_meta, tf) = PluginMeta.objects.get_or_create(**data)
        data = self.plg_data.copy()
        (plugin_fs, tf) = Plugin.objects.get_or_create(meta=pl_meta, **data)
        plugin_fs.compute_resources.set([self.compute_resource])
        plugin_fs.save()

        # add plugin's path parameter
        PluginParameter.objects.get_or_create(
            plugin=plugin_fs,
            name='dir',
            type='path',
            flag='--dir',
            optional=False,
        )

        # create user
        User.objects.create_user(username=self.username, password=self.password)

    def tearDown(self):
        # re-enable logging
        logging.disable(logging.NOTSET)
        logging.getLogger('plugininstances.services.uploadjobs').setLevel(logging.NOTSET)

    def test_run_success(self):
        with mock.patch.object(uploadjobs, 'json_zip2str',
                               return_value='raw') as json_zip2str_mock:
            user = User.objects.get(username=self.username)
            plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
            (pl_inst, tf) = PluginInstance.objects.get_or_create(
                plugin=plugin, owner=user, status='uploading',
                compute_resource=plugin.compute_resources.all()[0])
            upload_job = uploadjobs.PluginInstanceUploadJob(pl_inst)
            upload_job.get_job_status_summary = mock.Mock(return_value='summary')
            upload_job.pfcon_client.submit_job = mock.Mock(return_value='dictionary')

            upload_job.run()

            self.assertEqual(pl_inst.summary, 'summary')
            self.assertEqual(pl_inst.raw, 'raw')
            self.assertEqual(pl_inst.status, 'uploading')
            upload_job.pfcon_client.submit_job.assert_called_once()
            upload_job.get_job_status_summary.assert_called_once()
            json_zip2str_mock.assert_called_once()

    def test_run_skips_when_cancelled(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='cancelled',
            compute_resource=plugin.compute_resources.all()[0])
        upload_job = uploadjobs.PluginInstanceUploadJob(pl_inst)
        upload_job.pfcon_client.submit_job = mock.Mock()

        upload_job.run()

        upload_job.pfcon_client.submit_job.assert_not_called()

    def test_run_pfcon_error_retries_then_cancels(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='uploading',
            compute_resource=plugin.compute_resources.all()[0])
        pl_inst.upload_retry_count = PluginInstance.MAX_UPLOAD_RETRIES
        pl_inst.save(update_fields=['upload_retry_count'])
        upload_job = uploadjobs.PluginInstanceUploadJob(pl_inst)
        upload_job._submit = mock.Mock(side_effect=PfconRequestException('test error'))
        upload_job.job_exists = mock.Mock(return_value=None)  # nor did it create the job
        upload_job.enqueue_run = mock.Mock()

        with self.assertLogs('plugininstances.services', 'ERROR') as logs:
            upload_job.run()

        self.assertIn('[CODE01,', logs.output[0])
        self.assertEqual(pl_inst.status, 'cancelled')
        self.assertEqual(pl_inst.error_code, 'CODE01')
        upload_job.enqueue_run.assert_called_once_with('PluginInstanceDeleteJob')

    def test_check_exec_status_finished_successfully(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='uploading',
            compute_resource=plugin.compute_resources.all()[0])
        upload_job = uploadjobs.PluginInstanceUploadJob(pl_inst)
        upload_job._get_status = mock.Mock(
            return_value={'compute': {'status': 'finishedSuccessfully', 'logs': ''}})
        upload_job.handle_finished_successfully_status = mock.Mock()

        upload_job.check_exec_status()

        upload_job.handle_finished_successfully_status.assert_called_once()

    def test_check_exec_status_finished_with_error(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='uploading',
            compute_resource=plugin.compute_resources.all()[0])
        upload_job = uploadjobs.PluginInstanceUploadJob(pl_inst)
        upload_job._get_status = mock.Mock(
            return_value={'compute': {'status': 'finishedWithError', 'logs': ''}})
        upload_job.handle_finished_with_error_status = mock.Mock()

        upload_job.check_exec_status()

        upload_job.handle_finished_with_error_status.assert_called_once()

    def test_check_exec_status_undefined(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='uploading',
            compute_resource=plugin.compute_resources.all()[0])
        upload_job = uploadjobs.PluginInstanceUploadJob(pl_inst)
        upload_job._get_status = mock.Mock(
            return_value={'compute': {'status': 'undefined', 'logs': ''}})
        upload_job.handle_undefined_status = mock.Mock()

        upload_job.check_exec_status()

        upload_job.handle_undefined_status.assert_called_once()

    def test_check_exec_status_skips_when_not_uploading(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='started',
            compute_resource=plugin.compute_resources.all()[0])
        upload_job = uploadjobs.PluginInstanceUploadJob(pl_inst)
        upload_job._get_status = mock.Mock()

        result = upload_job.check_exec_status()

        upload_job._get_status.assert_not_called()
        self.assertEqual(result, 'started')

    def test_check_exec_status_returns_on_pfcon_error(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='uploading',
            compute_resource=plugin.compute_resources.all()[0])
        upload_job = uploadjobs.PluginInstanceUploadJob(pl_inst)
        upload_job._get_status = mock.Mock(side_effect=PfconRequestException('test error'))

        result = upload_job.check_exec_status()

        self.assertEqual(result, 'uploading')

    def test_cancel_exec(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='uploading',
            compute_resource=plugin.compute_resources.all()[0])
        upload_job = uploadjobs.PluginInstanceUploadJob(pl_inst)
        upload_job.enqueue_run = mock.Mock()

        upload_job.cancel_exec()

        self.assertEqual(pl_inst.status, 'cancelled')
        upload_job.enqueue_run.assert_called_once_with('PluginInstanceDeleteJob')

    def test_delete_success(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='uploading',
            compute_resource=plugin.compute_resources.all()[0])
        upload_job = uploadjobs.PluginInstanceUploadJob(pl_inst)
        upload_job._delete = mock.Mock()

        upload_job.delete()

        upload_job._delete.assert_called_once_with(JobType.UPLOAD, upload_job.str_job_id)
        self.assertNotEqual(pl_inst.error_code, 'CODE12')

    def test_delete_error(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='uploading',
            compute_resource=plugin.compute_resources.all()[0])
        upload_job = uploadjobs.PluginInstanceUploadJob(pl_inst)
        upload_job._delete = mock.Mock(side_effect=PfconRequestException('test error'))

        upload_job.delete()

        self.assertEqual(pl_inst.error_code, 'CODE12')

    def test_handle_finished_successfully_status_registers_the_files(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='uploading',
            compute_resource=plugin.compute_resources.all()[0])
        upload_job = uploadjobs.PluginInstanceUploadJob(pl_inst)
        upload_job.enqueue_registration = mock.Mock()

        upload_job.handle_finished_successfully_status()

        self.assertEqual(pl_inst.status, 'registeringFiles')
        # by a task of its own, which asks the remote how the plugin job ended
        upload_job.enqueue_registration.assert_called_once_with()

    def test_the_stuck_registration_sweep_counts_from_the_start_of_the_registration(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='uploading',
            compute_resource=plugin.compute_resources.all()[0])
        # the upload started, and end_date was stamped, five hours ago
        PluginInstance.objects.filter(pk=pl_inst.pk).update(
            end_date=timezone.now() - timedelta(hours=5))
        pl_inst.refresh_from_db()
        upload_job = uploadjobs.PluginInstanceUploadJob(pl_inst)
        upload_job.enqueue_registration = mock.Mock()

        upload_job.handle_finished_successfully_status()
        with mock.patch.object(tasks.cancel_plugin_instance_job, 'delay') as cancel:
            tasks.cancel_plugin_instances_stuck_in_lock()

        cancel.assert_not_called()

    def test_handle_finished_successfully_status_moved_on_by_another_task(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='uploading',
            compute_resource=plugin.compute_resources.all()[0])
        upload_job = uploadjobs.PluginInstanceUploadJob(pl_inst)
        upload_job.enqueue_registration = mock.Mock()
        # an overlapping status check got there first
        PluginInstance.objects.filter(pk=pl_inst.pk).transition('registeringFiles',
                                                                'uploading')

        upload_job.handle_finished_successfully_status()

        upload_job.enqueue_registration.assert_not_called()  # nothing registered twice

    def test_handle_finished_with_error_status(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='uploading',
            compute_resource=plugin.compute_resources.all()[0])
        upload_job = uploadjobs.PluginInstanceUploadJob(pl_inst)
        upload_job.enqueue_run = mock.Mock()

        upload_job.handle_finished_with_error_status()

        self.assertEqual(pl_inst.status, 'cancelled')
        self.assertEqual(pl_inst.error_code, 'CODE18')
        upload_job.enqueue_run.assert_called_once_with('PluginInstanceDeleteJob')

    def test_handle_undefined_status_retries(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='uploading',
            compute_resource=plugin.compute_resources.all()[0])
        pl_inst.upload_retry_count = 0
        pl_inst.save(update_fields=['upload_retry_count'])
        upload_job = uploadjobs.PluginInstanceUploadJob(pl_inst)
        upload_job.enqueue_run = mock.Mock()

        upload_job.handle_undefined_status()

        self.assertEqual(pl_inst.upload_retry_count, 1)
        upload_job.enqueue_run.assert_called_once_with('PluginInstanceUploadJob')

    def test_handle_undefined_status_gives_up(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='uploading',
            compute_resource=plugin.compute_resources.all()[0])
        pl_inst.upload_retry_count = PluginInstance.MAX_UPLOAD_RETRIES + 1
        pl_inst.save(update_fields=['upload_retry_count'])
        upload_job = uploadjobs.PluginInstanceUploadJob(pl_inst)
        upload_job.enqueue_run = mock.Mock()

        upload_job.handle_undefined_status()

        self.assertEqual(pl_inst.status, 'cancelled')
        self.assertEqual(pl_inst.error_code, 'CODE18')
        upload_job.enqueue_run.assert_called_once_with('PluginInstanceDeleteJob')

    @tag('integration')
    def test_integration_upload_job_can_run_and_check_exec_status(self):
        """
        Test whether the upload job can run and check execution status until the plugin
        instance reaches 'finishedSuccessfully'. Runs the plugin app job first so that
        pfcon has output data to upload.
        """
        user_space_path = f'home/{self.username}/uploads/'
        with io.StringIO('Test file') as f:
            self.storage_manager.upload_obj(user_space_path + 'test.txt', f.read(),
                                            content_type='text/plain')

        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        pl_inst = PluginInstance.objects.create(
            plugin=plugin, owner=user, status='scheduled',
            compute_resource=plugin.compute_resources.all()[0])
        pl_param = plugin.parameters.all()[0]
        PathParameter.objects.create(plugin_inst=pl_inst, plugin_param=pl_param,
                                     value=user_space_path)

        # run the plugin app job; when it finishes at pfcon its status check sets
        # status='uploading' (because compute_requires_upload_job=True) and enqueues the
        # upload job
        # no worker consumes the tests' queue here: the test submits each job itself, as
        # the task that a status check enqueues would
        enqueue = mock.patch.object(PluginInstanceJob, 'enqueue_run')
        enqueue.start()
        self.addCleanup(enqueue.stop)
        register = mock.patch.object(PluginInstanceJob, 'enqueue_registration',
                                     lambda job: PluginInstanceAppJob(
                                         job.c_plugin_inst).register_output())
        register.start()
        self.addCleanup(register.stop)
        app_job = PluginInstanceAppJob(pl_inst)
        app_job.run()

        # poll app job until pfcon reports finishedSuccessfully (which transitions
        # status to 'uploading')
        for _ in range(20):
            time.sleep(5)
            str_status = app_job.check_exec_status()
            if str_status == 'uploading':
                break
        if pl_inst.status != 'uploading':
            self.storage_manager.delete_obj(user_space_path + 'test.txt')
            self.skipTest('Plugin app job did not transition to uploading status')

        # poll the upload job until the plugin instance reaches 'finishedSuccessfully'
        upload_job = uploadjobs.PluginInstanceUploadJob(pl_inst)
        upload_job.run()
        for _ in range(20):
            time.sleep(5)
            upload_job.check_exec_status()
            if pl_inst.status == 'finishedSuccessfully':
                break
        self.assertEqual(pl_inst.status, 'finishedSuccessfully')

        # delete files from storage
        self.storage_manager.delete_path(pl_inst.output_folder.path)
        self.storage_manager.delete_obj(user_space_path + 'test.txt')

    @tag('integration')
    def test_integration_upload_job_delete(self):
        """
        Test whether the upload job can be deleted from the remote compute after
        submission. Runs the plugin app job first so that pfcon has output data to upload.
        """
        user_space_path = f'home/{self.username}/uploads/'
        with io.StringIO('Test file') as f:
            self.storage_manager.upload_obj(user_space_path + 'test.txt', f.read(),
                                            content_type='text/plain')

        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        pl_inst = PluginInstance.objects.create(
            plugin=plugin, owner=user, status='scheduled',
            compute_resource=plugin.compute_resources.all()[0])
        pl_param = plugin.parameters.all()[0]
        PathParameter.objects.create(plugin_inst=pl_inst, plugin_param=pl_param,
                                     value=user_space_path)

        # run the plugin app job; its status check moves it to 'uploading'
        # (compute_requires_upload_job=True)
        # no worker consumes the tests' queue here: the test submits each job itself, as
        # the task that a status check enqueues would
        enqueue = mock.patch.object(PluginInstanceJob, 'enqueue_run')
        enqueue.start()
        self.addCleanup(enqueue.stop)
        register = mock.patch.object(PluginInstanceJob, 'enqueue_registration',
                                     lambda job: PluginInstanceAppJob(
                                         job.c_plugin_inst).register_output())
        register.start()
        self.addCleanup(register.stop)
        app_job = PluginInstanceAppJob(pl_inst)
        app_job.run()

        for _ in range(20):
            time.sleep(5)
            str_status = app_job.check_exec_status()
            if str_status == 'uploading':
                break
        if pl_inst.status != 'uploading':
            self.storage_manager.delete_obj(user_space_path + 'test.txt')
            self.skipTest('Plugin app job did not transition to uploading status')

        # poll the upload job until it finishes (transitions away from 'uploading')
        upload_job = uploadjobs.PluginInstanceUploadJob(pl_inst)
        upload_job.run()
        for _ in range(20):
            time.sleep(5)
            upload_job.check_exec_status()
            if pl_inst.status != 'uploading':
                break

        # delete the upload job container from pfcon
        upload_job2 = uploadjobs.PluginInstanceUploadJob(pl_inst)
        upload_job2.delete()

        self.assertNotEqual(pl_inst.error_code, 'CODE12')

        # delete files from storage
        self.storage_manager.delete_path(pl_inst.output_folder.path)
        self.storage_manager.delete_obj(user_space_path + 'test.txt')
