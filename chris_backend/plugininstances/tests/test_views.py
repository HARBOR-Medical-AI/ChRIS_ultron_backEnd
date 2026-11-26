
import logging
import json
import time
import io
import os
from unittest import mock, skip

from django.test import TestCase, TransactionTestCase, tag
from django.urls import reverse
from django.contrib.auth.models import User
from django.conf import settings
from rest_framework import status

from celery.contrib.testing.worker import start_worker
import uuid

from pfconclient import client as pfcon

from core.models import ChrisFolder, ChrisInstance
from core.celery import app as celery_app
from core.storage import connect_storage
from core.tests.query_counts import QueryCountAssertionsMixin
from userfiles.models import UserFile
from pipelines.models import Pipeline
from plugins.models import PluginMeta, Plugin, PluginParameter, ComputeResource
from plugininstances.models import PluginInstance
from plugininstances.models import PathParameter, FloatParameter
from plugininstances.services.pluginjobs import PluginInstanceAppJob
from plugininstances.services.copyjobs import PluginInstanceCopyJob
from plugininstances.services.uploadjobs import PluginInstanceUploadJob
from plugininstances import tasks, views
from workflows.models import Workflow


COMPUTE_RESOURCE_URL = settings.COMPUTE_RESOURCE_URL
CHRIS_SUPERUSER_PASSWORD = settings.CHRIS_SUPERUSER_PASSWORD


def create_workflow(owner):
    """
    Create a workflow of a new pipeline, both owned by owner.
    """
    pipeline = Pipeline.objects.create(name='Pipeline1', owner=owner)
    return Workflow.objects.create(title='Workflow1', pipeline=pipeline, owner=owner)


def create_ds_instances(owner, previous, count, workflow=None):
    """
    Create count 'mri_convert' plugin instances of owner after the previous instance,
    in the workflow when one is given, and return them.
    """
    plugin = Plugin.objects.get(meta__name='mri_convert')
    return [PluginInstance.objects.create(
        plugin=plugin, owner=owner, previous=previous, workflow=workflow, title=f'ds{i}',
        compute_resource=plugin.compute_resources.all()[0]) for i in range(count)]


class ViewTests(QueryCountAssertionsMixin, TestCase):
    
    def setUp(self):
        # avoid cluttered console output (for instance logging all the http requests)
        logging.disable(logging.WARNING)

        # use a unique job_id_prefix to avoid pfcon job ID collisions with
        # stale jobs from prior test runs
        chris_inst = ChrisInstance.load()
        chris_inst.job_id_prefix = f'test-{uuid.uuid4().hex[:8]}-'
        chris_inst.save(update_fields=['job_id_prefix'])

        # superuser chris (owner of root and top-level folders)
        self.chris_username = 'chris'
        self.chris_password = CHRIS_SUPERUSER_PASSWORD

        self.username = 'foo'
        self.password = 'bar'
        self.other_username = 'boo'
        self.other_password = 'far'
        self.compute_user = 'pfcon'
        self.compute_password = 'pfcon1234'

        self.content_type = 'application/vnd.collection+json'

        token = pfcon.Client.get_auth_token(COMPUTE_RESOURCE_URL + 'auth-token/', 'pfcon',
                                            'pfcon1234')
        pfcon_client = pfcon.Client(COMPUTE_RESOURCE_URL, token)
        pfcon_client.get_server_info()

        (self.compute_resource, tf) = ComputeResource.objects.update_or_create(
            name='host',
            defaults={'compute_url': COMPUTE_RESOURCE_URL,
                      'compute_user': self.compute_user,
                      'compute_password': self.compute_password,
                      'compute_innetwork': pfcon_client.pfcon_innetwork,
                      'compute_requires_copy_job': False,
                      'compute_requires_upload_job': pfcon_client.pfcon_innetwork and settings.STORAGE_ENV in ('swift', 's3')})

        # create users
        User.objects.create_user(username=self.other_username,
                                 password=self.other_password)
        User.objects.create_user(username=self.username,
                                 password=self.password)

        # create two plugins
        (pl_meta, tf) = PluginMeta.objects.get_or_create(name='pacspull', type='fs')
        (plugin_fs, tf) = Plugin.objects.get_or_create(meta=pl_meta, version='0.1')
        plugin_fs.compute_resources.set([self.compute_resource])
        plugin_fs.save()

        (pl_meta, tf) = PluginMeta.objects.get_or_create(name='mri_convert', type='ds')
        (plugin_ds, tf) = Plugin.objects.get_or_create(meta=pl_meta, version='0.1')
        plugin_ds.compute_resources.set([self.compute_resource])
        plugin_ds.save()

    def tearDown(self):
        # re-enable logging
        logging.disable(logging.NOTSET)


class TasksViewTests(QueryCountAssertionsMixin, TransactionTestCase):

    @classmethod
    def setUpClass(cls):
        logging.disable(logging.WARNING)
        logging.getLogger('plugininstances.tasks').setLevel(logging.CRITICAL)
        super().setUpClass()
        # the test runner routes every task to the default 'celery' queue that only
        # the tests use (core/testrunner.py)
        # purge any stale messages from the default queue before starting
        # the worker, so leftover tasks from previous runs don't produce noise
        try:
            with celery_app.connection_for_write() as conn:
                conn.default_channel.queue_purge('celery')
        except Exception:
            pass
        cls.celery_worker = start_worker(celery_app,
                                         concurrency=1,
                                         perform_ping_check=False)
        cls.celery_worker.__enter__()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.celery_worker.__exit__(None, None, None)
        logging.getLogger('plugininstances.tasks').setLevel(logging.NOTSET)
        logging.disable(logging.NOTSET)

    def tearDown(self):
        # purge any unstarted Celery tasks then wait for in-flight tasks to finish
        # before the TransactionTestCase DB truncation to prevent spurious errors
        # from stale cleanup tasks finding missing DB records
        try:
            celery_app.control.purge()
        except Exception:
            pass
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                active = celery_app.control.inspect(timeout=1).active() or {}
                if all(len(tasks) == 0 for tasks in active.values()):
                    break
            except Exception:
                break
            time.sleep(0.5)
        super().tearDown()

    def setUp(self):
        # use a unique job_id_prefix to avoid pfcon job ID collisions with
        # stale jobs from prior test runs
        chris_inst = ChrisInstance.load()
        chris_inst.job_id_prefix = f'test-{uuid.uuid4().hex[:8]}-'
        chris_inst.save(update_fields=['job_id_prefix'])

        # create superuser chris (owner of root folders)
        self.chris_username = 'chris'
        self.chris_password = CHRIS_SUPERUSER_PASSWORD

        self.storage_manager = connect_storage(settings)
        self.username = 'foo'
        self.password = 'bar'
        self.other_username = 'boo'
        self.other_password = 'far'
        self.compute_user = 'pfcon'
        self.compute_password = 'pfcon1234'
        self.content_type = 'application/vnd.collection+json'

        token = pfcon.Client.get_auth_token(COMPUTE_RESOURCE_URL + 'auth-token/', 'pfcon',
                                            'pfcon1234')
        pfcon_client = pfcon.Client(COMPUTE_RESOURCE_URL, token)
        pfcon_client.get_server_info()

        (self.compute_resource, tf) = ComputeResource.objects.update_or_create(
            name='host',
            defaults={'compute_url': COMPUTE_RESOURCE_URL,
                      'compute_user': self.compute_user,
                      'compute_password': self.compute_password,
                      'compute_innetwork': pfcon_client.pfcon_innetwork,
                      'compute_requires_copy_job': False,
                      'compute_requires_upload_job': pfcon_client.pfcon_innetwork and settings.STORAGE_ENV in ('swift', 's3')})

        # create users
        User.objects.create_user(username=self.other_username,
                                 password=self.other_password)
        user = User.objects.create_user(username=self.username,
                                        password=self.password)

        # create two plugins
        (pl_meta, tf) = PluginMeta.objects.get_or_create(name='pacspull', type='fs')
        (plugin_fs, tf) = Plugin.objects.get_or_create(meta=pl_meta, version='0.1')
        plugin_fs.compute_resources.set([self.compute_resource])
        plugin_fs.save()

        (pl_meta, tf) = PluginMeta.objects.get_or_create(name='mri_convert', type='ds')
        (plugin_ds, tf) = Plugin.objects.get_or_create(meta=pl_meta, version='0.1')
        plugin_ds.compute_resources.set([self.compute_resource])
        plugin_ds.save()

        # create pacspull fs plugin instance
        (self.pl_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin_fs, owner=user,
            compute_resource=plugin_fs.compute_resources.all()[0])

        # create mri_convert ds plugin instance
        PluginInstance.objects.get_or_create(
            plugin=plugin_ds, owner=user, previous=self.pl_inst,
            compute_resource=plugin_ds.compute_resources.all()[0])


