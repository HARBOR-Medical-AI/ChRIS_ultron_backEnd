
import logging
import io
import time
import uuid
from unittest import mock

from django.test import TestCase, tag
from django.contrib.auth.models import User
from django.conf import settings

from pfconclient import client as pfcon
from pfconclient.client import JobType
from pfconclient.exceptions import PfconRequestException

from core.models import ChrisInstance
from core.storage import connect_storage
from plugins.models import PluginMeta, Plugin
from plugins.models import PluginParameter
from plugininstances.models import PluginInstance, PathParameter, ComputeResource
from plugininstances.services import copyjobs, pluginjobs


COMPUTE_RESOURCE_URL = settings.COMPUTE_RESOURCE_URL
CHRIS_SUPERUSER_PASSWORD = settings.CHRIS_SUPERUSER_PASSWORD


class PluginInstanceCopyJobTests(TestCase):

    def setUp(self):
        # avoid cluttered console output (for instance logging all the http requests)
        logging.disable(logging.WARNING)
        logging.getLogger('plugininstances.services.copyjobs').setLevel(logging.CRITICAL)

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

        plugin_parameters = [{'name': 'dir', 'type': 'path', 'action': 'store',
                              'optional': False, 'flag': '--dir', 'short_flag': '-d',
                              'help': 'test plugin', 'ui_exposed': True}]

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

        self.plugin_repr = self.plg_data.copy()
        self.plugin_repr.update(self.plg_meta_data)
        self.plugin_repr['parameters'] = plugin_parameters

        token = pfcon.Client.get_auth_token(COMPUTE_RESOURCE_URL + 'auth-token/', 'pfcon',
                                            'pfcon1234')
        pfcon_client = pfcon.Client(COMPUTE_RESOURCE_URL, token)
        pfcon_client.get_server_info()

        (self.compute_resource, tf) = ComputeResource.objects.update_or_create(
            name='host',
            defaults={'compute_url': COMPUTE_RESOURCE_URL, 'compute_user': 'pfcon',
                      'compute_password': 'pfcon1234',
                      'compute_innetwork': pfcon_client.pfcon_innetwork,
                      'compute_requires_copy_job': True})

        # create a plugin
        data = self.plg_meta_data.copy()
        (pl_meta, tf) = PluginMeta.objects.get_or_create(**data)
        data = self.plg_data.copy()
        (plugin_fs, tf) = Plugin.objects.get_or_create(meta=pl_meta, **data)
        plugin_fs.compute_resources.set([self.compute_resource])
        plugin_fs.save()

        # add plugin's parameters
        parameters = plugin_parameters
        PluginParameter.objects.get_or_create(
            plugin=plugin_fs,
            name=parameters[0]['name'],
            type=parameters[0]['type'],
            flag=parameters[0]['flag'],
            optional=parameters[0]['optional'],
        )

        # create user
        User.objects.create_user(username=self.username, password=self.password)

    def tearDown(self):
        # re-enable logging
        logging.disable(logging.NOTSET)
        logging.getLogger('plugininstances.services.copyjobs').setLevel(logging.NOTSET)

    def test_get_plugin_instance_path_parameters(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='copying',
            compute_resource=plugin.compute_resources.all()[0])
        pl_param = plugin.parameters.all()[0]
        user_space_path = f'home/{self.username}/uploads'
        PathParameter.objects.get_or_create(plugin_inst=pl_inst, plugin_param=pl_param,
                                            value=user_space_path)
        copy_job = copyjobs.PluginInstanceCopyJob(pl_inst)

        unextpath_params, path_params = copy_job.get_plugin_instance_path_parameters()

        self.assertEqual(unextpath_params, {})
        self.assertEqual(path_params, {'--dir': user_space_path})

    def test_run_success(self):
        with mock.patch.object(copyjobs, 'json_zip2str',
                               return_value='raw') as json_zip2str_mock:
            user = User.objects.get(username=self.username)
            plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
            (pl_inst, tf) = PluginInstance.objects.get_or_create(
                plugin=plugin, owner=user, status='copying',
                compute_resource=plugin.compute_resources.all()[0])
            pl_param = plugin.parameters.all()[0]
            PathParameter.objects.get_or_create(plugin_inst=pl_inst, plugin_param=pl_param,
                                                value=self.username)
            copy_job = copyjobs.PluginInstanceCopyJob(pl_inst)
            copy_job.get_job_status_summary = mock.Mock(return_value='summary')
            copy_job.pfcon_client.submit_job = mock.Mock(return_value='dictionary')

            copy_job.run()

            self.assertEqual(pl_inst.summary, 'summary')
            self.assertEqual(pl_inst.raw, 'raw')
            self.assertEqual(pl_inst.status, 'copying')
            copy_job.pfcon_client.submit_job.assert_called_once()
            copy_job.get_job_status_summary.assert_called_once()
            json_zip2str_mock.assert_called_once()

    def test_run_submit_error_for_a_job_the_remote_has_is_a_submission(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='copying',
            compute_resource=plugin.compute_resources.all()[0])
        pl_param = plugin.parameters.all()[0]
        PathParameter.objects.get_or_create(plugin_inst=pl_inst, plugin_param=pl_param,
                                            value=self.username)
        copy_job = copyjobs.PluginInstanceCopyJob(pl_inst)
        # another task submitted it at once: the remote refused the second container
        copy_job.pfcon_client.submit_job = mock.Mock(
            side_effect=PfconRequestException('Internal Server Error', code=500))
        copy_job._get_status = mock.Mock(return_value={
            'compute': {'status': 'started', 'logs': ''}})
        copy_job.enqueue_run = mock.Mock()

        logging.disable(logging.NOTSET)  # setUp disables WARNING and below
        self.addCleanup(logging.disable, logging.WARNING)
        with self.assertLogs('plugininstances.services', 'WARNING') as logs:
            copy_job.run()

        self.assertEqual([r.levelname for r in logs.records], ['WARNING'])  # no CODE01

        db_inst = PluginInstance.objects.get(pk=pl_inst.pk)
        self.assertEqual((db_inst.status, db_inst.copy_retry_count, db_inst.error_code),
                         ('copying', 0, ''))

    def test_run_submit_error_for_a_job_the_remote_lacks_cancels_it_past_the_retries(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='copying',
            compute_resource=plugin.compute_resources.all()[0])
        PluginInstance.objects.filter(pk=pl_inst.pk).update(
            copy_retry_count=PluginInstance.MAX_COPY_RETRIES)
        pl_inst.refresh_from_db()
        pl_param = plugin.parameters.all()[0]
        PathParameter.objects.get_or_create(plugin_inst=pl_inst, plugin_param=pl_param,
                                            value=self.username)
        copy_job = copyjobs.PluginInstanceCopyJob(pl_inst)
        copy_job.pfcon_client.requires_copy_job = True  # no legacy retry with a new token
        copy_job.pfcon_client.submit_job = mock.Mock(
            side_effect=PfconRequestException('Bad Request', code=400))
        copy_job._get_status = mock.Mock(
            side_effect=PfconRequestException('Not Found', code=404))
        copy_job.enqueue_run = mock.Mock()

        with self.assertLogs('plugininstances.services', 'ERROR') as logs:
            copy_job.run()

        self.assertIn(f'[CODE01,{copy_job.str_job_id}]: Error submitting copy job',
                      logs.output[-1])
        db_inst = PluginInstance.objects.get(pk=pl_inst.pk)
        self.assertEqual((db_inst.status, db_inst.error_code, db_inst.remote_cleanup_status),
                         ('cancelled', 'CODE01', 'deletingData'))
        copy_job.enqueue_run.assert_called_once_with('PluginInstanceDeleteJob')
        copy_job.pfcon_client.submit_job.assert_called_once()

    def test_run_skips_when_cancelled(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='cancelled',
            compute_resource=plugin.compute_resources.all()[0])
        copy_job = copyjobs.PluginInstanceCopyJob(pl_inst)
        copy_job.pfcon_client.submit_job = mock.Mock()

        copy_job.run()

        copy_job.pfcon_client.submit_job.assert_not_called()

    def test_check_exec_status_finished_successfully(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='copying',
            compute_resource=plugin.compute_resources.all()[0])
        copy_job = copyjobs.PluginInstanceCopyJob(pl_inst)
        copy_job._get_status = mock.Mock(
            return_value={'compute': {'status': 'finishedSuccessfully', 'logs': ''}})
        copy_job.handle_finished_successfully_status = mock.Mock()

        copy_job.check_exec_status()

        copy_job.handle_finished_successfully_status.assert_called_once()

    def test_check_exec_status_finished_with_error(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='copying',
            compute_resource=plugin.compute_resources.all()[0])
        copy_job = copyjobs.PluginInstanceCopyJob(pl_inst)
        copy_job._get_status = mock.Mock(
            return_value={'compute': {'status': 'finishedWithError', 'logs': ''}})
        copy_job.handle_finished_with_error_status = mock.Mock()

        copy_job.check_exec_status()

        copy_job.handle_finished_with_error_status.assert_called_once()

    def test_check_exec_status_undefined(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='copying',
            compute_resource=plugin.compute_resources.all()[0])
        copy_job = copyjobs.PluginInstanceCopyJob(pl_inst)
        copy_job._get_status = mock.Mock(
            return_value={'compute': {'status': 'undefined', 'logs': ''}})
        copy_job.handle_undefined_status = mock.Mock()

        copy_job.check_exec_status()

        copy_job.handle_undefined_status.assert_called_once()

    def test_check_exec_status_skips_when_not_copying(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='scheduled',
            compute_resource=plugin.compute_resources.all()[0])
        copy_job = copyjobs.PluginInstanceCopyJob(pl_inst)
        copy_job._get_status = mock.Mock()

        result = copy_job.check_exec_status()

        copy_job._get_status.assert_not_called()
        self.assertEqual(result, 'scheduled')

    def test_check_exec_status_returns_on_pfcon_error(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='copying',
            compute_resource=plugin.compute_resources.all()[0])
        copy_job = copyjobs.PluginInstanceCopyJob(pl_inst)
        copy_job._get_status = mock.Mock(side_effect=PfconRequestException('test error'))

        result = copy_job.check_exec_status()

        self.assertEqual(result, 'copying')

    def test_cancel_exec(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='copying',
            compute_resource=plugin.compute_resources.all()[0])
        copy_job = copyjobs.PluginInstanceCopyJob(pl_inst)
        copy_job.enqueue_run = mock.Mock()

        copy_job.cancel_exec('CODE13')

        pl_inst.refresh_from_db()
        self.assertEqual(pl_inst.status, 'cancelled')
        self.assertEqual(pl_inst.error_code, 'CODE13')
        # the remote cleanup is armed with the cancellation, and its first job submitted
        self.assertEqual(pl_inst.remote_cleanup_status, 'deletingData')
        copy_job.enqueue_run.assert_called_once_with('PluginInstanceDeleteJob')

    def test_delete_success(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='copying',
            compute_resource=plugin.compute_resources.all()[0])
        copy_job = copyjobs.PluginInstanceCopyJob(pl_inst)
        copy_job._delete = mock.Mock()

        copy_job.delete()

        copy_job._delete.assert_called_once_with(JobType.COPY, copy_job.str_job_id)
        self.assertNotEqual(pl_inst.error_code, 'CODE12')

    def test_delete_error(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='copying',
            compute_resource=plugin.compute_resources.all()[0])
        copy_job = copyjobs.PluginInstanceCopyJob(pl_inst)
        copy_job._delete = mock.Mock(side_effect=PfconRequestException('test error'))

        copy_job.delete()

        self.assertEqual(pl_inst.error_code, 'CODE12')

    def test_handle_finished_successfully_status(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='copying',
            compute_resource=plugin.compute_resources.all()[0])
        copy_job = copyjobs.PluginInstanceCopyJob(pl_inst)
        copy_job.enqueue_run = mock.Mock()

        copy_job.handle_finished_successfully_status()

        pl_inst.refresh_from_db()
        self.assertEqual(pl_inst.status, 'scheduled')
        self.assertTrue(pl_inst.summary['pushPath']['status'])
        # a status check never submits the app job itself
        copy_job.enqueue_run.assert_called_once_with('PluginInstanceAppJob',
                                                      moved_to='scheduled')

    def test_handle_finished_with_error_status(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='copying',
            compute_resource=plugin.compute_resources.all()[0])
        copy_job = copyjobs.PluginInstanceCopyJob(pl_inst)
        copy_job.enqueue_run = mock.Mock()

        copy_job.handle_finished_with_error_status()

        pl_inst.refresh_from_db()
        self.assertEqual(pl_inst.status, 'cancelled')
        self.assertEqual(pl_inst.error_code, 'CODE18')
        copy_job.enqueue_run.assert_called_once_with('PluginInstanceDeleteJob')

    def test_handle_undefined_status_retries(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='copying',
            compute_resource=plugin.compute_resources.all()[0])
        pl_inst.copy_retry_count = 0
        pl_inst.save(update_fields=['copy_retry_count'])
        copy_job = copyjobs.PluginInstanceCopyJob(pl_inst)
        copy_job.enqueue_run = mock.Mock()

        copy_job.handle_undefined_status()

        self.assertEqual(pl_inst.copy_retry_count, 1)
        # submitted again by a task of its own
        copy_job.enqueue_run.assert_called_once_with('PluginInstanceCopyJob')

    def test_handle_undefined_status_gives_up(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='copying',
            compute_resource=plugin.compute_resources.all()[0])
        pl_inst.copy_retry_count = PluginInstance.MAX_COPY_RETRIES + 1
        pl_inst.save(update_fields=['copy_retry_count'])
        copy_job = copyjobs.PluginInstanceCopyJob(pl_inst)
        copy_job.enqueue_run = mock.Mock()

        copy_job.handle_undefined_status()

        pl_inst.refresh_from_db()
        self.assertEqual(pl_inst.status, 'cancelled')
        self.assertEqual(pl_inst.error_code, 'CODE18')
        copy_job.enqueue_run.assert_called_once_with('PluginInstanceDeleteJob')

    @tag('integration')
    def test_integration_copy_job_can_run_and_check_exec_status(self):
        """
        Test whether the copy job can run and check execution status until the copy
        finishes and the app job is auto-started.
        """
        # upload a file to the user's storage space
        user_space_path = f'home/{self.username}/uploads/'
        with io.StringIO('Test file') as f:
            self.storage_manager.upload_obj(user_space_path + 'test.txt', f.read(),
                                            content_type='text/plain')

        # create a plugin instance in 'copying' status
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        pl_inst = PluginInstance.objects.create(
            plugin=plugin, owner=user, status='copying',
            compute_resource=plugin.compute_resources.all()[0])
        pl_param = plugin.parameters.all()[0]
        PathParameter.objects.get_or_create(plugin_inst=pl_inst, plugin_param=pl_param,
                                            value=user_space_path)

        # run the copy job (pfcon downloads input files from swift storage)
        copy_job = copyjobs.PluginInstanceCopyJob(pl_inst)
        copy_job.enqueue_run = mock.Mock()  # no worker consumes the tests' queue here
        copy_job.run()
        self.assertEqual(pl_inst.status, 'copying')

        # poll copy job status until the copy finishes and the app job is scheduled
        maxLoopTries = 10
        currentLoop = 1
        time.sleep(5)
        while currentLoop <= maxLoopTries:
            copy_job.check_exec_status()
            if pl_inst.status != 'copying':
                break
            time.sleep(5)
            currentLoop += 1
        if pl_inst.status == 'cancelled':
            self.storage_manager.delete_obj(user_space_path + 'test.txt')
            self.skipTest('Copy job failed to transition to scheduled - likely pfcon Docker issue')
        self.assertEqual(pl_inst.status, 'scheduled')
        copy_job.enqueue_run.assert_called_once_with('PluginInstanceAppJob',
                                                      moved_to='scheduled')

        # the task the status check enqueued submits the app job
        pluginjobs.PluginInstanceAppJob(pl_inst).run()
        self.assertEqual(pl_inst.status, 'started')

        # delete files from storage
        self.storage_manager.delete_path(pl_inst.output_folder.path)
        self.storage_manager.delete_obj(user_space_path + 'test.txt')

    @tag('integration')
    def test_integration_copy_job_cancel_exec(self):
        """
        Test whether the copy job can be cancelled after submission.
        """
        # upload a file to the user's storage space
        user_space_path = f'home/{self.username}/uploads/'
        with io.StringIO('Test file') as f:
            self.storage_manager.upload_obj(user_space_path + 'test.txt', f.read(),
                                            content_type='text/plain')

        # create a plugin instance in 'copying' status
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        pl_inst = PluginInstance.objects.create(
            plugin=plugin, owner=user, status='copying',
            compute_resource=plugin.compute_resources.all()[0])
        pl_param = plugin.parameters.all()[0]
        PathParameter.objects.get_or_create(plugin_inst=pl_inst, plugin_param=pl_param,
                                            value=user_space_path)

        # run the copy job
        copy_job = copyjobs.PluginInstanceCopyJob(pl_inst)
        copy_job.run()

        # cancel the copy job (mock enqueue_run to prevent Celery tasks from being
        # dispatched in a TestCase without a running worker)
        copy_job.enqueue_run = mock.Mock()
        copy_job.cancel_exec()

        self.assertEqual(pl_inst.status, 'cancelled')
        copy_job.enqueue_run.assert_called_once_with('PluginInstanceDeleteJob')

        # delete files from storage
        self.storage_manager.delete_obj(user_space_path + 'test.txt')

    @tag('integration')
    def test_integration_copy_job_delete(self):
        """
        Test whether the copy job can be deleted from the remote compute after it
        finishes.
        """
        # upload a file to the user's storage space
        user_space_path = f'home/{self.username}/uploads/'
        with io.StringIO('Test file') as f:
            self.storage_manager.upload_obj(user_space_path + 'test.txt', f.read(),
                                            content_type='text/plain')

        # create a plugin instance in 'copying' status
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        pl_inst = PluginInstance.objects.create(
            plugin=plugin, owner=user, status='copying',
            compute_resource=plugin.compute_resources.all()[0])
        pl_param = plugin.parameters.all()[0]
        PathParameter.objects.get_or_create(plugin_inst=pl_inst, plugin_param=pl_param,
                                            value=user_space_path)

        # run the copy job and poll until the copy finishes (status transitions away
        # from 'copying')
        copy_job = copyjobs.PluginInstanceCopyJob(pl_inst)
        copy_job.enqueue_run = mock.Mock()  # no worker consumes the tests' queue here
        copy_job.run()
        maxLoopTries = 10
        currentLoop = 1
        time.sleep(5)
        while currentLoop <= maxLoopTries:
            copy_job.check_exec_status()
            if pl_inst.status != 'copying':
                break
            time.sleep(5)
            currentLoop += 1

        # delete the copy job container from pfcon
        copy_job2 = copyjobs.PluginInstanceCopyJob(pl_inst)
        copy_job2.delete()

        self.assertNotEqual(pl_inst.error_code, 'CODE12')

        # delete files from storage
        self.storage_manager.delete_path(pl_inst.output_folder.path)
        self.storage_manager.delete_obj(user_space_path + 'test.txt')
