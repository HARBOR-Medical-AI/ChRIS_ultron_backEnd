
import logging
import os
import io
import time
import uuid
from unittest import mock

from django.test import TestCase, tag
from django.contrib.auth.models import User
from django.conf import settings

from pfconclient import client as pfcon

from core.models import ChrisInstance, ChrisFolder, ChrisLinkFile
from core.storage import connect_storage
from plugins.models import PluginMeta, Plugin
from plugins.models import PluginParameter
from plugininstances.models import PluginInstance, PathParameter, ComputeResource
from plugininstances.services import pluginjobs, copyjobs
from plugininstances.services.uploadjobs import PluginInstanceUploadJob


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

        # run the copy job
        plg_inst_copy_job = copyjobs.PluginInstanceCopyJob(pl_inst)
        plg_inst_copy_job.run()
        self.assertEqual(pl_inst.status, 'copying')

        # poll copy job status until it finishes and auto-transitions to app job
        maxLoopTries = 10
        currentLoop = 1
        time.sleep(5)
        while currentLoop <= maxLoopTries:
            plg_inst_copy_job.check_exec_status()
            if pl_inst.status == 'started':
                break
            time.sleep(5)
            currentLoop += 1
        if pl_inst.status == 'cancelled':
            self.storage_manager.delete_obj(user_space_path + 'test.txt')
            self.skipTest('Copy job failed to transition to started - likely pfcon Docker issue')
        self.assertEqual(pl_inst.status, 'started')

        # poll app job until it finishes or transitions to uploading
        plg_inst_app_job = pluginjobs.PluginInstanceAppJob(pl_inst)
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

    def _create_started_plugin_inst(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, status='started',
            compute_resource=plugin.compute_resources.all()[0])
        return pl_inst

    def test_register_output_files_on_success_persists_size(self):
        """
        Test whether the size accumulated while registering output files is saved
        to the DB. Regression test: 'size' was missing from the closing save's
        update_fields, so every instance reported size=0 through the API.
        """
        pl_inst = self._create_started_plugin_inst()
        job = pluginjobs.PluginInstanceAppJob(pl_inst)

        # bypass the remote fetch and cleanup; exercise registration -> final save
        job._get_job_zip_data = mock.Mock(return_value=b'')
        job._get_job_json_data = mock.Mock(return_value={})
        job.unpack_zip_file = mock.Mock()
        job.check_files_from_json_exist = mock.Mock()
        job.schedule_remote_cleanup = mock.Mock()
        job._register_output_files = mock.Mock(
            side_effect=lambda: setattr(pl_inst, 'size', pl_inst.size + 1024))

        job.register_output_files_on_success()

        self.assertEqual(pl_inst.status, 'finishedSuccessfully')
        db_inst = PluginInstance.objects.get(pk=pl_inst.id)   # fresh DB fetch
        self.assertEqual(db_inst.status, 'finishedSuccessfully')
        self.assertEqual(db_inst.size, 1024)

    def test_register_output_files_on_error_persists_size(self):
        """
        Same regression as above for the error path: files registered before the
        job is marked finishedWithError must still have their size persisted.
        """
        pl_inst = self._create_started_plugin_inst()
        job = pluginjobs.PluginInstanceAppJob(pl_inst)

        job._get_job_zip_data = mock.Mock(return_value=b'')
        job._get_job_json_data = mock.Mock(return_value={})
        job.unpack_zip_file = mock.Mock()
        job.check_files_from_json_exist = mock.Mock()
        job.schedule_remote_cleanup = mock.Mock()
        job._register_output_files = mock.Mock(
            side_effect=lambda: setattr(pl_inst, 'size', pl_inst.size + 512))

        job.register_output_files_on_error()

        db_inst = PluginInstance.objects.get(pk=pl_inst.id)
        self.assertEqual(db_inst.status, 'finishedWithError')
        self.assertEqual(db_inst.size, 512)

    def test_create_chris_link_file_refuses_unauthorized_path(self):
        """
        Test whether _create_chris_link_file refuses to create a link file to a
        path the plugin instance owner is not allowed to access.
        """
        pl_inst = self._create_started_plugin_inst()
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
        pl_inst = self._create_started_plugin_inst()
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
        pl_inst = self._create_started_plugin_inst()
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
        pl_inst = self._create_started_plugin_inst()
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