class PluginInstanceListViewTests(TasksViewTests):
    """
    Test the plugininstance-list view.
    """

    def setUp(self):
        super(PluginInstanceListViewTests, self).setUp()

        plugin = Plugin.objects.get(meta__name="pacspull")
        self.create_read_url = reverse("plugininstance-list", kwargs={"pk": plugin.id})
        self.user_space_path = f'home/{self.username}/uploads/'
        user = User.objects.get(username=self.username)
        ChrisFolder.objects.get_or_create(path=self.user_space_path.rstrip('/'), owner=user)
        self.post = json.dumps(
            {"template": {"data": [{"name": "dir", "value": self.user_space_path},
                                   {"name": "title", "value": 'test1'}]}})

    def test_plugin_instance_create_success(self):
        # add parameters to the plugin before the POST request
        plugin = Plugin.objects.get(meta__name="pacspull")
        PluginParameter.objects.get_or_create(plugin=plugin, name='dir', type='string',
                                              optional=False)

        # first test 'fs' plugin instance (has no previous plugin instance)

        with mock.patch('plugininstances.tasks.run_plugin_instance_job') as run_job_mock:
            # make API request
            self.client.login(username=self.username, password=self.password)
            response = self.client.post(self.create_read_url, data=self.post,
                                        content_type=self.content_type)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)

            # check that the run_plugin_instance task was called with appropriate args
            run_job_mock.delay.assert_called_with(response.data['id'], 'PluginInstanceAppJob')
            self.assertEqual(response.data['status'], 'scheduled')

        # now test 'ds' plugin instance (has previous plugin instance)

        previous_plg_inst = PluginInstance.objects.get(title='test1')
        plugin = Plugin.objects.get(meta__name="mri_convert")
        create_read_url = reverse("plugininstance-list", kwargs={"pk": plugin.id})
        post = json.dumps(
            {"template": {"data": [{"name": "previous_id", "value": previous_plg_inst.id}]}})

        previous_plg_inst.status = 'finishedSuccessfully'
        previous_plg_inst.save()
        with mock.patch('plugininstances.tasks.run_plugin_instance_job') as run_job_mock:
            self.client.login(username=self.username, password=self.password)
            response = self.client.post(create_read_url, data=post,
                                        content_type=self.content_type)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)

            # check that the run_plugin_instance task was called with appropriate args
            run_job_mock.delay.assert_called_with(response.data['id'], 'PluginInstanceAppJob')
            self.assertEqual(response.data['status'], 'scheduled')

        previous_plg_inst.status = 'started'
        previous_plg_inst.save()
        with mock.patch('plugininstances.tasks.run_plugin_instance_job') as run_job_mock:
            self.client.login(username=self.username, password=self.password)
            response = self.client.post(create_read_url, data=post,
                                        content_type=self.content_type)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)

            # check that the run_plugin_instance task was not called
            run_job_mock.delay.assert_not_called()
            self.assertEqual(response.data['status'], 'waiting')

        previous_plg_inst.status = 'finishedWithError'
        previous_plg_inst.save()
        with mock.patch('plugininstances.tasks.run_plugin_instance_job') as run_job_mock:
            self.client.login(username=self.username, password=self.password)
            response = self.client.post(create_read_url, data=post,
                                        content_type=self.content_type)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)

            # check that the run_plugin_instance task was not called
            run_job_mock.delay.assert_not_called()
            self.assertEqual(response.data['status'], 'cancelled')

    def test_ts_plugin_instance_create_success(self):
        # Use existing 'fs' plugin instance
        self.pl_inst.status = 'finishedSuccessfully'
        self.pl_inst.save()

        # create a new ds plugin instance to act as a parent
        plugin_ds = Plugin.objects.get(meta__name='mri_convert')
        user = User.objects.get(username=self.username)
        (parent_plg_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin_ds, owner=user, previous=self.pl_inst,
            compute_resource=plugin_ds.compute_resources.all()[0])

        # now create and test a 'ts' plugin instance (has previous plugin instance)

        (pl_meta, tf) = PluginMeta.objects.get_or_create(name='ts_copy', type='ts')
        (plugin, tf) = Plugin.objects.get_or_create(meta=pl_meta, version='0.1')
        plugin.compute_resources.set([self.compute_resource])
        plugin.save()
        PluginParameter.objects.get_or_create(plugin=plugin, name='plugininstances', type='string',
                                              optional=True)
        create_read_url = reverse("plugininstance-list", kwargs={"pk": plugin.id})
        post = json.dumps(
            {"template": {"data": [{"name": "previous_id", "value": self.pl_inst.id},
                                   {"name": "title", "value": "test_ts"},
                                   {"name": "plugininstances",
                                    "value": ','.join([str(self.pl_inst.id),
                                                       str(parent_plg_inst.id)])}]}})

        parent_plg_inst.status = 'finishedSuccessfully'
        parent_plg_inst.save()
        with mock.patch('plugininstances.tasks.run_plugin_instance_job') as run_job_mock:
            self.client.login(username=self.username, password=self.password)
            response = self.client.post(create_read_url, data=post,
                                        content_type=self.content_type)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)

            # check that the run_plugin_instance task was called with appropriate args
            run_job_mock.delay.assert_called_with(response.data['id'], 'PluginInstanceAppJob')
            self.assertEqual(response.data['status'], 'scheduled')

        parent_plg_inst.status = 'started'
        parent_plg_inst.save()
        with mock.patch('plugininstances.tasks.run_plugin_instance_job') as run_job_mock:
            self.client.login(username=self.username, password=self.password)
            response = self.client.post(create_read_url, data=post,
                                        content_type=self.content_type)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)

            # check that the run_plugin_instance task was not called
            run_job_mock.delay.assert_not_called()
            self.assertEqual(response.data['status'], 'waiting')

        parent_plg_inst.status = 'finishedWithError'
        parent_plg_inst.save()
        with mock.patch('plugininstances.tasks.run_plugin_instance_job') as run_job_mock:
            self.client.login(username=self.username, password=self.password)
            response = self.client.post(create_read_url, data=post,
                                        content_type=self.content_type)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)

            # check that the run_plugin_instance task was not called
            run_job_mock.delay.assert_not_called()
            self.assertEqual(response.data['status'], 'cancelled')

    @tag('integration')
    @mock.patch.object(tasks, 'REGISTER_FAST_MAX_FILES', 0)
    def test_integration_plugin_instance_create_success(self):
        # with no files allowed on the register queue its registration goes on to the
        # register_large queue's task (the other integration tests register on register)
        sent_on = mock.patch.object(tasks.register_large_plugin_instance_output, 'delay',
                                    wraps=tasks.register_large_plugin_instance_output.delay)
        large = sent_on.start()
        self.addCleanup(sent_on.stop)

        # enable copy job flow for this integration test
        self.compute_resource.compute_requires_copy_job = True
        self.compute_resource.save(update_fields=['compute_requires_copy_job'])

        # add an FS plugin to the system
        plugin_parameters = [{'name': 'dir', 'type': 'path', 'action': 'store',
                              'optional': False, 'flag': '--dir', 'short_flag': '-d',
                              'help': 'test plugin', 'ui_exposed': True}]

        self.plg_data = {'description': 'A simple chris fs app demo',
                         'version': '0.1',
                         'dock_image': 'fnndsc/pl-simplefsapp',
                         'execshell': 'python3',
                         'selfpath': '/usr/local/bin',
                         'selfexec': 'simplefsapp'}

        self.plg_meta_data = {'name': 'simplefsapp',
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

        data = self.plg_meta_data.copy()
        (pl_meta, tf) = PluginMeta.objects.get_or_create(**data)
        data = self.plg_data.copy()
        (plugin, tf) = Plugin.objects.get_or_create(meta=pl_meta, **data)
        plugin.compute_resources.set([self.compute_resource])
        plugin.save()

        # add plugin's parameters
        parameters = plugin_parameters
        PluginParameter.objects.get_or_create(
            plugin=plugin,
            name=parameters[0]['name'],
            type=parameters[0]['type'],
            flag=parameters[0]['flag'])

        # upload a file to the storage user's space
        with io.StringIO('Test file') as f:
            self.storage_manager.upload_obj(self.user_space_path + 'test.txt', f.read(),
                                          content_type='text/plain')

        # make POST API request to create a plugin instance
        create_read_url = reverse("plugininstance-list", kwargs={"pk": plugin.id})
        self.client.login(username=self.username, password=self.password)
        response = self.client.post(create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        pl_inst = PluginInstance.objects.get(pk=response.data['id'])

        # share the instance's feed with another user
        user = User.objects.get(username=self.other_username)
        pl_inst.feed.grant_user_permission(user)
        self.assertTrue(pl_inst.output_folder.has_user_permission(user, 'w'))

        # the Celery worker submitted the copy job; poll status until app job starts
        copy_job = PluginInstanceCopyJob(pl_inst)
        for _ in range(20):
            time.sleep(3)
            pl_inst.refresh_from_db()
            if pl_inst.status == 'copying':
                copy_job.check_exec_status()
                pl_inst.refresh_from_db()
            if pl_inst.status == 'started':
                break
        if pl_inst.status == 'cancelled':
            self.storage_manager.delete_obj(self.user_space_path + 'test.txt')
            self.skipTest('Copy job failed to transition to started - likely pfcon Docker issue')
        self.assertEqual(pl_inst.status, 'started')

        # poll app job until it finishes or transitions to uploading
        plg_inst_app_job = PluginInstanceAppJob(pl_inst)
        maxLoopTries = 10
        currentLoop = 1
        b_checkAgain = True
        time.sleep(10)
        while b_checkAgain:
            # the registration runs in the worker's task: its status is in the database
            plg_inst_app_job.check_exec_status()
            pl_inst.refresh_from_db()
            str_responseStatus = pl_inst.status
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
            for _ in range(10):
                time.sleep(10)
                upload_job.check_exec_status()
                pl_inst.refresh_from_db()
                if pl_inst.status == 'finishedSuccessfully':
                    break

        self.assertEqual(pl_inst.status, 'finishedSuccessfully')
        large.assert_called_once_with(pl_inst.pk)

        # make sure output file was created in storage
        output_path = pl_inst.output_folder.path
        self.assertTrue(self.storage_manager.obj_exists(output_path + '/test.txt'))

        # delete files from storage
        self.storage_manager.delete_path(pl_inst.output_folder.path)
        self.storage_manager.delete_obj(self.user_space_path + 'test.txt')

    @tag('integration')
    def test_integration_plugin_instance_create_output_chris_link_success(self):

        # enable copy job flow for this integration test
        self.compute_resource.compute_requires_copy_job = True
        self.compute_resource.save(update_fields=['compute_requires_copy_job'])

        # add an FS plugin with unextpath parameter to the system
        plugin_parameters = [{'name': 'dir', 'type': 'unextpath', 'action': 'store',
                              'optional': False, 'flag': '--dir', 'short_flag': '-d',
                              'help': 'test plugin', 'ui_exposed': True}]

        plg_data = {'description': 'A simple chris dircopy app demo',
                    'version': '0.1',
                    'dock_image': 'fnndsc/pl-dircopy',
                    'execshell': 'python3',
                    'selfpath': '/usr/local/bin',
                    'selfexec': 'dircopy'}

        plg_meta_data = {'name': 'dircopy',
                         'title': 'Dir plugin',
                         'license': 'MIT',
                         'type': 'fs',
                         'icon': 'http://github.com/plugin',
                         'category': 'Dir',
                         'stars': 0,
                         'authors': 'FNNDSC (dev@babyMRI.org)'}

        plugin_repr = plg_data.copy()
        plugin_repr.update(plg_meta_data)
        plugin_repr['parameters'] = plugin_parameters

        data = plg_meta_data.copy()
        (pl_meta, tf) = PluginMeta.objects.get_or_create(**data)
        data = plg_data.copy()
        (plugin, tf) = Plugin.objects.get_or_create(meta=pl_meta, **data)
        plugin.compute_resources.set([self.compute_resource])
        plugin.save()

        # add plugin's parameters
        parameters = plugin_parameters
        PluginParameter.objects.get_or_create(
            plugin=plugin,
            name=parameters[0]['name'],
            type=parameters[0]['type'],
            flag=parameters[0]['flag'])

        # upload a file to the storage user's space
        with io.StringIO('Test file') as f:
            self.storage_manager.upload_obj(self.user_space_path + 'test.txt', f.read(),
                                            content_type='text/plain')

        # make POST API request to create a plugin instance
        create_read_url = reverse("plugininstance-list", kwargs={"pk": plugin.id})
        self.client.login(username=self.username, password=self.password)
        response = self.client.post(create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # the Celery worker submitted the copy job; poll status until app job starts
        pl_inst = PluginInstance.objects.get(pk=response.data['id'])
        copy_job = PluginInstanceCopyJob(pl_inst)
        for _ in range(20):
            time.sleep(3)
            pl_inst.refresh_from_db()
            if pl_inst.status == 'copying':
                copy_job.check_exec_status()
                pl_inst.refresh_from_db()
            if pl_inst.status == 'started':
                break
        if pl_inst.status == 'cancelled':
            self.storage_manager.delete_obj(self.user_space_path + 'test.txt')
            self.skipTest('Copy job failed to transition to started - likely pfcon Docker issue')
        self.assertEqual(pl_inst.status, 'started')

        # poll app job until it finishes or transitions to uploading
        plg_inst_app_job = PluginInstanceAppJob(pl_inst)
        maxLoopTries = 10
        currentLoop = 1
        b_checkAgain = True
        time.sleep(10)
        while b_checkAgain:
            # the registration runs in the worker's task: its status is in the database
            plg_inst_app_job.check_exec_status()
            pl_inst.refresh_from_db()
            str_responseStatus = pl_inst.status
            if str_responseStatus in ('finishedSuccessfully', 'uploading'):
                b_checkAgain = False
            elif currentLoop < maxLoopTries:
                time.sleep(10)
            if currentLoop == maxLoopTries:
                b_checkAgain = False
            currentLoop += 1

        if pl_inst.status == 'uploading':
            upload_job = PluginInstanceUploadJob(pl_inst)
            for _ in range(10):
                time.sleep(10)
                upload_job.check_exec_status()
                pl_inst.refresh_from_db()
                if pl_inst.status == 'finishedSuccessfully':
                    break

        self.assertEqual(pl_inst.status, 'finishedSuccessfully')

        # make sure output ChRIS link file was created in storage
        link_file = pl_inst.output_folder.chris_link_files.first()
        self.assertTrue(self.storage_manager.obj_exists(link_file.fname.name))

        # delete files from storage
        self.storage_manager.delete_path(pl_inst.output_folder.path)
        self.storage_manager.delete_obj(self.user_space_path + 'test.txt')

    @tag('integration')
    def test_integration_ts_plugin_instance_create_success(self):
        # enable copy job flow for this integration test
        self.compute_resource.compute_requires_copy_job = True
        self.compute_resource.save(update_fields=['compute_requires_copy_job'])

        # upload a file to the storage user's space
        with io.StringIO('Test file') as f:
            self.storage_manager.upload_obj(self.user_space_path + 'test.txt', f.read(),
                                            content_type='text/plain')
        # create an FS plugin instance
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name="pacspull")
        (fs_plg_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, compute_resource=plugin.compute_resources.all()[0])

        # upload FS plugin instace output link file to storage
        str_source_trace_dir = self.user_space_path.rstrip('/').replace('/', '_')
        path = os.path.join(fs_plg_inst.get_output_path(), f'{str_source_trace_dir}.chrislink')

        with io.StringIO(self.user_space_path.rstrip('/')) as test_link_file:
            self.storage_manager.upload_obj(path, test_link_file.read(),
                                          content_type='text/plain')

        folder_path = os.path.dirname(path)
        (file_parent_folder, _) = ChrisFolder.objects.get_or_create(path=folder_path,
                                                                    owner=user)
        fs_plg_inst_file = UserFile(owner=user, parent_folder=file_parent_folder)
        fs_plg_inst_file.fname.name = path
        fs_plg_inst_file.save()
        fs_plg_inst.status = 'finishedSuccessfully'
        fs_plg_inst.save()

        # add a TS plugin to the system
        plugin_parameters = [{'name': 'plugininstances', 'type': 'string',
                              'action': 'store', 'optional': True,
                              'flag': '--plugininstances', 'short_flag': '--plugininstances',
                              'help': 'test plugin parameter', 'ui_exposed': True},

                             {'name': 'filter', 'type': 'string',
                              'action': 'store', 'optional': True,
                              'flag': '--filter',
                              'short_flag': '-f',
                              'help': 'test plugin parameter', 'ui_exposed': True}
                             ]
        self.plg_data = {'description': 'A toplological copy ts plugin',
                         'version': '0.1',
                         'dock_image': 'fnndsc/pl-topologicalcopy',
                         'execshell': 'python3',
                         'selfpath': '/usr/local/bin',
                         'selfexec': 'topologicalcopy'}

        self.plg_meta_data = {'name': 'topologicalcopy',
                              'title': 'TS copy plugin',
                              'license': 'MIT',
                              'type': 'ts',
                              'icon': 'http://github.com/plugin',
                              'category': 'Utility',
                              'stars': 0,
                              'authors': 'FNNDSC (dev@babyMRI.org)'}

        self.plugin_repr = self.plg_data.copy()
        self.plugin_repr.update(self.plg_meta_data)
        self.plugin_repr['parameters'] = plugin_parameters

        (compute_resource, tf) = ComputeResource.objects.get_or_create(
            name="host", compute_url=COMPUTE_RESOURCE_URL, compute_user=self.compute_user,
            compute_password=self.compute_password)

        data = self.plg_meta_data.copy()
        (pl_meta, tf) = PluginMeta.objects.get_or_create(**data)
        data = self.plg_data.copy()
        (plugin, tf) = Plugin.objects.get_or_create(meta=pl_meta, **data)
        plugin.compute_resources.set([compute_resource])
        plugin.save()

        # add plugin's parameters
        parameters = plugin_parameters
        PluginParameter.objects.get_or_create(
            plugin=plugin,
            name=parameters[0]['name'],
            type=parameters[0]['type'],
            flag=parameters[0]['flag'])

        # make POST API request to create a ts plugin instance
        create_read_url = reverse("plugininstance-list", kwargs={"pk": plugin.id})
        post = json.dumps(
            {"template": {"data": [{"name": "previous_id", "value": fs_plg_inst.id},
                                   {"name": "plugininstances", "value": str(fs_plg_inst.id)},
                                   {"name": "filter", "value": ".txt$"}]}})
        self.client.login(username=self.username, password=self.password)
        response = self.client.post(create_read_url, data=post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # the Celery worker submitted the copy job; poll status until app job starts
        pl_inst = PluginInstance.objects.get(pk=response.data['id'])
        copy_job = PluginInstanceCopyJob(pl_inst)
        for _ in range(20):
            time.sleep(3)
            pl_inst.refresh_from_db()
            if pl_inst.status == 'copying':
                copy_job.check_exec_status()
                pl_inst.refresh_from_db()
            if pl_inst.status == 'started':
                break
        if pl_inst.status == 'cancelled':
            self.storage_manager.delete_obj(self.user_space_path + 'test.txt')
            self.skipTest('Copy job failed to transition to started - likely pfcon Docker issue')
        self.assertEqual(pl_inst.status, 'started')

        # poll app job until it finishes or transitions to uploading
        plg_inst_app_job = PluginInstanceAppJob(pl_inst)
        maxLoopTries = 10
        currentLoop = 1
        b_checkAgain = True
        time.sleep(10)
        while b_checkAgain:
            # the registration runs in the worker's task: its status is in the database
            plg_inst_app_job.check_exec_status()
            pl_inst.refresh_from_db()
            str_responseStatus = pl_inst.status
            if str_responseStatus in ('finishedSuccessfully', 'uploading'):
                b_checkAgain = False
            elif currentLoop < maxLoopTries:
                time.sleep(10)
            if currentLoop == maxLoopTries:
                b_checkAgain = False
            currentLoop += 1

        if pl_inst.status == 'uploading':
            upload_job = PluginInstanceUploadJob(pl_inst)
            for _ in range(10):
                time.sleep(10)
                upload_job.check_exec_status()
                pl_inst.refresh_from_db()
                if pl_inst.status == 'finishedSuccessfully':
                    break

        self.assertEqual(pl_inst.status, 'finishedSuccessfully')
        output_user_files = UserFile.objects.filter(
            fname__startswith=pl_inst.output_folder.path)
        self.assertEqual(output_user_files.count(), 1)

        # make sure output ChRIS link file was created in storage
        link_file = pl_inst.output_folder.chris_link_files.first()
        self.assertTrue(self.storage_manager.obj_exists(link_file.fname.name))

        # delete files from storage
        self.storage_manager.delete_path(pl_inst.output_folder.path)
        self.storage_manager.delete_obj(path)
        self.storage_manager.delete_obj(self.user_space_path + 'test.txt')

    def test_plugin_instance_create_failure_unauthenticated(self):
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_plugin_instance_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.create_read_url)
        self.assertContains(response, "pacspull")

    def test_plugin_instance_list_query_count_does_not_grow_with_instances(self):
        # the related objects the serializer reads for every instance are fetched in
        # the list query itself; the new instances set every relation
        self.client.login(username=self.username, password=self.password)
        user = User.objects.get(username=self.username)
        workflow = create_workflow(user)
        plugin = Plugin.objects.get(meta__name="mri_convert")
        list_url = reverse("plugininstance-list", kwargs={"pk": plugin.id})
        self.assertQueriesPerListItem(
            list_url, lambda: create_ds_instances(user, self.pl_inst, 3, workflow))

    def test_plugin_instance_list_failure_unauthenticated(self):
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class PluginInstanceDetailViewTests(TasksViewTests):
    """
    Test the plugininstance-detail view.
    """

    def setUp(self):
        super(PluginInstanceDetailViewTests, self).setUp()

        self.read_update_delete_url = reverse("plugininstance-detail",
                                              kwargs={"pk": self.pl_inst.id})

    def test_plugin_instance_detail_success(self):
        self.pl_inst.status = 'started'
        self.pl_inst.save()
        # make API request
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_update_delete_url)
        self.assertContains(response, "pacspull")
        self.assertEqual(response.data['status'], 'started')
        # with mock.patch.object(views.check_plugin_instance_exec_status, 'delay',
        #                        return_value=None) as delay_mock:
        #     # make API request
        #     self.client.login(username=self.username, password=self.password)
        #     response = self.client.get(self.read_update_delete_url)
        #     self.assertContains(response, "pacspull")
        #     self.assertEqual(response.data['status'], 'started')
        #     # check that the check_plugin_instance_exec_status task was called with appropriate args
        #     delay_mock.assert_called_with(self.pl_inst.id)

    def test_plugin_instance_detail_fetches_related_objects_with_the_instance(self):
        user = User.objects.get(username=self.username)
        (plg_inst,) = create_ds_instances(user, self.pl_inst, 1, create_workflow(user))
        self.client.login(username=self.username, password=self.password)
        # two queries authenticate the session and one fetches the instance with the
        # related objects the serializer and the permission checks read
        with self.assertNumQueries(3):
            self.client.get(reverse("plugininstance-detail", kwargs={"pk": plg_inst.id}))

    @tag('integration')
    def test_integration_plugin_instance_detail_success(self):

        # enable copy job flow for this integration test
        self.compute_resource.compute_requires_copy_job = True
        self.compute_resource.save(update_fields=['compute_requires_copy_job'])

        # add an FS plugin to the system
        plugin_parameters = [{'name': 'dir', 'type': 'path', 'action': 'store',
                              'optional': False, 'flag': '--dir', 'short_flag': '-d',
                              'help': 'test plugin', 'ui_exposed': True}]

        self.plg_data = {'description': 'A simple chris fs app demo',
                         'version': '0.1',
                         'dock_image': 'fnndsc/pl-simplefsapp',
                         'execshell': 'python3',
                         'selfpath': '/usr/local/bin',
                         'selfexec': 'simplefsapp'}

        self.plg_meta_data = {'name': 'simplefsapp',
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

        (compute_resource, tf) = ComputeResource.objects.get_or_create(
            name="host", compute_url=COMPUTE_RESOURCE_URL, compute_user=self.compute_user,
            compute_password=self.compute_password)

        data = self.plg_meta_data.copy()
        (pl_meta, tf) = PluginMeta.objects.get_or_create(**data)
        data = self.plg_data.copy()
        (plugin, tf) = Plugin.objects.get_or_create(meta=pl_meta, **data)
        plugin.compute_resources.set([compute_resource])
        plugin.save()

        # add plugin's parameters
        parameters = plugin_parameters
        (pl_param, tf) = PluginParameter.objects.get_or_create(
            plugin=plugin,
            name=parameters[0]['name'],
            type=parameters[0]['type'],
            flag=parameters[0]['flag'])

        # upload a file to the storage user's space
        user_space_path = f'home/{self.username}/uploads/'
        with io.StringIO('Test file') as f:
            self.storage_manager.upload_obj(user_space_path + 'test.txt', f.read(),
                                          content_type='text/plain')

        # create a plugin's instance in 'copying' status
        user = User.objects.get(username=self.username)
        (pl_inst, tf) = PluginInstance.objects.get_or_create(
            title='test2', plugin=plugin, owner=user, status='copying',
            compute_resource=plugin.compute_resources.all()[0])
        PathParameter.objects.get_or_create(plugin_inst=pl_inst, plugin_param=pl_param,
                                            value=user_space_path)
        read_update_delete_url = reverse("plugininstance-detail",
                                         kwargs={"pk": pl_inst.id})

        # run the copy job then poll until the app job starts
        copy_job = PluginInstanceCopyJob(pl_inst)
        copy_job.run()
        for _ in range(20):
            time.sleep(3)
            pl_inst.refresh_from_db()
            if pl_inst.status == 'copying':
                copy_job.check_exec_status()
                pl_inst.refresh_from_db()
            if pl_inst.status == 'started':
                break
        if pl_inst.status == 'cancelled':
            self.storage_manager.delete_obj(user_space_path + 'test.txt')
            self.skipTest('Copy job failed to transition to started - likely pfcon Docker issue')
        self.assertEqual(pl_inst.status, 'started')

        # poll app job until it finishes or transitions to uploading; also check view
        self.client.login(username=self.username, password=self.password)
        plg_inst_app_job = PluginInstanceAppJob(pl_inst)
        maxLoopTries = 10
        currentLoop = 1
        b_checkAgain = True
        time.sleep(10)
        while b_checkAgain:
            plg_inst_app_job.check_exec_status()
            response = self.client.get(read_update_delete_url)
            str_responseStatus = response.data['status']
            if str_responseStatus in ('finishedSuccessfully', 'uploading'):
                b_checkAgain = False
            elif currentLoop < maxLoopTries:
                time.sleep(10)
            if currentLoop == maxLoopTries:
                b_checkAgain = False
            currentLoop += 1

        if pl_inst.status == 'uploading':
            upload_job = PluginInstanceUploadJob(pl_inst)
            for _ in range(10):
                time.sleep(10)
                upload_job.check_exec_status()
                pl_inst.refresh_from_db()
                if pl_inst.status == 'finishedSuccessfully':
                    break
            response = self.client.get(read_update_delete_url)

        self.assertContains(response, "finishedSuccessfully")
        self.assertContains(response, "simplefsapp")

        # delete files from storage
        self.storage_manager.delete_path(pl_inst.output_folder.path)
        self.storage_manager.delete_obj(user_space_path + 'test.txt')

    def test_plugin_instance_detail_failure_unauthenticated(self):
        response = self.client.get(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_plugin_instance_update_success(self):
        put = json.dumps({
            "template": {"data": [{"name": "title", "value": "Test instance"},
                                  {"name": "status", "value": "cancelled"}]}})

        self.client.login(username=self.username, password=self.password)
        response = self.client.put(self.read_update_delete_url, data=put,
                                   content_type=self.content_type)
        self.assertContains(response, "Test instance")
        self.assertContains(response, "cancelled")
        self.pl_inst.refresh_from_db()
        self.assertEqual((self.pl_inst.title, self.pl_inst.status),
                         ('Test instance', 'cancelled'))

    def test_plugin_instance_update_of_the_title_writes_nothing_else(self):
        put = json.dumps({
            "template": {"data": [{"name": "title", "value": "Test instance"}]}})
        self.client.login(username=self.username, password=self.password)
        original = views.PluginInstanceDetail.perform_update

        def moved_on_meanwhile(view, serializer):
            # a task moves the instance on after the request read it
            PluginInstance.objects.filter(pk=self.pl_inst.pk).update(status='started')
            original(view, serializer)

        with mock.patch.object(views.PluginInstanceDetail, 'perform_update',
                               moved_on_meanwhile):
            response = self.client.put(self.read_update_delete_url, data=put,
                                       content_type=self.content_type)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.pl_inst.refresh_from_db()
        self.assertEqual((self.pl_inst.title, self.pl_inst.status),
                         ('Test instance', 'started'))

    def test_plugin_instance_cancel_cancels_every_descendant_and_their_remote_jobs(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name="mri_convert")
        children = {}
        for status_ in ('waiting', 'started', 'finishedSuccessfully', 'cancelled'):
            (child, tf) = PluginInstance.objects.get_or_create(
                plugin=plugin, owner=user, previous=self.pl_inst, title=status_,
                compute_resource=plugin.compute_resources.all()[0])
            PluginInstance.objects.filter(pk=child.pk).update(status=status_)
            children[status_] = child
        PluginInstance.objects.filter(pk=self.pl_inst.pk).update(status='started')
        put = json.dumps({"template": {"data": [{"name": "status", "value": "cancelled"}]}})

        self.client.login(username=self.username, password=self.password)
        response = self.client.put(self.read_update_delete_url, data=put,
                                   content_type=self.content_type)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = dict(PluginInstance.objects.filter(
            pk__in=[self.pl_inst.pk] + [c.pk for c in children.values()]
        ).values_list('pk', 'status'))
        self.assertEqual(set(rows.values()), {'cancelled'})
        # the remote cleanup armed in the cancel's UPDATE for each that had a job
        armed = set(PluginInstance.objects.filter(
            pk__in=rows, remote_cleanup_status='deletingData').values_list('pk', flat=True))
        self.assertEqual(armed, {self.pl_inst.pk, children['started'].pk})

    def test_plugin_instance_cancel_arms_the_cleanup_of_a_job_submitted_since_the_read(self):
        PluginInstance.objects.filter(pk=self.pl_inst.pk).update(status='waiting')
        put = json.dumps({"template": {"data": [{"name": "status", "value": "cancelled"}]}})
        original = views.PluginInstanceDetail.perform_update

        def copying_meanwhile(view, serializer):
            # the request read 'waiting'; a task then submitted the copy job
            PluginInstance.objects.filter(pk=self.pl_inst.pk).update(status='copying')
            original(view, serializer)

        self.client.login(username=self.username, password=self.password)
        with mock.patch.object(views.PluginInstanceDetail, 'perform_update',
                               copying_meanwhile):
            response = self.client.put(self.read_update_delete_url, data=put,
                                       content_type=self.content_type)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.pl_inst.refresh_from_db()
        self.assertEqual((self.pl_inst.status, self.pl_inst.remote_cleanup_status),
                         ('cancelled', 'deletingData'))

    def test_plugin_instance_delete_of_a_root_that_finished_meanwhile_cancels_its_children(
            self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name="mri_convert")
        compute_resource = plugin.compute_resources.all()[0]
        root = PluginInstance.objects.create(plugin=plugin, owner=user,
                                             previous=self.pl_inst, title='root',
                                             compute_resource=compute_resource)
        child = PluginInstance.objects.create(plugin=plugin, owner=user, previous=root,
                                              title='child',
                                              compute_resource=compute_resource)
        PluginInstance.objects.filter(pk=child.pk).update(status='waiting')
        PluginInstance.objects.filter(pk=root.pk).update(status='started')
        original = views.cancel_plugin_instance_tree

        def finished_meanwhile(inst):
            PluginInstance.objects.filter(pk=inst.pk).update(status='finishedSuccessfully')
            return original(inst)

        self.client.login(username=self.username, password=self.password)
        with mock.patch.object(views, 'cancel_plugin_instance_tree', finished_meanwhile), \
                mock.patch.object(views.delete_plugin_instance, 'delay'):
            response = self.client.delete(reverse("plugininstance-detail",
                                                  kwargs={"pk": root.pk}))

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(PluginInstance.objects.get(pk=child.pk).status, 'cancelled')
        # the root keeps the status it ended with
        self.assertEqual(PluginInstance.objects.get(pk=root.pk).status,
                         'finishedSuccessfully')

    def test_plugin_instance_cancel_of_a_cancelled_instance_cancels_its_descendants(self):
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name="mri_convert")
        (child, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, previous=self.pl_inst, title='child',
            compute_resource=plugin.compute_resources.all()[0])
        PluginInstance.objects.filter(pk=child.pk).update(status='started')
        PluginInstance.objects.filter(pk=self.pl_inst.pk).update(status='cancelled')
        put = json.dumps({"template": {"data": [{"name": "status", "value": "cancelled"}]}})

        self.client.login(username=self.username, password=self.password)
        response = self.client.put(self.read_update_delete_url, data=put,
                                   content_type=self.content_type)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'cancelled')
        child.refresh_from_db()
        self.assertEqual((child.status, child.remote_cleanup_status),
                         ('cancelled', 'deletingData'))

    def test_plugin_instance_update_ignores_the_fields_it_does_not_change(self):
        put = json.dumps({"template": {"data": [
            {"name": "title", "value": "renamed"},
            {"name": "compute_resource_name", "value": "elsewhere"},
            {"name": "deletion_status", "value": "pending"},
            {"name": "deletion_error", "value": "forged"}]}})

        self.client.login(username=self.username, password=self.password)
        response = self.client.put(self.read_update_delete_url, data=put,
                                   content_type=self.content_type)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.pl_inst.refresh_from_db()
        self.assertEqual((self.pl_inst.title, self.pl_inst.compute_resource.name,
                          self.pl_inst.deletion_status, self.pl_inst.deletion_error),
                         ('renamed', 'host', 'inactive', ''))

    def test_plugin_instance_cancel_of_an_instance_that_finished_meanwhile_is_refused(self):
        put = json.dumps({"template": {"data": [{"name": "status", "value": "cancelled"}]}})
        self.client.login(username=self.username, password=self.password)
        original = views.PluginInstanceDetail.perform_update

        def finished_meanwhile(view, serializer):
            PluginInstance.objects.filter(pk=self.pl_inst.pk).update(
                status='finishedSuccessfully')
            original(view, serializer)

        with mock.patch.object(views.PluginInstanceDetail, 'perform_update',
                               finished_meanwhile):
            response = self.client.put(self.read_update_delete_url, data=put,
                                       content_type=self.content_type)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.pl_inst.refresh_from_db()
        self.assertEqual(self.pl_inst.status, 'finishedSuccessfully')

    def test_plugin_instance_update_failure_current_status_is_finishedSuccessfully_or_finishedWithError(self):
        put = json.dumps({
            "template": {"data": [{"name": "status", "value": "cancelled"}]}})

        self.pl_inst.status = 'finishedSuccessfully'
        self.pl_inst.save()
        self.client.login(username=self.username, password=self.password)
        response = self.client.put(self.read_update_delete_url, data=put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.pl_inst.status = 'finishedWithError'
        self.pl_inst.save()
        self.client.login(username=self.username, password=self.password)
        response = self.client.put(self.read_update_delete_url, data=put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_plugin_instance_update_failure_status_can_only_be_changed_to_cancelled(self):
        put = json.dumps({
            "template": {"data": [{"name": "status", "value": "finishedSuccessfully"}]}})

        self.client.login(username=self.username, password=self.password)
        response = self.client.put(self.read_update_delete_url, data=put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_plugin_instance_update_failure_unauthenticated(self):
        put = json.dumps({
            "template": {"data": [{"name": "title", "value": "Test instance"}]}})
        response = self.client.put(self.read_update_delete_url, data=put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_plugin_instance_update_failure_access_denied(self):
        put = json.dumps({
            "template": {"data": [{"name": "status", "value": "cancelled"}]}})

        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.put(self.read_update_delete_url, data=put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_plugin_instance_delete_success(self):
        user = User.objects.get(username=self.username)

        # create two plugin instances
        plugin = Plugin.objects.get(meta__name="pacspull")
        (fs_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, compute_resource=plugin.compute_resources.all()[0])

        plugin = Plugin.objects.get(meta__name="mri_convert")
        (ds_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, previous=fs_inst,
            compute_resource=plugin.compute_resources.all()[0])

        url = reverse("plugininstance-detail", kwargs={"pk": ds_inst.id})

        self.client.login(username=self.username, password=self.password)

        with mock.patch.object(views.delete_plugin_instance, 'delay',
                               return_value=None) as delay_mock:
            response = self.client.delete(url)
            self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

            # check that the delete_plugin_instance task was called with appropriate args
            delay_mock.assert_called_with(ds_inst.id)

    @tag('integration')
    def test_integration_plugin_instance_delete_success(self):
        user = User.objects.get(username=self.username)

        # create two plugin instances
        plugin = Plugin.objects.get(meta__name="pacspull")
        (fs_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, compute_resource=plugin.compute_resources.all()[0])

        plugin = Plugin.objects.get(meta__name="mri_convert")
        (ds_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, previous=fs_inst,
            compute_resource=plugin.compute_resources.all()[0])

        url = reverse("plugininstance-detail", kwargs={"pk": ds_inst.id})
        inst_count = PluginInstance.objects.count()

        self.client.login(username=self.username, password=self.password)
        response = self.client.delete(url)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        for _ in range(10):
            time.sleep(3)
            if PluginInstance.objects.count() == inst_count - 1: break
        self.assertEqual(PluginInstance.objects.count(), inst_count-1)

    def test_plugin_instance_delete_failure_unauthenticated(self):
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_plugin_instance_delete_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_plugin_instance_delete_failure_fs_plugin(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class PluginInstanceListQuerySearchViewTests(ViewTests):
    """
    Test the allplugininstance-list-query-search view.
    """

    def setUp(self):
        super(PluginInstanceListQuerySearchViewTests, self).setUp()

        user = User.objects.get(username=self.username)

        (pl_meta, tf) = PluginMeta.objects.get_or_create(name='pacsquery', type='fs')
        (plugin_fs, tf) = Plugin.objects.get_or_create(meta=pl_meta, version='0.1')
        plugin_fs.compute_resources.set([self.compute_resource])
        plugin_fs.save()

        # create three plugin instances
        PluginInstance.objects.get_or_create(
            plugin=plugin_fs, owner=user, compute_resource=plugin_fs.compute_resources.all()[0])

        plugin = Plugin.objects.get(meta__name="pacspull")
        (inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, compute_resource=plugin.compute_resources.all()[0])

        inst.status = 'finishedSuccessfully'  # set second instance's status
        inst.save()

        plugin = Plugin.objects.get(meta__name="mri_convert")
        (inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, previous=inst,
            compute_resource=plugin.compute_resources.all()[0])

        inst.status = 'finishedSuccessfully'  # set third instance's status
        inst.save()

        inst.feed.public = True  # second and third instances now belongs to a public feed
        inst.feed.save()

        self.list_url = reverse("allplugininstance-list-query-search") + '?status=created'

    def test_plugin_instance_query_search_list_success(self):
        self.client.login(username=self.username, password=self.password)

        response = self.client.get(self.list_url)
        # response should only contain the instances that match the query
        self.assertContains(response, 'created')
        self.assertEqual(len(response.data['results']), 1)
        self.assertNotContains(response,'finishedSuccessfully')

    def test_plugin_instance_query_search_list_success_active(self):
        self.client.login(username=self.username, password=self.password)

        list_url = reverse("allplugininstance-list-query-search") + '?active=true'
        response = self.client.get(list_url)
        # response should only contain the instances that are active
        self.assertContains(response, 'created')
        self.assertEqual(len(response.data['results']), 1)
        self.assertNotContains(response,'finishedSuccessfully')

        list_url = reverse("allplugininstance-list-query-search") + '?active=false'
        response = self.client.get(list_url)
        # response should only contain the instances that are not active
        self.assertContains(response, 'finishedSuccessfully')
        self.assertEqual(len(response.data['results']), 2)
        self.assertNotContains(response, 'created')

    def test_all_plugin_instance_list_query_count_does_not_grow_with_instances(self):
        # the related objects the serializer reads for every instance are fetched in
        # the list query itself; the new instances set every relation, the workflow
        # and its pipeline among them
        self.client.login(username=self.username, password=self.password)
        user = User.objects.get(username=self.username)
        previous = PluginInstance.objects.get(plugin__meta__name="pacspull")
        workflow = create_workflow(user)
        self.assertQueriesPerListItem(
            reverse("allplugininstance-list"),
            lambda: create_ds_instances(user, previous, 3, workflow))

    def test_plugin_instance_query_search_query_count_does_not_grow_with_instances(self):
        # searching by root_id walks the tree under the root instance, which costs one
        # query per instance walked but none to serialize it
        self.client.login(username=self.username, password=self.password)
        user = User.objects.get(username=self.username)
        previous = PluginInstance.objects.get(plugin__meta__name="pacspull")
        workflow = create_workflow(user)
        search_url = reverse("allplugininstance-list-query-search")

        for query, queries_per_item in (('?status=created', 0),
                                        (f'?previous_id={previous.id}', 0),
                                        (f'?root_id={previous.id}', 1)):
            with self.subTest(query=query):
                self.assertQueriesPerListItem(
                    search_url + query + '&limit=20',
                    lambda: create_ds_instances(user, previous, 3, workflow),
                    queries_per_item)

    def test_plugin_instance_query_search_list_success_but_empty_unauthenticated(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['results'], [])

    def test_plugin_instance_query_search_list_success_unauthenticated(self):
        list_url = reverse("allplugininstance-list-query-search") + '?status=finishedSuccessfully'
        response = self.client.get(list_url)
        # response should only contain the instances that match the query
        self.assertContains(response, 'finishedSuccessfully')
        self.assertEqual(len(response.data['results']), 2)
        self.assertNotContains(response, 'created')


class PluginInstanceDescendantListViewTests(ViewTests):
    """
    Test the plugininstance-descendant-list view.
    """

    def setUp(self):
        super(PluginInstanceDescendantListViewTests, self).setUp()

        user = User.objects.get(username=self.username)

        # create an 'fs' plugin instance
        plugin = Plugin.objects.get(meta__name="pacspull")
        (fs_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, compute_resource=plugin.compute_resources.all()[0])

        # create a tree of 'ds' plugin instances
        plugin = Plugin.objects.get(meta__name="mri_convert")
        PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, previous=fs_inst,
            compute_resource=plugin.compute_resources.all()[0])

        (pl_meta, tf) = PluginMeta.objects.get_or_create(name='mri_info', type='ds')
        (plugin, tf) = Plugin.objects.get_or_create(meta=pl_meta, version='0.1')
        plugin.compute_resources.set([self.compute_resource])
        plugin.save()
        (ds_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, previous=fs_inst,
            compute_resource=plugin.compute_resources.all()[0])

        (pl_meta, tf) = PluginMeta.objects.get_or_create(name='mri_surf', type='ds')
        (plugin, tf) = Plugin.objects.get_or_create(meta=pl_meta, version='0.1')
        plugin.compute_resources.set([self.compute_resource])
        plugin.save()
        PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, previous=ds_inst,
            compute_resource=plugin.compute_resources.all()[0])

        self.list_url = reverse("plugininstance-descendant-list", kwargs={"pk": fs_inst.id})

    def test_plugin_instance_descendant_list_query_count_grows_only_with_the_walk(self):
        # the walk that finds the descendants costs one query per instance, but the
        # related objects the serializer reads are fetched once for the whole page; an
        # instance in a workflow puts every relation in the page before more are added
        self.client.login(username=self.username, password=self.password)
        user = User.objects.get(username=self.username)
        root = PluginInstance.objects.get(plugin__meta__name="pacspull")
        workflow = create_workflow(user)
        create_ds_instances(user, root, 1, workflow)
        self.assertQueriesPerListItem(
            self.list_url, lambda: create_ds_instances(user, root, 3, workflow),
            queries_per_item=1)

    def test_plugin_instance_descendant_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.list_url)
        # response should contain all the instances in the tree
        self.assertContains(response, "pacspull")
        self.assertContains(response, "mri_convert")
        self.assertContains(response, "mri_info")
        self.assertContains(response, "mri_surf")

    def test_plugin_instance_descendant_list_failure_unauthenticated(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class PluginInstanceSplitListViewTests(ViewTests):
    """
    Test the plugininstancesplit-list view.
    """

    def setUp(self):
        super(PluginInstanceSplitListViewTests, self).setUp()

        user = User.objects.get(username=self.username)

        # create an 'fs' plugin instance
        plugin = Plugin.objects.get(meta__name="pacspull")
        (self.fs_inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, compute_resource=plugin.compute_resources.all()[0])

        # create a 'ts' plugin
        (pl_meta, tf) = PluginMeta.objects.get_or_create(name='pl-topologicalcopy', type='ts')
        (plugin_ts, tf) = Plugin.objects.get_or_create(meta=pl_meta, version='0.1')
        plugin_ts.compute_resources.set([self.compute_resource])
        plugin_ts.save()

        self.create_read_url = reverse("plugininstancesplit-list", kwargs={"pk": self.fs_inst.id})

    def test_plugin_instance_split_create_failure_access_denied(self):
        post = json.dumps({"template": {"data": [{"name": "filter", "value": ""}]}})
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.post(self.create_read_url, data=post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_plugin_instance_split_create_success(self):
        post = json.dumps({"template": {"data": [{"name": "filter", "value": ""}]}})

        # add parameters to the plugin before the POST request
        plugin = Plugin.objects.get(meta__name="pl-topologicalcopy")
        PluginParameter.objects.get_or_create(plugin=plugin, name='filter', type='string')
        PluginParameter.objects.get_or_create(plugin=plugin, name='plugininstances',
                                              type='string')

        self.client.login(username=self.username, password=self.password)

        # make API requests
        response = self.client.post(self.create_read_url, data=post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        self.fs_inst.status = 'finishedSuccessfully'
        self.fs_inst.save()
        with mock.patch('plugininstances.tasks.run_plugin_instance_job') as run_job_mock:

            response = self.client.post(self.create_read_url, data=post,
                                        content_type=self.content_type)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            # check that the run_plugin_instance task was called with appropriate args
            run_job_mock.delay.assert_called_once()

    def test_plugin_instance_split_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.create_read_url)
        # response should contain all the instances in the tree
        self.assertContains(response, "filter")

    def test_plugin_instance_split_list_failure_unauthenticated(self):
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class PluginInstanceParameterListViewTests(ViewTests):
    """
    Test the plugininstance-parameter-list view.
    """

    def setUp(self):
        super(PluginInstanceParameterListViewTests, self).setUp()

        user = User.objects.get(username=self.username)

        # create a plugin
        plugin = Plugin.objects.get(meta__name="pacspull")
        parameters = [{"type": "path", "name": "param1", "flag": "--param1"},
                      {"type": "float", "name": "param2", "flag": "--param2"}]

        # add plugin's parameters
        (param1, tf) = PluginParameter.objects.get_or_create(
            plugin=plugin,
            name=parameters[0]['name'],
            type=parameters[0]['type'],
            flag=parameters[0]['flag'])
        (param2, tf) = PluginParameter.objects.get_or_create(
            plugin=plugin,
            name=parameters[1]['name'],
            type=parameters[1]['type'],
            flag=parameters[1]['flag'])

        # create a plugin instance
        (inst, tf) = PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, compute_resource=plugin.compute_resources.all()[0])

        # create two plugin parameter instances associated to the plugin instance
        PathParameter.objects.get_or_create(plugin_inst=inst, plugin_param=param1,
                                            value=self.username)
        FloatParameter.objects.get_or_create(plugin_inst=inst, plugin_param=param2,
                                             value=3.14)

        self.list_url = reverse("plugininstance-parameter-list", kwargs={"pk": inst.id})

    def test_plugin_instance_parameter_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.list_url)
        self.assertContains(response, "param1")
        self.assertContains(response, self.username)
        self.assertContains(response, "param2")
        self.assertContains(response, 3.14)

    def test_plugin_instance_parameter_list_failure_unauthenticated(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
