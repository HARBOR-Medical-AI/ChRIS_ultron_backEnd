
import logging
import os
import io
import time
import uuid
from unittest import mock

from celery.exceptions import SoftTimeLimitExceeded
from django.db import connection
from django.test import TestCase, tag
from django.test.utils import CaptureQueriesContext
from django.contrib.auth.models import User
from django.conf import settings

from pfconclient import client as pfcon
from pfconclient.exceptions import PfconRequestException

from core.models import ChrisInstance, ChrisFolder, ChrisLinkFile
from userfiles.models import UserFile
from core.storage import connect_storage
from plugins.models import PluginMeta, Plugin
from plugins.models import PluginParameter
from plugininstances.models import (BoolParameter, ComputeResource, PathParameter,
                                    PluginInstance, StrParameter)
from plugininstances.services import pluginjobs, copyjobs
from plugininstances.services.abstractjobs import PluginInstanceJob
from plugininstances.services.uploadjobs import PluginInstanceUploadJob
from plugininstances.tests.test_transitions import PluginInstanceFixture


COMPUTE_RESOURCE_URL = settings.COMPUTE_RESOURCE_URL
CHRIS_SUPERUSER_PASSWORD = settings.CHRIS_SUPERUSER_PASSWORD


class PluginInstanceAppJobTests(TestCase):
    
    def setUp(self):
        # avoid cluttered console output (for instance logging all the http requests)
        logging.disable(logging.WARNING)

        # use a unique job_id_prefix to avoid pfcon job ID collisions with
        # stale jobs from prior test runs
        chris_inst = ChrisInstance.load()
        chris_inst.job_id_prefix = f'test-{uuid.uuid4().hex[:8]}-'
        chris_inst.save(update_fields=['job_id_prefix'])

        # create superuser chris (owner of root folders)
        self.chris_username = 'chris'
        self.chris_password = CHRIS_SUPERUSER_PASSWORD

        self.storage_manager = connect_storage(settings)

        self.plugin_fs_name = "simplefsapp"
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
                      'compute_requires_copy_job': False,
                      'compute_requires_upload_job': pfcon_client.pfcon_innetwork and settings.STORAGE_ENV in ('swift', 's3')})

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

    def test_assemble_exec(self):
        self.assertListEqual(
            ['python', '/usr/local/src/script.py'],
            pluginjobs.PluginInstanceAppJob._assemble_exec('/usr/local/src', 'script.py', 'python')
        )
        self.assertListEqual(
            ['python', 'script.py'],
            pluginjobs.PluginInstanceAppJob._assemble_exec(None, 'script.py', 'python')
        )
        self.assertListEqual(
            ['rust_is_better_than_python'],
            pluginjobs.PluginInstanceAppJob._assemble_exec(None, 'rust_is_better_than_python', None)
        )

    def _files_check_job(self, *listings):
        """
        An app job whose output in a mocked storage lists ``listings`` one after another
        (the last one from then on), and a job JSON listing 'a.txt' and 'sub/b.txt' in it.
        """
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        pl_inst = PluginInstance.objects.create(
            plugin=plugin, owner=user, compute_resource=plugin.compute_resources.all()[0])
        job = pluginjobs.PluginInstanceAppJob(pl_inst)
        output = pl_inst.get_output_path()
        listings = [[f'{output}/{name}' for name in names] for names in listings]
        job.storage_manager = mock.Mock()
        job.storage_manager.ls.side_effect = lambda path: (
            listings.pop(0) if len(listings) > 1 else listings[0])
        content = {'job_output_path': output, 'rel_file_paths': ['a.txt', 'sub/b.txt']}
        return job, content

    def test_the_files_check_lists_the_output_once_when_every_file_is_there(self):
        job, content = self._files_check_job(['a.txt', 'sub/b.txt', 'c.txt'])

        with mock.patch.object(pluginjobs.time, 'sleep') as sleep:
            job.check_files_from_json_exist(content)

        job.storage_manager.ls.assert_called_once_with(content['job_output_path'])
        sleep.assert_not_called()
        self.assertEqual(job.plugin_inst_output_files,
                         {f"{content['job_output_path']}/a.txt",
                          f"{content['job_output_path']}/sub/b.txt"})

    def test_the_files_check_lists_again_a_second_later_until_every_file_is_there(self):
        job, content = self._files_check_job(['a.txt'], ['a.txt'], ['a.txt', 'sub/b.txt'])

        with mock.patch.object(pluginjobs.time, 'sleep') as sleep:
            job.check_files_from_json_exist(content)

        self.assertEqual(job.storage_manager.ls.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(1)] * 2)
        self.assertEqual(len(job.plugin_inst_output_files), 2)

    def test_the_files_check_fails_with_code14_after_60_listings_a_file_missed(self):
        job, content = self._files_check_job(['a.txt'])

        with mock.patch.object(pluginjobs.time, 'sleep') as sleep, \
                self.assertLogs('plugininstances', 'ERROR') as logs, \
                self.assertRaisesRegex(ValueError, 'Missing 1 files in storage'):
            job.check_files_from_json_exist(content)

        self.assertIn('[CODE14,', logs.output[0])
        self.assertEqual(job.storage_manager.ls.call_count, 60)
        self.assertEqual(sleep.call_count, 59)
        self.assertEqual(job.c_plugin_inst.error_code, 'CODE14')

    def test_plugin_job_can_run_registered_plugin_app(self):
        """
        Test whether the plugin job can run an already registered plugin app.
        """
        with mock.patch.object(pluginjobs, 'json_zip2str',
                               return_value='raw') as json_zip2str_mock:
            user = User.objects.get(username=self.username)
            plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
            (pl_inst, tf) = PluginInstance.objects.get_or_create(
                plugin=plugin, owner=user, status='scheduled',
                compute_resource=plugin.compute_resources.all()[0])
            pl_param = plugin.parameters.all()[0]
            PathParameter.objects.get_or_create(plugin_inst=pl_inst,
                                                plugin_param=pl_param,
                                                value=self.username)
            plg_inst_app_job = pluginjobs.PluginInstanceAppJob(pl_inst)
            plg_inst_app_job.get_job_status_summary = mock.Mock(return_value='summary')
            plg_inst_app_job.pfcon_client.submit_job = mock.Mock(
                return_value='dictionary')

            # call run method
            plg_inst_app_job.run()

            self.assertEqual(pl_inst.status, 'started')
            self.assertEqual(pl_inst.summary, 'summary')
            self.assertEqual(pl_inst.raw, 'raw')
            plg_inst_app_job.pfcon_client.submit_job.assert_called_once()
            plg_inst_app_job.get_job_status_summary.assert_called_once()
            json_zip2str_mock.assert_called_once()

    @tag('integration', 'error-pfcon')
    def test_integration_plugin_job_can_run_and_check_exec_status(self):
        """
        Test whether the plugin job can run copy and plugin app jobs and check
        execution status.
        """
        self.compute_resource.compute_requires_copy_job = True
        self.compute_resource.save(update_fields=['compute_requires_copy_job'])

        # upload a file to the storage user's space
        user_space_path = 'home/%s/uploads/' % self.username
        with io.StringIO('Test file') as f:
            self.storage_manager.upload_obj(user_space_path + 'test.txt', f.read(),
                                          content_type='text/plain')

        # create a plugin's instance in 'copying' status
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        pl_inst = PluginInstance.objects.create(
            plugin=plugin, owner=user, status='copying',
            compute_resource=plugin.compute_resources.all()[0])
        pl_param = plugin.parameters.all()[0]
        PathParameter.objects.get_or_create(plugin_inst=pl_inst, plugin_param=pl_param,
                                            value=user_space_path)

        # no worker consumes the tests' queue here: the test submits each job itself, as
        # the task that a status check enqueues would
        enqueue = mock.patch.object(PluginInstanceJob, 'enqueue_run')
        enqueue.start()
        self.addCleanup(enqueue.stop)

        # run the copy job
        plg_inst_copy_job = copyjobs.PluginInstanceCopyJob(pl_inst)
        plg_inst_copy_job.run()
        self.assertEqual(pl_inst.status, 'copying')

        # poll copy job status until it finishes and the app job is scheduled
        maxLoopTries = 10
        currentLoop = 1
        time.sleep(5)
        while currentLoop <= maxLoopTries:
            plg_inst_copy_job.check_exec_status()
            if pl_inst.status != 'copying':
                break
            time.sleep(5)
            currentLoop += 1
        if pl_inst.status == 'cancelled':
            self.storage_manager.delete_obj(user_space_path + 'test.txt')
            self.skipTest('Copy job failed to transition to scheduled - likely pfcon Docker issue')
        self.assertEqual(pl_inst.status, 'scheduled')

        # run the app job and poll it until it finishes or transitions to uploading;
        # the registration its status check enqueues runs here too
        register = mock.patch.object(PluginInstanceJob, 'enqueue_registration',
                                     lambda job: pluginjobs.PluginInstanceAppJob(
                                         job.c_plugin_inst).register_output())
        register.start()
        self.addCleanup(register.stop)
        plg_inst_app_job = pluginjobs.PluginInstanceAppJob(pl_inst)
        plg_inst_app_job.run()
        self.assertEqual(pl_inst.status, 'started')
        maxLoopTries = 10
        currentLoop = 1
        b_checkAgain = True
        time.sleep(10)
        while b_checkAgain:
            str_responseStatus = plg_inst_app_job.check_exec_status()
            if str_responseStatus in ('finishedSuccessfully', 'uploading'):
                b_checkAgain = False
            elif currentLoop < maxLoopTries:
                time.sleep(10)
            if currentLoop == maxLoopTries:
                b_checkAgain = False
            currentLoop += 1

        # if compute_requires_upload_job=True the app job transitions to 'uploading';
        # poll the upload job until finishedSuccessfully
        if pl_inst.status == 'uploading':
            upload_job = PluginInstanceUploadJob(pl_inst)
            upload_job.run()
            maxLoopTries = 10
            currentLoop = 1
            time.sleep(5)
            while currentLoop <= maxLoopTries:
                upload_job.check_exec_status()
                if pl_inst.status == 'finishedSuccessfully':
                    break
                time.sleep(10)
                currentLoop += 1

        self.assertEqual(pl_inst.status, 'finishedSuccessfully')

        # the registered output size must be persisted to the DB, not just
        # accumulated on the in-memory instance
        self.assertGreater(PluginInstance.objects.get(pk=pl_inst.id).size, 0)

        str_fileCreatedByPlugin = os.path.join(pl_inst.get_output_path(), 'out.txt')
        # make sure str_fileCreatedByPlugin file was created in storage
        self.assertTrue(self.storage_manager.obj_exists(str_fileCreatedByPlugin))

        # delete files from storage
        self.storage_manager.delete_path(pl_inst.output_folder.path)
        self.storage_manager.delete_obj(user_space_path + 'test.txt')

    @tag('integration')
    def test_integration_plugin_job_can_register_output_files(self):
        """
        Test whether the plugin job can register output files.
        """
        # create a plugin's instance
        user_space_path = 'home/%s/uploads/' % self.username
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)

        pl_inst = PluginInstance.objects.create(
            plugin=plugin, owner=user, status='finishedSuccessfully',
            compute_resource=plugin.compute_resources.all()[0])

        pl_param = plugin.parameters.all()[0]
        PathParameter.objects.get_or_create(plugin_inst=pl_inst, plugin_param=pl_param,
                                            value=user_space_path)

        outputdir = pl_inst.get_output_path()

        # upload two files to the plugin instance's output path
        with io.StringIO('Test file') as f:
            self.storage_manager.upload_obj(outputdir + '/, ,/SAG,T1,MPRAGE/tes,t1.txt',
                                            f.read(), content_type='text/plain')
            f.seek(0)
            self.storage_manager.upload_obj(outputdir + '/, ,/SAG,T1,MPRAGE/test2.txt',
                                            f.read(), content_type='text/plain')

        plg_inst_app_job = pluginjobs.PluginInstanceAppJob(pl_inst)
        plg_inst_app_job.plugin_inst_output_files = {
            outputdir + '/, ,/SAG,T1,MPRAGE/tes,t1.txt',
            outputdir + '/, ,/SAG,T1,MPRAGE/test2.txt'}

        plg_inst_app_job._register_output_files()

        self.assertEqual(pl_inst.get_output_path(), outputdir)

        folders = pl_inst.output_folder.children.all()
        self.assertEqual(len(folders), 1)

        folder = folders.first()
        fnames = [f.fname.name for f in folder.chris_files.all()]

        self.assertEqual(len(fnames), 2)
        self.assertIn(outputdir + '/SAGT1MPRAGE/test1.txt', fnames)
        self.assertIn(outputdir + '/SAGT1MPRAGE/test2.txt', fnames)

        self.assertEqual(len(plg_inst_app_job.plugin_inst_output_files), 2)
        self.assertIn(outputdir + '/SAGT1MPRAGE/test1.txt',
                      plg_inst_app_job.plugin_inst_output_files)
        self.assertIn(outputdir + '/SAGT1MPRAGE/test2.txt',
                      plg_inst_app_job.plugin_inst_output_files)

        # delete files from storage
        self.storage_manager.delete_path(outputdir)

    def _create_plugin_inst(self, status='registeringFiles'):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status=status,
            compute_resource=plugin.compute_resources.all()[0])
        return pl_inst

    def _registering_job(self, pl_inst, size, remote_status='finishedSuccessfully'):
        """
        An app job for ``pl_inst`` whose remote says the job ended with ``remote_status``,
        whose remote fetch is bypassed and whose registration finds ``size`` bytes.
        """
        job = pluginjobs.PluginInstanceAppJob(pl_inst)
        job._get_status = mock.Mock(return_value={'compute': {'status': remote_status}})
        job._get_job_zip_data = mock.Mock(return_value=b'')
        job._get_job_json_data = mock.Mock(return_value={})
        job.unpack_zip_file = mock.Mock()
        job.check_files_from_json_exist = mock.Mock()
        job.enqueue_run = mock.Mock()
        job._register_output_files = mock.Mock(
            side_effect=lambda: setattr(pl_inst, 'size', pl_inst.size + size))
        return job

    def test_register_output_on_success_persists_size(self):
        """
        Test whether the size accumulated while registering output files is saved
        to the DB. Regression test: 'size' was missing from the closing save's
        update_fields, so every instance reported size=0 through the API.
        """
        pl_inst = self._create_plugin_inst()
        job = self._registering_job(pl_inst, 1024)

        job.register_output()

        self.assertEqual(pl_inst.status, 'finishedSuccessfully')
        db_inst = PluginInstance.objects.get(pk=pl_inst.id)   # fresh DB fetch
        self.assertEqual(db_inst.status, 'finishedSuccessfully')
        self.assertEqual(db_inst.size, 1024)
        # the remote cleanup is armed with the final status
        self.assertEqual(db_inst.remote_cleanup_status, 'deletingData')
        job.enqueue_run.assert_called_once_with('PluginInstanceDeleteJob')

    def test_register_output_on_error_persists_size(self):
        """
        Same regression as above for the error path: files registered before the
        job is marked finishedWithError must still have their size persisted.
        """
        pl_inst = self._create_plugin_inst()
        job = self._registering_job(pl_inst, 512, 'finishedWithError')

        job.register_output()

        db_inst = PluginInstance.objects.get(pk=pl_inst.id)
        self.assertEqual(db_inst.status, 'finishedWithError')
        self.assertEqual(db_inst.size, 512)

    def test_register_output_of_a_job_that_did_not_end_cancels_with_code10(self):
        pl_inst = self._create_plugin_inst()
        job = self._registering_job(pl_inst, 1, 'started')

        with self.assertLogs('plugininstances.services.pluginjobs', 'ERROR') as logs:
            job.register_output()

        self.assertIn('[CODE10,', logs.output[0])
        db_inst = PluginInstance.objects.get(pk=pl_inst.id)
        self.assertEqual((db_inst.status, db_inst.error_code), ('cancelled', 'CODE10'))
        job._register_output_files.assert_not_called()

    def test_a_cancel_that_lands_during_registration_survives_its_final_write(self):
        pl_inst = self._create_plugin_inst()
        job = self._registering_job(pl_inst, 64)

        def cancelled_meanwhile():
            pl_inst.size += 64
            # the API cancels the instance while its files are being registered
            PluginInstance.objects.get(pk=pl_inst.pk).transition('cancelled',
                                                                 'registeringFiles')
        job._register_output_files = mock.Mock(side_effect=cancelled_meanwhile)

        job.register_output()

        db_inst = PluginInstance.objects.get(pk=pl_inst.id)
        self.assertEqual(db_inst.status, 'cancelled')
        self.assertEqual(db_inst.size, 0)  # the registration that lost wrote nothing
        # but the cancel's cleanup goes again, after whatever the registration inserted
        self.assertEqual(db_inst.remote_cleanup_status, 'deletingData')
        job.enqueue_run.assert_called_once_with('PluginInstanceDeleteJob')

    def test_a_registration_cancelled_before_its_insert_registers_no_files(self):
        pl_inst = self._create_plugin_inst()
        name = f'{pl_inst.get_output_path()}/a.txt'
        self.storage_manager.upload_obj(name, b'12345')
        self.addCleanup(self.storage_manager.delete_path, pl_inst.get_output_path())
        job = pluginjobs.PluginInstanceAppJob(pl_inst)
        job._get_status = mock.Mock(return_value={'compute': {'status':
                                                              'finishedSuccessfully'}})
        job._get_job_json_data = mock.Mock(return_value={})
        job.enqueue_run = mock.Mock()

        def listed_then_cancelled(content):
            job.plugin_inst_output_files = {name}
            # the API cancels it and its cleanup may already be deleting the output
            PluginInstance.objects.get(pk=pl_inst.pk).transition('cancelled',
                                                                 'registeringFiles')

        job.check_files_from_json_exist = mock.Mock(side_effect=listed_then_cancelled)

        job.register_output()

        self.assertFalse(UserFile.objects.filter(fname=name).exists())
        self.assertEqual(PluginInstance.objects.get(pk=pl_inst.pk).status, 'cancelled')

    def test_a_check_that_finds_the_job_undefined_cancels_it_with_code10(self):
        pl_inst = self._create_plugin_inst('started')
        job = pluginjobs.PluginInstanceAppJob(pl_inst)
        job._get_status = mock.Mock(return_value={'compute': {'status': 'undefined',
                                                              'logs': ''}})
        job.enqueue_run = mock.Mock()

        with self.assertLogs('plugininstances.services.pluginjobs', 'ERROR'):
            job.check_exec_status()

        db_inst = PluginInstance.objects.get(pk=pl_inst.pk)
        self.assertEqual((db_inst.status, db_inst.error_code), ('cancelled', 'CODE10'))
        job.enqueue_run.assert_called_once_with('PluginInstanceDeleteJob')

    def test_a_timed_out_check_cancels_from_the_status_it_checked_only(self):
        pl_inst = self._create_plugin_inst('started')
        job = pluginjobs.PluginInstanceAppJob(pl_inst)   # holds 'started'
        job._job_has_timeout = mock.Mock(return_value=True)
        job.enqueue_run = mock.Mock()
        # another task moved it on meanwhile: the timeout is that of a job that ended
        PluginInstance.objects.get(pk=pl_inst.pk).transition('registeringFiles',
                                                             'started')

        with self.assertLogs('plugininstances.services.pluginjobs', 'ERROR'):
            status = job.check_exec_status()

        self.assertEqual(status, 'registeringFiles')
        self.assertEqual(PluginInstance.objects.get(pk=pl_inst.pk).error_code, '')

        # in 'started', as the check read it: cancelled, with the code
        PluginInstance.objects.filter(pk=pl_inst.pk).update(status='started')
        with self.assertLogs('plugininstances.services.pluginjobs', 'ERROR'):
            job.check_exec_status()

        db_inst = PluginInstance.objects.get(pk=pl_inst.pk)
        self.assertEqual((db_inst.status, db_inst.error_code), ('cancelled', 'CODE13'))

    def test_a_soft_time_limit_inside_the_registration_reaches_its_task(self):
        pl_inst = self._create_plugin_inst()
        job = self._registering_job(pl_inst, 10)
        job._register_output_files = mock.Mock(side_effect=SoftTimeLimitExceeded())

        with self.assertRaises(SoftTimeLimitExceeded):
            job.register_output()

        self.assertEqual(PluginInstance.objects.get(pk=pl_inst.pk).status,
                         'registeringFiles')

    def test_a_stale_started_object_cannot_move_a_cancelled_instance_on(self):
        pl_inst = self._create_plugin_inst('started')
        job = pluginjobs.PluginInstanceAppJob(pl_inst)   # holds 'started'
        job._get_status = mock.Mock(return_value={
            'compute': {'status': 'finishedSuccessfully', 'logs': ''}})
        job.enqueue_registration = mock.Mock()
        job.enqueue_run = mock.Mock()
        PluginInstance.objects.get(pk=pl_inst.pk).transition('cancelled', 'started')

        status = job.check_exec_status()

        self.assertEqual(status, 'cancelled')
        self.assertEqual(PluginInstance.objects.get(pk=pl_inst.pk).status, 'cancelled')
        job.enqueue_registration.assert_not_called()
        job.enqueue_run.assert_not_called()

    def test_the_status_check_of_an_ended_job_has_a_task_register_its_files(self):
        pl_inst = self._create_plugin_inst('started')
        job = pluginjobs.PluginInstanceAppJob(pl_inst)
        job.pfcon_client.requires_upload_job = False  # the fixture's needs one on swift, s3
        job._get_status = mock.Mock(return_value={
            'compute': {'status': 'finishedWithError', 'logs': ''}})
        job.enqueue_registration = mock.Mock()
        job.enqueue_run = mock.Mock()
        job.register_output = mock.Mock()

        self.assertEqual(job.check_exec_status(), 'registeringFiles')

        job.enqueue_registration.assert_called_once_with()
        job.enqueue_run.assert_not_called()
        job.register_output.assert_not_called()  # never in the status check

    def test_the_status_check_of_an_ended_job_has_its_upload_job_fetch_the_files(self):
        pl_inst = self._create_plugin_inst('started')
        job = pluginjobs.PluginInstanceAppJob(pl_inst)
        job.pfcon_client.requires_upload_job = True
        job._get_status = mock.Mock(return_value={
            'compute': {'status': 'finishedWithError', 'logs': ''}})
        job.enqueue_registration = mock.Mock()
        job.enqueue_run = mock.Mock()
        job.register_output = mock.Mock()

        self.assertEqual(job.check_exec_status(), 'uploading')

        # the upload job fetches the files; its status check has them registered
        job.enqueue_run.assert_called_once_with('PluginInstanceUploadJob',
                                                moved_to='uploading')
        job.enqueue_registration.assert_not_called()
        job.register_output.assert_not_called()

    def test_a_registration_that_cannot_ask_the_remote_cancels_with_code02(self):
        pl_inst = self._create_plugin_inst()
        job = self._registering_job(pl_inst, 1)
        job._get_status = mock.Mock(side_effect=PfconRequestException('down'))

        with mock.patch.object(pluginjobs.time, 'sleep') as sleep, \
                self.assertLogs('plugininstances.services.pluginjobs', 'ERROR') as logs:
            job.register_output()

        db_inst = PluginInstance.objects.get(pk=pl_inst.id)
        self.assertEqual((db_inst.status, db_inst.error_code), ('cancelled', 'CODE02'))
        self.assertEqual(len([line for line in logs.output if '[CODE02,' in line]),
                         job.REMOTE_STATUS_TRIES)
        self.assertEqual(job._get_status.call_count, job.REMOTE_STATUS_TRIES)
        self.assertEqual(sleep.call_count, job.REMOTE_STATUS_TRIES - 1)

    def test_a_run_a_newer_one_overtook_writes_nothing(self):
        pl_inst = self._create_plugin_inst()
        older = self._registering_job(pl_inst, 10)
        newer = self._registering_job(PluginInstance.objects.get(pk=pl_inst.pk), 20)

        older_token = older._claim_registration()
        newer.register_output()   # claims after the older run did, and ends it
        older.c_plugin_inst.size = 10
        older._end_registration('cancelled', older_token)

        db_inst = PluginInstance.objects.get(pk=pl_inst.id)
        self.assertEqual((db_inst.status, db_inst.size), ('finishedSuccessfully', 20))
        older.enqueue_run.assert_not_called()

    def test_a_run_whose_claim_a_newer_run_took_writes_nothing_while_that_one_runs(self):
        pl_inst = self._create_plugin_inst()
        older = self._registering_job(pl_inst, 10)
        older_token = older._claim_registration()
        newer = self._registering_job(PluginInstance.objects.get(pk=pl_inst.pk), 20)
        newer._claim_registration()               # redelivered, still registering

        older.c_plugin_inst.size = 10
        older._end_registration('finishedSuccessfully', older_token)

        db_inst = PluginInstance.objects.get(pk=pl_inst.pk)
        self.assertEqual((db_inst.status, db_inst.size, db_inst.remote_cleanup_status),
                         ('registeringFiles', 0, 'notStarted'))
        older.enqueue_run.assert_not_called()

    def test_a_registration_run_twice_registers_each_file_once(self):
        pl_inst = self._create_plugin_inst()
        output = pl_inst.get_output_path()
        names = [f'{output}/a.txt', f'{output}/sub/b.txt']

        for name in names:
            self.storage_manager.upload_obj(name, b'12345')
        self.addCleanup(self.storage_manager.delete_path, output)

        inserts = []
        for _ in range(2):
            PluginInstance.objects.filter(pk=pl_inst.pk).update(status='registeringFiles')
            job = pluginjobs.PluginInstanceAppJob(PluginInstance.objects.get(pk=pl_inst.pk))
            job._get_status = mock.Mock(
                return_value={'compute': {'status': 'finishedSuccessfully'}})
            job._get_job_json_data = mock.Mock(return_value={})
            job.check_files_from_json_exist = mock.Mock(
                side_effect=lambda content, job=job: setattr(
                    job, 'plugin_inst_output_files', set(names)))
            job.enqueue_run = mock.Mock()

            with CaptureQueriesContext(connection) as queries:
                job.register_output()
            inserts.append(['ON CONFLICT' in q['sql'] for q in queries
                            if q['sql'].startswith('INSERT INTO "core_chrisfile"')])

        self.assertEqual(UserFile.objects.filter(fname__in=names).count(), 2)
        db_inst = PluginInstance.objects.get(pk=pl_inst.id)
        self.assertEqual((db_inst.status, db_inst.size), ('finishedSuccessfully', 10))
        # only the second run, whose INSERT fails on the first run's rows, skips rows
        self.assertEqual(inserts, [[False], [False, True]])

    def test_a_link_file_written_again_keeps_its_row(self):
        pl_inst = self._create_plugin_inst()
        user = User.objects.get(username=self.username)
        folder = pl_inst.output_folder
        linked = f'home/{self.username}/uploads'
        name = linked.replace('/', '_')

        first = pluginjobs.PluginInstanceAppJob._link_file(linked, user, folder, name)
        first.save(name=name)
        again = pluginjobs.PluginInstanceAppJob._link_file(linked, user, folder, name)

        self.assertEqual(again.pk, first.pk)
        again.save(name=name)  # writes its storage object again, no second row
        self.assertEqual(ChrisLinkFile.objects.filter(fname=first.fname.name).count(), 1)

    def test_a_link_file_name_that_links_another_path_is_refused(self):
        pl_inst = self._create_plugin_inst()
        user = User.objects.get(username=self.username)
        folder = pl_inst.output_folder
        # 'a/b_c' and 'a_b/c' both come out as 'a_b_c'
        first = pluginjobs.PluginInstanceAppJob._link_file('a/b_c', user, folder, 'a_b_c')
        first.save(name='a_b_c')

        with self.assertRaisesRegex(ValueError, 'links a/b_c, not a_b/c'):
            pluginjobs.PluginInstanceAppJob._link_file('a_b/c', user, folder, 'a_b_c')

    def test_two_links_to_one_path_in_one_run_are_written_and_counted_once(self):
        pl_inst = self._create_plugin_inst('registeringFiles')
        job = pluginjobs.PluginInstanceAppJob(pl_inst)
        folder = pl_inst.output_folder
        linked = f'home/{self.username}/uploads/data'
        ChrisFolder.objects.get_or_create(path=linked, owner=pl_inst.owner)

        job._create_chris_link_file(linked, folder)
        size = pl_inst.size
        job._create_chris_link_file(linked + '/', folder)  # from another parent's output

        self.assertEqual(pl_inst.size, size)
        self.assertEqual(ChrisLinkFile.objects.filter(parent_folder=folder).count(), 1)

    def test_submitting_a_job_another_task_submitted_does_not_cancel_it(self):
        pl_inst = self._create_plugin_inst('scheduled')
        job = pluginjobs.PluginInstanceAppJob(pl_inst)
        # the remote refused a second container for the job, which exists
        job.pfcon_client.submit_job = mock.Mock(
            side_effect=PfconRequestException('Internal Server Error', code=500))
        job._get_status = mock.Mock(return_value={
            'compute': {'status': 'started', 'logs': ''}})
        job.get_cmd_args = mock.Mock(return_value=[])
        job.enqueue_run = mock.Mock()

        logging.disable(logging.INFO)  # setUp disables WARNING and below
        # (this compute resource takes the legacy path, which logs the refusal with a
        # traceback and retries with a new token)
        with self.assertLogs('plugininstances.services', 'WARNING') as logs:
            job.run()

        self.assertIn('another task, or an earlier try of this one, submitted it',
                      logs.output[-1])
        self.assertFalse([line for line in logs.output if '[CODE01,' in line])
        db_inst = PluginInstance.objects.get(pk=pl_inst.pk)
        self.assertEqual((db_inst.status, db_inst.error_code), ('started', ''))
        job.enqueue_run.assert_not_called()

    def test_submitting_a_job_the_remote_refused_and_lacks_cancels_it_with_code01(self):
        pl_inst = self._create_plugin_inst('scheduled')
        job = pluginjobs.PluginInstanceAppJob(pl_inst)
        job.pfcon_client.requires_copy_job = True  # no legacy retry with a new token
        job.pfcon_client.submit_job = mock.Mock(
            side_effect=PfconRequestException('Bad Request', code=400))
        job._get_status = mock.Mock(side_effect=PfconRequestException('Not Found', code=404))
        job.get_cmd_args = mock.Mock(return_value=[])
        job.enqueue_run = mock.Mock()

        with self.assertLogs('plugininstances.services', 'ERROR') as logs:
            job.run()

        self.assertIn(f'[CODE01,{job.str_job_id}]: Error submitting plugin job',
                      logs.output[-1])
        db_inst = PluginInstance.objects.get(pk=pl_inst.pk)
        self.assertEqual((db_inst.status, db_inst.error_code, db_inst.remote_cleanup_status),
                         ('cancelled', 'CODE01', 'deletingData'))
        job.enqueue_run.assert_called_once_with('PluginInstanceDeleteJob')

    def test_a_job_submitted_after_its_instance_was_cancelled_gets_cleaned_up(self):
        pl_inst = self._create_plugin_inst('scheduled')
        PluginInstance.objects.filter(pk=pl_inst.pk).update(
            status='cancelled', remote_cleanup_status='complete')
        job = pluginjobs.PluginInstanceAppJob(pl_inst)   # holds 'scheduled'
        job.pfcon_client.submit_job = mock.Mock(return_value={
            'compute': {'status': 'started', 'logs': ''}})
        job.get_cmd_args = mock.Mock(return_value=[])
        job.enqueue_run = mock.Mock()

        job.run()

        db_inst = PluginInstance.objects.get(pk=pl_inst.pk)
        self.assertEqual(db_inst.status, 'cancelled')
        # a cleanup that completed before the job existed is started again
        self.assertEqual(db_inst.remote_cleanup_status, 'deletingData')
        job.enqueue_run.assert_called_once_with('PluginInstanceDeleteJob')

    def test_create_chris_link_file_refuses_unauthorized_path(self):
        """
        Test whether _create_chris_link_file refuses to create a link file to a
        path the plugin instance owner is not allowed to access.
        """
        pl_inst = self._create_plugin_inst('started')
        job = pluginjobs.PluginInstanceAppJob(pl_inst)

        other = User.objects.create_user(username='other', password='other-pass')
        other_folder, _ = ChrisFolder.objects.get_or_create(
            path='home/other/uploads', owner=other)

        parent_folder, _ = ChrisFolder.objects.get_or_create(
            path=pl_inst.get_output_path(), owner=pl_inst.owner)

        # refusing the unauthorized path logs at ERROR (CODE17) before raising;
        # capture/suppress with assertLogs and verify the expected message fired
        with self.assertLogs('plugininstances.services.pluginjobs',
                              level='ERROR') as cm:
            with self.assertRaises(ValueError):
                job._create_chris_link_file(other_folder.path, parent_folder)
        self.assertEqual(pl_inst.error_code, 'CODE17')
        self.assertTrue(any('CODE17' in msg and 'home/other/uploads' in msg
                            for msg in cm.output))

    def test_find_all_storage_object_paths_refuses_unauthorized_link(self):
        """
        Test whether find_all_storage_object_paths refuses to follow a ChRIS link
        file pointing to a path the plugin instance owner is not allowed to access.
        """
        pl_inst = self._create_plugin_inst('started')
        job = pluginjobs.PluginInstanceAppJob(pl_inst)

        other = User.objects.create_user(username='other', password='other-pass')
        ChrisFolder.objects.get_or_create(path='home/other/uploads', owner=other)

        storage_path = 'home/%s/uploads' % self.username
        link_obj = storage_path + '/secret.chrislink'

        job.storage_manager = mock.Mock()
        job.storage_manager.ls = mock.Mock(return_value=[link_obj])
        job.storage_manager.download_obj = mock.Mock(
            return_value=b'home/other/uploads')

        obj_paths = set()
        visited_paths = set()
        # refusing the unauthorized link logs at ERROR (CODE17) before raising;
        # capture/suppress with assertLogs and verify the expected message fired
        with self.assertLogs('plugininstances.services.pluginjobs',
                              level='ERROR') as cm:
            with self.assertRaises(ValueError):
                job.find_all_storage_object_paths(storage_path, obj_paths,
                                                  visited_paths)
        self.assertEqual(pl_inst.error_code, 'CODE17')
        self.assertTrue(any('CODE17' in msg and 'home/other/uploads' in msg
                            for msg in cm.output))

    def test_find_all_storage_object_paths_follows_authorized_link(self):
        """
        Test whether find_all_storage_object_paths still follows a ChRIS link file
        pointing to a path the plugin instance owner is allowed to access.
        """
        user = User.objects.get(username=self.username)
        pl_inst = self._create_plugin_inst('started')
        job = pluginjobs.PluginInstanceAppJob(pl_inst)

        linked_path = 'home/%s/uploads' % self.username
        ChrisFolder.objects.get_or_create(path=linked_path, owner=user)

        storage_path = 'home/%s/uploads_input' % self.username
        link_obj = storage_path + '/data.chrislink'
        linked_file = linked_path + '/file.txt'

        def fake_ls(path):
            if path == storage_path:
                return [link_obj]
            if path == linked_path:
                return [linked_file]
            return []

        job.storage_manager = mock.Mock()
        job.storage_manager.ls = mock.Mock(side_effect=fake_ls)
        job.storage_manager.download_obj = mock.Mock(
            return_value=linked_path.encode())

        obj_paths = set()
        visited_paths = set()
        job.find_all_storage_object_paths(storage_path, obj_paths, visited_paths)

        self.assertIn(link_obj, obj_paths)
        self.assertIn(linked_file, obj_paths)
        self.assertEqual(pl_inst.error_code, '')

    @tag('integration')
    def test_integration_register_output_files_refuses_remote_link_file(self):
        """
        Test whether _register_output_files refuses to register a .chrislink file
        coming from the remote compute and deletes it from storage, while still
        registering regular output files.
        """
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        pl_inst = PluginInstance.objects.create(
            plugin=plugin, owner=user, status='finishedSuccessfully',
            compute_resource=plugin.compute_resources.all()[0])

        outputdir = pl_inst.get_output_path()

        with io.StringIO('Test file') as f:
            self.storage_manager.upload_obj(outputdir + '/data.txt', f.read(),
                                            content_type='text/plain')
        with io.StringIO('home/other/uploads') as f:
            self.storage_manager.upload_obj(outputdir + '/evil.chrislink',
                                            f.read(), content_type='text/plain')

        job = pluginjobs.PluginInstanceAppJob(pl_inst)
        job.plugin_inst_output_files = {outputdir + '/data.txt',
                                        outputdir + '/evil.chrislink'}
        job._register_output_files()

        fnames = [f.fname.name for f in pl_inst.output_folder.chris_files.all()]
        self.assertIn(outputdir + '/data.txt', fnames)
        self.assertNotIn(outputdir + '/evil.chrislink', fnames)

        self.assertFalse(
            self.storage_manager.obj_exists(outputdir + '/evil.chrislink'))
        self.assertTrue(
            self.storage_manager.obj_exists(outputdir + '/data.txt'))

        self.assertIn(outputdir + '/data.txt', job.plugin_inst_output_files)
        self.assertNotIn(outputdir + '/evil.chrislink',
                         job.plugin_inst_output_files)

        # delete files from storage
        self.storage_manager.delete_path(outputdir)

    def test_register_output_files_fails_when_link_delete_fails(self):
        """
        Test whether _register_output_files fails the job (sets CODE07 and
        propagates the exception) when a refused remote link file cannot be
        deleted from storage.
        """
        pl_inst = self._create_plugin_inst('started')
        job = pluginjobs.PluginInstanceAppJob(pl_inst)
        outputdir = pl_inst.get_output_path()

        job.storage_manager = mock.Mock()
        job.storage_manager.sanitize_obj_names = mock.Mock(return_value={})
        job.storage_manager.delete_obj = mock.Mock(
            side_effect=Exception('boom'))
        job.plugin_inst_output_files = {outputdir + '/evil.chrislink'}

        # the failed deletion logs at ERROR (CODE07) before re-raising; capture/
        # suppress with assertLogs and verify the expected message fired
        with self.assertLogs('plugininstances.services.pluginjobs',
                              level='ERROR') as cm:
            with self.assertRaises(Exception):
                job._register_output_files()
        self.assertEqual(pl_inst.error_code, 'CODE07')
        self.assertTrue(any('CODE07' in msg and 'boom' in msg
                            for msg in cm.output))

    @tag('integration')
    def test_integration_register_output_files_keeps_cube_generated_link_file(self):
        """
        Test whether _register_output_files preserves legit ChRIS link files
        created by CUBE (via _create_chris_link_file) for 'unextpath'/'ts' flows,
        i.e. they are not in plugin_inst_output_files and are not scrubbed.
        """
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        pl_inst = PluginInstance.objects.create(
            plugin=plugin, owner=user, status='finishedSuccessfully',
            compute_resource=plugin.compute_resources.all()[0])
        outputdir = pl_inst.get_output_path()

        # an authorized folder owned by the instance owner, used as link target
        target_folder, _ = ChrisFolder.objects.get_or_create(
            path=f'home/{self.username}/uploads', owner=user)
        with io.StringIO('Test file') as f:
            self.storage_manager.upload_obj(
                f'home/{self.username}/uploads/in.txt', f.read(),
                content_type='text/plain')

        # a regular remote output file
        with io.StringIO('Test file') as f:
            self.storage_manager.upload_obj(outputdir + '/data.txt', f.read(),
                                            content_type='text/plain')

        job = pluginjobs.PluginInstanceAppJob(pl_inst)
        parent_folder, _ = ChrisFolder.objects.get_or_create(
            path=outputdir, owner=user)
        job._create_chris_link_file(target_folder.path, parent_folder)

        link_path = (outputdir + '/' +
                     target_folder.path.replace('/', '_') + '.chrislink')

        # CUBE-generated link is a ChrisLinkFile and NOT in the remote-output set
        self.assertTrue(ChrisLinkFile.objects.filter(fname=link_path).exists())
        self.assertNotIn(link_path, job.plugin_inst_output_files)

        job.plugin_inst_output_files = {outputdir + '/data.txt'}
        job._register_output_files()

        # the link file survived the scrub (storage + DB)
        self.assertTrue(self.storage_manager.obj_exists(link_path))
        self.assertTrue(ChrisLinkFile.objects.filter(fname=link_path).exists())

        # delete files from storage
        self.storage_manager.delete_path(outputdir)
        self.storage_manager.delete_obj(f'home/{self.username}/uploads/in.txt')


class RegistrationSizeTests(PluginInstanceFixture, TestCase):
    """
    PluginInstanceAppJob.registers_at_most: on a mocked storage, what it lists, how much of
    it and what it answers; on the stack's storage, that it counts what a registration of
    the same output registers. And the 'ts' parameters it reads.
    """

    def job(self, inst, listings, unextpaths=(), inputs=()):
        """
        An app job of ``inst`` whose storage lists ``listings[prefix]`` (as much of it as
        the limit asked lets), whose 'unextpath' parameter names ``unextpaths`` and, as a
        'ts' plugin's, whose input instances are ``inputs``.
        """
        job = pluginjobs.PluginInstanceAppJob(inst)
        job.storage_manager = mock.Mock()
        job.storage_manager.ls.side_effect = (
            lambda prefix, limit=None: listings.get(prefix, [])[:limit])
        job.get_plugin_instance_path_parameters = mock.Mock(
            return_value=({'--dir': ','.join(unextpaths)} if unextpaths else {}, {}))
        job.get_ts_plugin_instance_parameters = mock.Mock(
            return_value=([str(i.pk) for i in inputs], [], False))
        return job

    def test_an_output_of_at_most_the_files_is_small_and_one_of_more_is_not(self):
        inst = self.instance('registeringFiles')
        output = inst.get_output_path()
        job = self.job(inst, {output: [f'{output}/{i}' for i in range(3)]})

        self.assertTrue(job.registers_at_most(3))
        self.assertFalse(job.registers_at_most(2))
        self.assertEqual(job.storage_manager.ls.call_args_list,
                         [mock.call(output, limit=4), mock.call(output, limit=3)])

    def test_an_output_an_out_of_network_remote_sends_as_a_zip_is_not_small(self):
        inst = self.instance('registeringFiles')
        inst.compute_resource.compute_innetwork = False
        job = self.job(inst, {})

        self.assertFalse(job.registers_at_most(100))
        job.storage_manager.ls.assert_not_called()

    def test_unextpath_paths_add_their_objects_under_filesystem_storage_else_a_link(self):
        inst = self.instance('registeringFiles')
        output = inst.get_output_path()
        listings = {output: [f'{output}/x'],
                    'a': ['a/1', 'a/2'], 'b': ['b/1', 'b/2', 'b/3']}

        job = self.job(inst, listings, unextpaths=('a', 'b'))
        job.storage_env = 'filesystem'  # copies of every object
        self.assertTrue(job.registers_at_most(6))
        self.assertFalse(job.registers_at_most(5))

        job = self.job(inst, listings, unextpaths=('a', 'b'))
        job.storage_env = 'fslink'  # a link file per path
        self.assertTrue(job.registers_at_most(3))
        self.assertFalse(job.registers_at_most(2))
        self.assertEqual({c.args[0] for c in job.storage_manager.ls.call_args_list},
                         {output})

    def test_a_ts_instance_adds_its_inputs_objects_and_one_with_a_link_is_large(self):
        inst = self.instance('registeringFiles')
        first, second = (self.instance('finishedSuccessfully') for _ in range(2))
        listings = {first.get_output_path(): ['p/1', 'p/2'],
                    second.get_output_path(): ['q/1', 'q/2']}

        job = self.job(inst, listings, inputs=(first, second))
        self.assertTrue(job.registers_at_most(4))
        limits = [c.kwargs['limit'] for c in job.storage_manager.ls.call_args_list]
        # the output, then each input with the room left
        self.assertEqual(limits, [5, 5, 3])
        self.assertFalse(job.registers_at_most(3))

        listings[second.get_output_path()] = ['q/1', 'q/link.chrislink']
        job = self.job(inst, listings, inputs=(first, second))
        self.assertFalse(job.registers_at_most(10))

    @staticmethod
    def _delete(path):
        storage = connect_storage(settings)
        if storage.path_exists(path):
            storage.delete_path(path)

    def _upload(self, path, names):
        """
        Upload a small object at each of ``names`` under ``path`` in the stack's storage,
        deleted at the end of the test.
        """
        self.addCleanup(self._delete, path)
        storage = connect_storage(settings)
        for name in names:
            storage.upload_obj(f'{path}/{name}', b'x')

    def _register_for_real(self, job, names=()):
        """
        Run ``job``'s registration on the stack's storage and database, its remote mocked:
        the job ended successfully and its JSON lists ``names`` in the output. Returns how
        many files and link files it registered.
        """
        output = job.c_plugin_inst.get_output_path()
        job._get_status = mock.Mock(
            return_value={'compute': {'status': 'finishedSuccessfully'}})
        job._get_job_json_data = mock.Mock(
            return_value={'job_output_path': output, 'rel_file_paths': list(names)})
        job.enqueue_run = mock.Mock()  # the remote cleanup
        self.addCleanup(self._delete, output)

        job.register_output()

        self.assertEqual(PluginInstance.objects.get(pk=job.c_plugin_inst.pk).status,
                         'finishedSuccessfully')
        return len(job.plugin_inst_output_files) + len(job._links_written)

    def _ts_plugin(self):
        (meta, _) = PluginMeta.objects.get_or_create(name='pl-topologicalcopy', type='ts')
        (ts_plugin, _) = Plugin.objects.get_or_create(meta=meta, version='0.1')
        ts_plugin.compute_resources.set([self.compute_resource])
        return ts_plugin

    def test_the_count_of_an_output_is_what_its_registration_registers(self):
        inst = self.instance('registeringFiles')
        names = ('a.txt', 'sub/b.txt', 'sub/c.txt')
        self._upload(inst.get_output_path(), names)
        job = pluginjobs.PluginInstanceAppJob(inst)

        self.assertTrue(job.registers_at_most(3))
        self.assertFalse(job.registers_at_most(2))
        self.assertEqual(self._register_for_real(job, names), 3)

    def test_the_count_of_a_ts_instance_is_what_its_registration_copies(self):
        ts_plugin = self._ts_plugin()
        parents = [self.instance('finishedSuccessfully') for _ in range(2)]
        self._upload(parents[0].get_output_path(), ('a.txt', 'sub/b.txt'))
        self._upload(parents[1].get_output_path(), ('c.txt', 'sub/d.txt'))
        inst = PluginInstance.objects.create(plugin=ts_plugin, owner=self.user,
                                             previous=parents[0],
                                             compute_resource=self.compute_resource)
        param = PluginParameter.objects.create(plugin=ts_plugin, name='plugininstances',
                                               flag='--plugininstances')
        StrParameter.objects.create(plugin_inst=inst, plugin_param=param,
                                    value=f'{parents[0].pk},{parents[1].pk}')
        PluginInstance.objects.filter(pk=inst.pk).update(status='registeringFiles')
        job = pluginjobs.PluginInstanceAppJob(PluginInstance.objects.get(pk=inst.pk))

        self.assertTrue(job.registers_at_most(4))
        self.assertFalse(job.registers_at_most(3))
        self.assertEqual(self._register_for_real(job), 4)

    def test_a_most_of_0_takes_only_an_output_without_files(self):
        inst = self.instance('registeringFiles')
        output = inst.get_output_path()

        self.assertTrue(self.job(inst, {}).registers_at_most(0))
        self.assertFalse(self.job(inst, {output: [f'{output}/a']}).registers_at_most(0))

    def test_a_link_file_in_the_output_itself_counts_as_one_file(self):
        inst = self.instance('registeringFiles')
        output = inst.get_output_path()
        job = self.job(inst, {output: [f'{output}/x.chrislink', f'{output}/y']})

        self.assertTrue(job.registers_at_most(2))
        self.assertFalse(job.registers_at_most(1))

    def test_the_ts_parameters_are_read_from_a_ts_instance_only(self):
        ts_plugin = self._ts_plugin()
        parent = self.instance('finishedSuccessfully')
        inst = PluginInstance.objects.create(plugin=ts_plugin, owner=self.user,
                                             previous=parent,
                                             compute_resource=self.compute_resource)
        for name, value in (('plugininstances', '4,5'), ('filter', r'\.txt$,')):
            param = PluginParameter.objects.create(plugin=ts_plugin, name=name,
                                                   flag=f'--{name}')
            StrParameter.objects.create(plugin_inst=inst, plugin_param=param, value=value)
        param = PluginParameter.objects.create(plugin=ts_plugin, name='groupByInstance',
                                               flag='--groupByInstance', type='boolean')
        BoolParameter.objects.create(plugin_inst=inst, plugin_param=param, value=True)

        self.assertEqual(
            pluginjobs.PluginInstanceAppJob(inst).get_ts_plugin_instance_parameters(),
            (['4', '5'], [r'\.txt$', ''], True))
        fs_inst = self.instance('registeringFiles')  # a parameter of that name too
        param = PluginParameter.objects.create(plugin=fs_inst.plugin, name='plugininstances',
                                               flag='--plugininstances')
        StrParameter.objects.create(plugin_inst=fs_inst, plugin_param=param, value='4')
        self.assertEqual(
            pluginjobs.PluginInstanceAppJob(fs_inst).get_ts_plugin_instance_parameters(),
            ([], [], False))

    def test_the_count_stops_listing_once_it_is_past_the_most(self):
        inst = self.instance('registeringFiles')
        output = inst.get_output_path()
        parent = self.instance('finishedSuccessfully')

        job = self.job(inst, {output: ['o/1', 'o/2', 'o/3']}, inputs=(parent,))
        self.assertFalse(job.registers_at_most(2))
        job.storage_manager.ls.assert_called_once_with(output, limit=3)

        job = self.job(inst, {}, unextpaths=('a', 'b', 'c'))
        job.storage_env = 'fslink'  # three link files: past two before any listing
        self.assertFalse(job.registers_at_most(2))
        job.storage_manager.ls.assert_not_called()
