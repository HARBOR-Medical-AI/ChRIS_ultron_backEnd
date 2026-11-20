
import logging
import io
import os
import json
import time
from unittest import mock

from django.test import TestCase,TransactionTestCase, tag
from django.conf import settings
from django.contrib.auth.models import User, Group
from django.urls import reverse
from rest_framework import status

from celery.contrib.testing.worker import start_worker
from core.celery import app as celery_app

from core.models import (ChrisFolder, ChrisFile, ChrisLinkFile, FolderGroupPermission,
                         FolderUserPermission, FileGroupPermission, FileUserPermission,
                         LinkFileGroupPermission, LinkFileUserPermission)
from core.storage import connect_storage
from users.models import UserProxy
from userfiles.models import UserFile
from plugins.models import PluginMeta, Plugin, ComputeResource
from plugininstances.models import PluginInstance
from filebrowser import views


COMPUTE_RESOURCE_URL = settings.COMPUTE_RESOURCE_URL
CHRIS_SUPERUSER_PASSWORD = settings.CHRIS_SUPERUSER_PASSWORD


class FileBrowserViewTests(TestCase):
    """
    Generic filebrowser view tests' setup and tearDown.
    """
    content_type = 'application/vnd.collection+json'

    # superuser chris (owner of root and top-level folders)
    chris_username = 'chris'
    chris_password = CHRIS_SUPERUSER_PASSWORD

    # normal users
    username = 'foo'
    password = 'foopass'
    other_username = 'booo'
    other_password = 'booopass'

    @classmethod
    def setUpClass(cls):

        # avoid cluttered console output (for instance logging all the http requests)
        logging.disable(logging.WARNING)

        # create users with their home folders setup
        UserProxy.objects.create_user(username=cls.username, password=cls.password)
        UserProxy.objects.create_user(username=cls.other_username,
                                      password=cls.other_password)

    @classmethod
    def tearDownClass(cls):
        storage_manager = connect_storage(settings)

        for username in [cls.username, cls.other_username]:
            User.objects.get(username=username).delete()
            home = f'home/{username}'
            if storage_manager.path_exists(home):
                storage_manager.delete_path(home)

        # re-enable logging
        logging.disable(logging.NOTSET)


class FileBrowserTasksViewTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.WARNING)
        super().setUpClass()
        # the test runner routes every task to the default 'celery' queue that only
        # the tests use (core/testrunner.py)
        cls.celery_worker = start_worker(celery_app,
                                         concurrency=1,
                                         perform_ping_check=False)
        cls.celery_worker.__enter__()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.celery_worker.__exit__(None, None, None)
        logging.disable(logging.NOTSET)

    def setUp(self):
        super(FileBrowserTasksViewTests, self).setUp()
        self.content_type = 'application/vnd.collection+json'

        # superuser chris (owner of root and top-level folders)
        self.chris_username = 'chris'
        self.chris_password = CHRIS_SUPERUSER_PASSWORD

        # normal users
        self.username = 'foo'
        self.password = 'foopass'
        self.other_username = 'booo'
        self.other_password = 'booopass'

        # create users with their home folders setup
        UserProxy.objects.create_user(username=self.username, password=self.password)
        UserProxy.objects.create_user(username=self.other_username,
                                      password=self.other_password)

    def tearDown(self):
        storage_manager = connect_storage(settings)

        for username in [self.username, self.other_username]:
            User.objects.get(username=username).delete()
            home = f'home/{username}'
            if storage_manager.path_exists(home):
                storage_manager.delete_path(home)


class FileBrowserFolderListViewTests(FileBrowserViewTests):
    """
    Test the 'chrisfolder-list' view.
    """

    def setUp(self):
        super(FileBrowserFolderListViewTests, self).setUp()
        self.create_read_url = reverse('chrisfolder-list')
        self.post = json.dumps(
            {"template":
                 {"data": [{"name": "path",
                            "value": f"home/{self.username}/uploads/folder1/folder2"}]}})

    def test_filebrowserfolder_create_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["path"], f"home/{self.username}/uploads/folder1/folder2")
        self.assertFalse(response.data["public"])

    def test_filebrowserfolder_create_public_status_keeps_unchanged(self):
        self.client.login(username=self.username, password=self.password)
        post = json.dumps(
            {"template":
                 {"data": [{"name": "public", "value": True}, {"name": "path",
                            "value": f"home/{self.username}/uploads/folder1/folder3"}]}})
        response = self.client.post(self.create_read_url, data=post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["path"], f"home/{self.username}/uploads/folder1/folder3")
        self.assertFalse(response.data["public"])

    def test_filebrowserfolder_create_failure_unauthenticated(self):
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfolder_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.create_read_url)
        self.assertContains(response, 'path')
        #import pdb; pdb.set_trace()
        self.assertEqual(response.data['results'][0]['path'],'')

    def test_filebrowserfolder_list_success_unauthenticated(self):
        response = self.client.get(self.create_read_url)
        self.assertContains(response, 'path')
        self.assertEqual(response.data['results'][0]['path'],'')


class FileBrowserFolderListQuerySearchViewTests(FileBrowserViewTests):
    """
    Test the 'chrisfolder-list-query-search' view.
    """

    def setUp(self):
        super(FileBrowserFolderListQuerySearchViewTests, self).setUp()

        # create compute resource
        (compute_resource, tf) = ComputeResource.objects.get_or_create(
            name="host", compute_url=COMPUTE_RESOURCE_URL)

        # create 'fs' plugin
        (pl_meta, tf) = PluginMeta.objects.get_or_create(name='pacspull', type='fs')
        (plugin, tf) = Plugin.objects.get_or_create(meta=pl_meta, version='0.1')
        plugin.compute_resources.set([compute_resource])
        plugin.save()

        self.plugin = plugin

    def test_filebrowserfolder_list_query_search_home_folder_success(self):
        read_url = reverse('chrisfolder-list-query-search') + '?path=home'
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(read_url)
        self.assertContains(response, 'home')

    def test_filebrowserfolder_list_query_search_home_folder_success_unauthenticated(self):
        read_url = reverse('chrisfolder-list-query-search') + '?path=home'
        response = self.client.get(read_url)
        self.assertContains(response, 'home')

    def test_filebrowserfolder_list_query_search_user_home_folder_success(self):
        read_url = reverse('chrisfolder-list-query-search') + f'?path=home/{self.username}'
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(read_url)
        self.assertContains(response, f'home/{self.username}')

    def test_filebrowserfolder_list_query_search_user_home_folder_failure_unauthenticated(self):
        read_url = reverse('chrisfolder-list-query-search') + f'?path=home/{self.username}'
        response = self.client.get(read_url)
        self.assertFalse(response.data['results'])

    def test_filebrowserfolder_list_query_search_SERVICES_folder_success(self):
        read_url = reverse('chrisfolder-list-query-search') + '?path=SERVICES'
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(read_url)
        self.assertContains(response, 'SERVICES')

    def test_filebrowserfolder_list_query_search_SERVICES_folder_failure_unauthenticated(self):
        read_url = reverse('chrisfolder-list-query-search') + '?path=SERVICES'
        response = self.client.get(read_url)
        self.assertFalse(response.data['results'])

    def test_filebrowserfolder_list_query_search_PIPELINES_folder_success(self):
        read_url = reverse('chrisfolder-list-query-search') + '?path=PIPELINES'
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(read_url)
        self.assertContains(response, 'PIPELINES')

    def test_filebrowserfolder_list_query_search_PIPELINES_folder_succes_unauthenticated(self):
        read_url = reverse('chrisfolder-list-query-search') + '?path=PIPELINES'
        response = self.client.get(read_url)
        self.assertContains(response, 'PIPELINES')

    def test_filebrowserfolder_list_query_search_feed_folder_success_shared_feed(self):
        other_user = User.objects.get(username=self.other_username)
        plugin = self.plugin

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=plugin, owner=other_user,
                                                title='test',
                                                compute_resource=plugin.compute_resources.all()[0])
        pl_inst.feed.name = 'shared_feed'
        pl_inst.feed.save()
        pl_inst.feed.grant_user_permission(User.objects.get(username=self.username))

        self.client.login(username=self.username, password=self.password)
        read_url = reverse('chrisfolder-list-query-search') + f'?path=home/{self.other_username}/feeds/feed_{pl_inst.feed.id}'

        response = self.client.get(read_url)
        self.assertContains(response, f'home/{self.other_username}/feeds/feed_{pl_inst.feed.id}')

    def test_filebrowserfolder_list_query_search_feed_folder_failure_shared_feed_unauthenticated(self):
        other_user = User.objects.get(username=self.other_username)
        plugin = self.plugin

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=plugin, owner=other_user,
                                                title='test',
                                                compute_resource=plugin.compute_resources.all()[0])
        pl_inst.feed.name = 'shared_feed'
        pl_inst.feed.save()
        pl_inst.feed.grant_user_permission(User.objects.get(username=self.username))

        read_url = reverse('chrisfolder-list-query-search') + f'?path=home/{self.other_username}/feeds/feed_{pl_inst.feed.id}'

        response = self.client.get(read_url)
        self.assertFalse(response.data['results'])

    def test_filebrowserfolder_list_query_search_feed_folder_success_public_feed(self):
        other_user = User.objects.get(username=self.other_username)
        plugin = self.plugin

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=plugin, owner=other_user,
                                                title='test',
                                                compute_resource=plugin.compute_resources.all()[0])
        pl_inst.feed.name = 'public_feed'
        pl_inst.feed.save()

        # make feed public
        pl_inst.feed.grant_public_access()

        self.client.login(username=self.username, password=self.password)
        read_url = reverse('chrisfolder-list-query-search') + f'?path=home/{self.other_username}/feeds/feed_{pl_inst.feed.id}'

        response = self.client.get(read_url)
        self.assertContains(response, f'home/{self.other_username}/feeds/feed_{pl_inst.feed.id}')
        self.assertTrue(response.data['results'])

        pl_inst.feed.remove_public_access()

    def test_filebrowserfolder_list_query_search_feed_folder_success_public_feed_unauthenticated(self):
        other_user = User.objects.get(username=self.other_username)
        plugin = self.plugin

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=plugin, owner=other_user,
                                                title='test',
                                                compute_resource=plugin.compute_resources.all()[0])
        pl_inst.feed.name = 'public_feed'
        pl_inst.feed.save()

        # make feed public
        pl_inst.feed.grant_public_access()

        read_url = reverse('chrisfolder-list-query-search') + f'?path=home/{self.other_username}/feeds/feed_{pl_inst.feed.id}'

        response = self.client.get(read_url)
        self.assertContains(response, f'home/{self.other_username}/feeds/feed_{pl_inst.feed.id}')
        self.assertTrue(response.data['results'])

        pl_inst.feed.remove_public_access()

    def test_filebrowserfolder_list_query_search_failure(self):
        self.client.login(username=self.username, password=self.password)
        read_url = reverse('chrisfolder-list-query-search') + '?path=LOLO'
        response = self.client.get(read_url)
        self.assertFalse(response.data['results'])

    def test_filebrowserfolder_list_query_search_failure_unauthenticated(self):
        read_url = reverse('chrisfolder-list-query-search') + '?path=LOLO'
        response = self.client.get(read_url)
        self.assertFalse(response.data['results'])

class FileBrowserFolderDetailViewTests(FileBrowserTasksViewTests):
    """
    Test the chrisfolder-detail view.
    """

    def setUp(self):
        super(FileBrowserFolderDetailViewTests, self).setUp()

        # create compute resource
        (compute_resource, tf) = ComputeResource.objects.get_or_create(
            name="host", compute_url=COMPUTE_RESOURCE_URL)

        # create 'fs' plugin
        (pl_meta, tf) = PluginMeta.objects.get_or_create(name='pacspull', type='fs')
        (plugin, tf) = Plugin.objects.get_or_create(meta=pl_meta, version='0.1')
        plugin.compute_resources.set([compute_resource])
        plugin.save()

        self.plugin = plugin

        folder = ChrisFolder.objects.get(path=f'home/{self.username}')
        self.read_update_delete_url = reverse("chrisfolder-detail",
                                              kwargs={"pk": folder.id})

        self.put = json.dumps({
            "template": {"data": [{"name": "public", "value": True}]}})

    def test_filebrowserfolder_detail_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_update_delete_url)
        self.assertContains(response, f'home/{self.username}')

    def test_filebrowserfolder_detail_failure_unauthenticated(self):
        response = self.client.get(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfolder_update_success(self):
        # create a folder
        owner = User.objects.get(username=self.username)
        folder, _ = ChrisFolder.objects.get_or_create(
            path=f'home/{self.username}/uploads/test_update', owner=owner)

        # create another folder within the folder
        inner_folder_path = f'home/{self.username}/uploads/test_update/inner'
        inner_folder, _ = ChrisFolder.objects.get_or_create(
            path=inner_folder_path, owner=owner)

        # create a file within the folder
        storage_manager = connect_storage(settings)
        file_path = f'home/{self.username}/uploads/test_update/update_file.txt'
        with io.StringIO("test file") as update_file:
            storage_manager.upload_obj(file_path, update_file.read(),
                                       content_type='text/plain')
        f = UserFile(owner=owner, parent_folder=folder)
        f.fname.name = file_path
        f.save()

        # create a link file within the folder
        lf_path = f'home/{self.username}/uploads/test_update/SERVICES_PACS.chrislink'
        lf = ChrisLinkFile(path='SERVICES/PACS', owner=owner, parent_folder=folder)
        lf.save(name='SERVICES_PACS')

        read_update_delete_url = reverse("chrisfolder-detail",
                                         kwargs={"pk": folder.id})

        new_path = f'home/{self.username}/uploads/test_update_folder/test'
        put = json.dumps({
            "template": {"data": [{"name": "public", "value": True},
                                  {"name": "path", "value": new_path}]}})

        self.client.login(username=self.username, password=self.password)
        response = self.client.put(read_update_delete_url, data=put,
                                   content_type=self.content_type)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["public"],True)
        self.assertEqual(response.data["path"], new_path)
        folder.refresh_from_db()

        inner_folder.refresh_from_db()
        self.assertEqual(inner_folder.path, new_path + '/inner')
        self.assertEqual(inner_folder.parent, folder)

        self.assertTrue(storage_manager.obj_exists(new_path + '/update_file.txt'))
        self.assertFalse(storage_manager.obj_exists(file_path))
        f.refresh_from_db()
        self.assertEqual(f.parent_folder, folder)

        self.assertTrue(storage_manager.obj_exists(new_path + '/SERVICES_PACS.chrislink'))
        self.assertFalse(storage_manager.obj_exists(lf_path))
        lf.refresh_from_db()
        self.assertEqual(lf.parent_folder, folder)

        folder.remove_public_link()
        folder.remove_public_access()
        folder.delete()

    def test_filebrowserfolder_update_failure_unauthenticated(self):
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfolder_update_failure_user_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_filebrowserfolder_delete_success(self):
        # create a folder
        owner = User.objects.get(username=self.username)
        folder, _ = ChrisFolder.objects.get_or_create(
            path=f'home/{self.username}/uploads/test', owner=owner)

        read_update_delete_url = reverse("chrisfolder-detail",
                                         kwargs={"pk": folder.id})
        self.client.login(username=self.username, password=self.password)

        with mock.patch.object(views.delete_folder, 'delay',
                               return_value=None) as delay_mock:
            response = self.client.delete(read_update_delete_url)
            self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

            # check that the delete_folder task was called with appropriate args
            delay_mock.assert_called_with(response.data['id'])

    @tag('integration')
    def test_integration_filebrowserfolder_delete_success(self):
        # create a folder
        owner = User.objects.get(username=self.username)
        folder, _ = ChrisFolder.objects.get_or_create(
            path=f'home/{self.username}/uploads/test', owner=owner)

        read_update_delete_url = reverse("chrisfolder-detail",
                                         kwargs={"pk": folder.id})
        self.client.login(username=self.username, password=self.password)
        folder_count = ChrisFolder.objects.count()
        response = self.client.delete(read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        for _ in range(10):
            time.sleep(3)
            if ChrisFolder.objects.count() == folder_count - 1: break
        self.assertEqual(ChrisFolder.objects.count(), folder_count - 1)

    def test_filebrowserfolder_delete_failure_home_folder(self):
        folder = ChrisFolder.objects.get(path=f'home/{self.username}')

        read_update_delete_url = reverse("chrisfolder-detail",
                                         kwargs={"pk": folder.id})

        self.client.login(username=self.username, password=self.password)
        response = self.client.delete(read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_filebrowserfolder_delete_failure_feeds_folder(self):
        folder = ChrisFolder.objects.get(path=f'home/{self.username}/feeds')

        read_update_delete_url = reverse("chrisfolder-detail",
                                         kwargs={"pk": folder.id})

        self.client.login(username=self.username, password=self.password)
        response = self.client.delete(read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_filebrowserfolder_delete_failure_unauthenticated(self):
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfolder_delete_failure_user_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_filebrowserfolder_home_folder_success(self):
        folder = ChrisFolder.objects.get(path='home')
        read_update_delete_url = reverse("chrisfolder-detail",
                                         kwargs={"pk": folder.id})
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(read_update_delete_url)
        self.assertContains(response, 'home')

    def test_filebrowserfolder_home_folder_failure_unauthenticated(self):
        folder = ChrisFolder.objects.get(path='home')
        read_url = reverse("chrisfolder-detail", kwargs={"pk": folder.id})
        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfolder_user_home_folder_success(self):
        folder = ChrisFolder.objects.get(path=f'home/{self.username}')
        read_url = reverse("chrisfolder-detail", kwargs={"pk": folder.id})
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(read_url)
        self.assertContains(response, f'home/{self.username}')

    def test_filebrowserfolder_user_home_folder_failure_not_unauthenticated(self):
        folder = ChrisFolder.objects.get(path=f'home/{self.username}')
        read_url = reverse("chrisfolder-detail", kwargs={"pk": folder.id})
        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfolder_SERVICES_folder_success(self):
        folder = ChrisFolder.objects.get(path='SERVICES')
        read_url = reverse("chrisfolder-detail", kwargs={"pk": folder.id})
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(read_url)
        self.assertContains(response, 'SERVICES')

    def test_filebrowserfolder_SERVICES_folder_failure_unauthenticated(self):
        folder = ChrisFolder.objects.get(path='SERVICES')
        read_url = reverse("chrisfolder-detail", kwargs={"pk": folder.id})
        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfolder_PIPELINES_folder_success(self):
        folder = ChrisFolder.objects.get(path='PIPELINES')
        read_url = reverse("chrisfolder-detail", kwargs={"pk": folder.id})
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(read_url)
        self.assertContains(response, 'PIPELINES')

    def test_filebrowserfolder_PIPELINES_folder_succes_unauthenticated(self):
        folder = ChrisFolder.objects.get(path='PIPELINES')
        read_url = reverse("chrisfolder-detail", kwargs={"pk": folder.id})
        response = self.client.get(read_url)
        self.assertContains(response, 'PIPELINES')

    def test_filebrowserfolder_feed_folder_success_shared_feed(self):
        other_user = User.objects.get(username=self.other_username)
        plugin = self.plugin

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=plugin, owner=other_user,
                                                title='test',
                                                compute_resource=
                                                plugin.compute_resources.all()[0])
        pl_inst.feed.name = 'shared_feed'
        pl_inst.feed.save()
        pl_inst.feed.grant_user_permission(User.objects.get(username=self.username))

        self.client.login(username=self.username, password=self.password)
        folder = ChrisFolder.objects.get(path=f'home/{self.other_username}/feeds/feed_{pl_inst.feed.id}')
        read_url = reverse("chrisfolder-detail", kwargs={"pk": folder.id})

        response = self.client.get(read_url)
        self.assertContains(response,
                            f'home/{self.other_username}/feeds/feed_{pl_inst.feed.id}')

    def test_filebrowserfolder_feed_folder_failure_not_found_shared_feed_unauthenticated(self):
        other_user = User.objects.get(username=self.other_username)
        plugin = self.plugin

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=plugin, owner=other_user,
                                                title='test',
                                                compute_resource=
                                                plugin.compute_resources.all()[0])
        pl_inst.feed.name = 'shared_feed'
        pl_inst.feed.save()
        pl_inst.feed.grant_user_permission(User.objects.get(username=self.username))

        folder = ChrisFolder.objects.get(
            path=f'home/{self.other_username}/feeds/feed_{pl_inst.feed.id}')
        read_url = reverse("chrisfolder-detail", kwargs={"pk": folder.id})

        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfolder_feed_folder_success_public_feed(self):
        other_user = User.objects.get(username=self.other_username)
        plugin = self.plugin

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=plugin, owner=other_user,
                                                title='test',
                                                compute_resource=
                                                plugin.compute_resources.all()[0])
        pl_inst.feed.name = 'public_feed'
        pl_inst.feed.save()

        pl_inst.feed.grant_public_access()

        self.client.login(username=self.username, password=self.password)
        folder = ChrisFolder.objects.get(
            path=f'home/{self.other_username}/feeds/feed_{pl_inst.feed.id}')
        read_url = reverse("chrisfolder-detail", kwargs={"pk": folder.id})

        response = self.client.get(read_url)
        self.assertContains(response,
                            f'home/{self.other_username}/feeds/feed_{pl_inst.feed.id}')

        pl_inst.feed.remove_public_access()

    def test_filebrowserfolder_feed_folder_success_public_feed_unauthenticated(self):
        other_user = User.objects.get(username=self.other_username)
        plugin = self.plugin

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=plugin, owner=other_user,
                                                title='test',
                                                compute_resource=
                                                plugin.compute_resources.all()[0])
        pl_inst.feed.name = 'public_feed'
        pl_inst.feed.save()

        # make feed public
        pl_inst.feed.grant_public_access()

        folder = ChrisFolder.objects.get(
            path=f'home/{self.other_username}/feeds/feed_{pl_inst.feed.id}')
        read_url = reverse("chrisfolder-detail", kwargs={"pk": folder.id})

        response = self.client.get(read_url)
        self.assertContains(response,
                            f'home/{self.other_username}/feeds/feed_{pl_inst.feed.id}')
        pl_inst.feed.remove_public_access()

    def test_filebrowserfolder_failure_not_found(self):
        self.client.login(username=self.username, password=self.password)
        read_url = reverse("chrisfolder-detail", kwargs={"pk": 111111111})
        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_filebrowserfolder_failure_not_found_unauthenticated(self):
        read_url = reverse("chrisfolder-detail", kwargs={"pk": 111111111})
        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class FileBrowserFolderChildListViewTests(FileBrowserViewTests):
    """
    Test the 'chrisfolder-child-list' view.
    """

    def setUp(self):
        super(FileBrowserFolderChildListViewTests, self).setUp()

        # create compute resource
        (compute_resource, tf) = ComputeResource.objects.get_or_create(
            name="host", compute_url=COMPUTE_RESOURCE_URL)

        # create 'fs' plugin
        (pl_meta, tf) = PluginMeta.objects.get_or_create(name='pacspull', type='fs')
        (plugin, tf) = Plugin.objects.get_or_create(meta=pl_meta, version='0.1')
        plugin.compute_resources.set([compute_resource])
        plugin.save()

        self.plugin = plugin

    def test_filebrowserfolderchild_list_root_folder_success(self):
        folder = ChrisFolder.objects.get(path='')
        read_url = reverse("chrisfolder-child-list", kwargs={"pk": folder.id})
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(read_url)
        self.assertContains(response, 'home')
        self.assertContains(response, 'SERVICES')
        self.assertContains(response, 'PIPELINES')
        self.assertContains(response, 'PUBLIC')
        self.assertContains(response, 'SHARED')
        self.assertEqual(len(response.data['results']), 5)

    def test_filebrowserfolderchild_list_root_folder_success_unauthenticated(self):
        folder = ChrisFolder.objects.get(path='')
        read_url = reverse("chrisfolder-child-list", kwargs={"pk": folder.id})
        response = self.client.get(read_url)
        self.assertContains(response, 'PUBLIC')
        self.assertContains(response, 'PIPELINES')
        self.assertEqual(len(response.data['results']), 2)

    def test_filebrowserfolderchild_list_home_folder_success(self):
        folder = ChrisFolder.objects.get(path='home')
        read_url = reverse("chrisfolder-child-list", kwargs={"pk": folder.id})
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(read_url)
        self.assertContains(response, f'home/{self.username}')
        self.assertEqual(len(response.data['results']), 1)

    def test_filebrowserfolderchild_list_home_folder_empty_unauthenticated(self):
        folder = ChrisFolder.objects.get(path='home')
        read_url = reverse("chrisfolder-child-list", kwargs={"pk": folder.id})
        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfolderchild_list_user_home_folder_success(self):
        folder = ChrisFolder.objects.get(path=f'home/{self.username}')
        read_url = reverse("chrisfolder-child-list", kwargs={"pk": folder.id})
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(read_url)
        self.assertContains(response, f'home/{self.username}')

    def test_filebrowserfolderchild_list_user_home_folder_failure_unauthenticated(self):
        folder = ChrisFolder.objects.get(path=f'home/{self.username}')
        read_url = reverse("chrisfolder-child-list", kwargs={"pk": folder.id})
        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfolderchild_list_SERVICES_folder_success(self):
        folder = ChrisFolder.objects.get(path='SERVICES')
        read_url = reverse("chrisfolder-child-list", kwargs={"pk": folder.id})
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(read_url)
        self.assertContains(response, 'SERVICES/PACS')

    def test_filebrowserfolderchild_list_SERVICES_folder_failure_unauthenticated(self):
        folder = ChrisFolder.objects.get(path='SERVICES')
        read_url = reverse("chrisfolder-child-list", kwargs={"pk": folder.id})
        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfolderchild_list_PIPELINES_folder_success(self):
        folder = ChrisFolder.objects.get(path='PIPELINES')
        read_url = reverse("chrisfolder-child-list", kwargs={"pk": folder.id})
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_filebrowserfolderchild_list_PIPELINES_folder_succes_unauthenticated(self):
        folder = ChrisFolder.objects.get(path='PIPELINES')
        read_url = reverse("chrisfolder-child-list", kwargs={"pk": folder.id})
        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_filebrowserfolderchild_list_feeds_folder_failure_shared_feed(self):
        other_user = User.objects.get(username=self.other_username)
        plugin = self.plugin

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=plugin, owner=other_user,
                                                title='test',
                                                compute_resource=
                                                plugin.compute_resources.all()[0])
        pl_inst.feed.name = 'shared_feed'
        pl_inst.feed.save()
        pl_inst.feed.grant_user_permission(User.objects.get(username=self.username))

        self.client.login(username=self.username, password=self.password)
        folder = ChrisFolder.objects.get(path=f'home/{self.other_username}/feeds')
        read_url = reverse("chrisfolder-child-list", kwargs={"pk": folder.id})
        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_filebrowserfolderchild_list_feed_folder_success_shared_feed(self):
        other_user = User.objects.get(username=self.other_username)
        plugin = self.plugin

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=plugin, owner=other_user,
                                                title='test',
                                                compute_resource=
                                                plugin.compute_resources.all()[0])
        pl_inst.feed.name = 'shared_feed'
        pl_inst.feed.save()
        pl_inst.feed.grant_user_permission(User.objects.get(username=self.username))

        self.client.login(username=self.username, password=self.password)
        folder = ChrisFolder.objects.get(
            path=f'home/{self.other_username}/feeds/feed_{pl_inst.feed.id}')
        read_url = reverse("chrisfolder-child-list", kwargs={"pk": folder.id})

        response = self.client.get(read_url)
        self.assertContains(response,
                            f'home/{self.other_username}/feeds/feed_{pl_inst.feed.id}/')

    def test_filebrowserfolderchild_list_feed_folder_failure_shared_feed_unauthenticated(self):
        other_user = User.objects.get(username=self.other_username)
        plugin = self.plugin

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=plugin, owner=other_user,
                                                title='test',
                                                compute_resource=
                                                plugin.compute_resources.all()[0])
        pl_inst.feed.name = 'shared_feed'
        pl_inst.feed.save()
        pl_inst.feed.grant_user_permission(User.objects.get(username=self.username))

        folder = ChrisFolder.objects.get(
            path=f'home/{self.other_username}/feeds/feed_{pl_inst.feed.id}')
        read_url = reverse("chrisfolder-child-list", kwargs={"pk": folder.id})

        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfolderchild_list_feed_folder_success_public_feed(self):
        other_user = User.objects.get(username=self.other_username)
        plugin = self.plugin

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=plugin, owner=other_user,
                                                title='test',
                                                compute_resource=plugin.compute_resources.all()[0])
        pl_inst.feed.name = 'public_feed'
        pl_inst.feed.save()

        # make feed public
        pl_inst.feed.grant_public_access()

        self.client.login(username=self.username, password=self.password)
        folder = ChrisFolder.objects.get(
            path=f'home/{self.other_username}/feeds/feed_{pl_inst.feed.id}')
        read_url = reverse("chrisfolder-child-list", kwargs={"pk": folder.id})

        response = self.client.get(read_url)
        self.assertContains(response,
                            f'home/{self.other_username}/feeds/feed_{pl_inst.feed.id}/')
        pl_inst.feed.remove_public_access()

    def test_filebrowserfolderchild_list_feed_folder_success_public_feed_unauthenticated(self):
        other_user = User.objects.get(username=self.other_username)
        plugin = self.plugin

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=plugin, owner=other_user,
                                                title='test',
                                                compute_resource=
                                                plugin.compute_resources.all()[0])
        pl_inst.feed.name = 'public_feed'
        pl_inst.feed.save()

        # make feed public
        pl_inst.feed.grant_public_access()

        folder = ChrisFolder.objects.get(
            path=f'home/{self.other_username}/feeds/feed_{pl_inst.feed.id}')
        read_url = reverse("chrisfolder-child-list", kwargs={"pk": folder.id})

        response = self.client.get(read_url)
        self.assertContains(response,
                            f'home/{self.other_username}/feeds/feed_{pl_inst.feed.id}/')
        pl_inst.feed.remove_public_access()

    def test_filebrowserfolderchild_list_failure_not_found(self):
        self.client.login(username=self.username, password=self.password)
        read_url = reverse("chrisfolder-child-list", kwargs={"pk": 111111111})
        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_filebrowserfolderchild_list_failure_not_found_unauthenticated(self):
        read_url = reverse("chrisfolder-child-list", kwargs={"pk": 111111111})
        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class FileBrowserFolderGroupPermissionListViewTests(FileBrowserViewTests):
    """
    Test the 'foldergrouppermission-list' view.
    """

    def setUp(self):
        super(FileBrowserFolderGroupPermissionListViewTests, self).setUp()

        user = User.objects.get(username=self.username)
        self.grp_name = 'all_users'

        # create folder
        self.path = f'home/{self.username}/test1'
        folder = ChrisFolder.objects.create(path=self.path, owner=user)

        self.create_read_url = reverse('foldergrouppermission-list',
                                       kwargs={"pk": folder.id})
        self.post = json.dumps(
            {"template":
                 {"data": [{"name": "grp_name", "value": self.grp_name},
                           {"name": "permission", "value": "r"}]}})

    def tearDown(self):
        # delete folder tree
        folder = ChrisFolder.objects.get(path=self.path)
        folder.delete()

        super(FileBrowserFolderGroupPermissionListViewTests, self).tearDown()

    def test_filebrowserfoldergrouppermission_create_success(self):
        user = User.objects.get(username=self.username)

        # create inner folder
        inner_folder = ChrisFolder.objects.create(path=f'{self.path}/inner', owner=user)

        # create a file in the inner folder
        storage_manager = connect_storage(settings)

        upload_path = f'{self.path}/inner/file7.txt'
        with io.StringIO("test file") as file7:
            storage_manager.upload_obj(upload_path, file7.read(),
                                       content_type='text/plain')
        f = UserFile(owner=user, parent_folder=inner_folder)
        f.fname.name = upload_path
        f.save()

        # create link file in the inner folder
        lf = ChrisLinkFile(path='SERVICES/PACS', owner=user,
                                       parent_folder=inner_folder)
        lf.save(name='SERVICES_PACS')

        self.client.login(username=self.username, password=self.password)

        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        folder = ChrisFolder.objects.get(path=self.path)

        self.assertIn(self.grp_name, [g.name for g in folder.shared_groups.all()])
        self.assertIn(self.grp_name, [g.name for g in inner_folder.shared_groups.all()])
        self.assertIn(self.grp_name, [g.name for g in f.shared_groups.all()])
        self.assertIn(self.grp_name, [g.name for g in lf.shared_groups.all()])

        folder.remove_shared_link()

    def test_filebrowserfoldergrouppermission_create_failure_unauthenticated(self):
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfoldergrouppermission_create_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_filebrowserfoldergrouppermission_shared_create_failure_access_denied(self):
        user = User.objects.get(username=self.other_username)
        folder = ChrisFolder.objects.get(path=self.path)
        folder.grant_user_permission(user, 'w')

        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_filebrowserfoldergrouppermission_list_success(self):
        grp = Group.objects.get(name=self.grp_name)
        folder = ChrisFolder.objects.get(path=self.path)
        folder.grant_group_permission(grp, 'r')

        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.create_read_url)
        self.assertContains(response, self.grp_name)

    def test_filebrowserfoldergrouppermission_list_failure_unauthenticated(self):
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfoldergrouppermission_list_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_filebrowserfoldergrouppermission_shared_user_list_success(self):
        user = User.objects.get(username=self.other_username)
        folder = ChrisFolder.objects.get(path=self.path)
        folder.grant_user_permission(user, 'w')

        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class FileBrowserFolderGroupPermissionListQuerySearchViewTests(FileBrowserViewTests):
    """
    Test the 'foldergrouppermission-list-query-search' view.
    """

    def setUp(self):
        super(FileBrowserFolderGroupPermissionListQuerySearchViewTests, self).setUp()

        user = User.objects.get(username=self.username)
        self.grp_name = 'all_users'

        # create folder
        self.path = f'home/{self.username}/test2'
        folder = ChrisFolder.objects.create(path=self.path, owner=user)

        self.read_url = reverse('foldergrouppermission-list-query-search',
                                kwargs={"pk": folder.id})

        grp = Group.objects.get(name=self.grp_name)
        folder.grant_group_permission(grp, 'r')

    def tearDown(self):
        # delete folder tree
        folder = ChrisFolder.objects.get(path=self.path)
        folder.delete()

        super(FileBrowserFolderGroupPermissionListQuerySearchViewTests, self).tearDown()

    def test_filebrowserfoldergrouppermission_list_query_search_success(self):
        read_url = f'{self.read_url}?group_name={self.grp_name}'

        self.client.login(username=self.username, password=self.password)
        response = self.client.get(read_url)
        self.assertContains(response, self.grp_name)

    def test_filebrowserfoldergrouppermission_list_query_search_success_shared(self):
        read_url = f'{self.read_url}?group_name={self.grp_name}'

        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(read_url)
        self.assertContains(response, self.grp_name)

    def test_filebrowserfoldergrouppermission_list_query_search_failure_unauthenticated(self):
        read_url = f'{self.read_url}?group_name={self.grp_name}'

        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfoldergrouppermission_list_query_search_failure_other_user(self):
        grp = Group.objects.get(name=self.grp_name)
        folder = ChrisFolder.objects.get(path=self.path)
        folder.remove_group_permission(grp, 'r')

        read_url = f'{self.read_url}?group_name={self.grp_name}'

        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['results'])


class FileBrowserFolderGroupPermissionDetailViewTests(FileBrowserViewTests):
    """
    Test the foldergrouppermission-detail view.
    """

    def setUp(self):
        super(FileBrowserFolderGroupPermissionDetailViewTests, self).setUp()

        user = User.objects.get(username=self.username)
        self.grp_name = 'all_users'

        # create folder
        self.path = f'home/{self.username}/test3'
        folder = ChrisFolder.objects.create(path=self.path, owner=user)

        grp = Group.objects.get(name=self.grp_name)
        folder.grant_group_permission(grp, 'r')

        gp = FolderGroupPermission.objects.get(group=grp, folder=folder)

        self.read_update_delete_url = reverse("foldergrouppermission-detail",
                                              kwargs={"pk": gp.id})

        self.put = json.dumps({
            "template": {"data": [{"name": "permission", "value": "w"}]}})


    def tearDown(self):
        # delete folder tree
        folder = ChrisFolder.objects.get(path=self.path)
        folder.delete()

        super(FileBrowserFolderGroupPermissionDetailViewTests, self).tearDown()

    def test_filebrowserfoldergrouppermission_detail_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_update_delete_url)
        self.assertContains(response, 'all_users')
        self.assertContains(response, self.path)

    def test_filebrowserfoldergrouppermission_detail_shared_success(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.read_update_delete_url)
        self.assertContains(response, 'all_users')
        self.assertContains(response, self.path)

    def test_filebrowserfoldergrouppermission_detail_failure_unauthenticated(self):
        response = self.client.get(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfoldergrouppermission_update_success(self):
        user = User.objects.get(username=self.username)

        # create inner folder
        inner_folder = ChrisFolder.objects.create(path=f'{self.path}/inner', owner=user)

        # create a file in the inner folder
        storage_manager = connect_storage(settings)

        upload_path = f'{self.path}/inner/file8.txt'
        with io.StringIO("test file") as file8:
            storage_manager.upload_obj(upload_path, file8.read(),
                                       content_type='text/plain')
        f = UserFile(owner=user, parent_folder=inner_folder)
        f.fname.name = upload_path
        f.save()

        # create link file in the inner folder
        lf = ChrisLinkFile(path='SERVICES/PACS', owner=user,
                                       parent_folder=inner_folder)
        lf.save(name='SERVICES_PACS')

        self.client.login(username=self.username, password=self.password)
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["permission"], 'w')

        self.assertEqual(FileGroupPermission.objects.get(file=f).permission, 'w')
        self.assertEqual(LinkFileGroupPermission.objects.get(link_file=lf).permission,
                         'w')

    def test_filebrowserfoldergrouppermission_update_failure_unauthenticated(self):
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfoldergrouppermission_update_failure_user_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_filebrowserfoldergrouppermission_delete_success(self):
        folder = ChrisFolder.objects.get(path=self.path)
        grp = Group.objects.get(name='pacs_users')

        # create a group permission
        folder.grant_group_permission(grp, 'r')
        gp = FolderGroupPermission.objects.get(group=grp, folder=folder)

        read_update_delete_url = reverse("foldergrouppermission-detail",
                                         kwargs={"pk": gp.id})
        self.client.login(username=self.username, password=self.password)
        response = self.client.delete(read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_filebrowserfoldergrouppermission_delete_failure_unauthenticated(self):
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfoldergrouppermission_delete_failure_user_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class FileBrowserFolderUserPermissionListViewTests(FileBrowserViewTests):
    """
    Test the 'folderuserpermission-list' view.
    """

    def setUp(self):
        super(FileBrowserFolderUserPermissionListViewTests, self).setUp()

        user = User.objects.get(username=self.username)

        # create folder
        self.path = f'home/{self.username}/test4'
        folder = ChrisFolder.objects.create(path=self.path, owner=user)

        self.create_read_url = reverse('folderuserpermission-list',
                                       kwargs={"pk": folder.id})
        self.post = json.dumps(
            {"template":
                 {"data": [{"name": "username", "value": self.other_username},
                           {"name": "permission", "value": "r"}]}})

    def tearDown(self):
        # delete folder tree
        folder = ChrisFolder.objects.get(path=self.path)
        folder.delete()

        super(FileBrowserFolderUserPermissionListViewTests, self).tearDown()

    def test_filebrowserfolderuserpermission_create_success(self):
        user = User.objects.get(username=self.username)

        # create inner folder
        inner_folder = ChrisFolder.objects.create(path=f'{self.path}/inner', owner=user)

        # create a file in the inner folder
        storage_manager = connect_storage(settings)

        upload_path = f'{self.path}/inner/file7.txt'
        with io.StringIO("test file") as file7:
            storage_manager.upload_obj(upload_path, file7.read(),
                                       content_type='text/plain')
        f = UserFile(owner=user, parent_folder=inner_folder)
        f.fname.name = upload_path
        f.save()

        # create link file in the inner folder
        lf = ChrisLinkFile(path='SERVICES/PACS', owner=user,
                                       parent_folder=inner_folder)
        lf.save(name='SERVICES_PACS')

        self.client.login(username=self.username, password=self.password)

        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        folder = ChrisFolder.objects.get(path=self.path)

        self.assertIn(self.other_username, [u.username for u in folder.shared_users.all()])
        self.assertIn(self.other_username, [u.username for u in inner_folder.shared_users.all()])
        self.assertIn(self.other_username, [u.username for u in f.shared_users.all()])
        self.assertIn(self.other_username, [u.username for u in lf.shared_users.all()])

        folder.remove_shared_link()

    def test_filebrowserfolderuserpermission_create_failure_unauthenticated(self):
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfolderuserpermission_create_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_filebrowserfolderuserpermission_shared_create_failure_access_denied(self):
        user = User.objects.get(username=self.other_username)
        folder = ChrisFolder.objects.get(path=self.path)
        folder.grant_user_permission(user, 'w')

        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_filebrowserfolderuserpermission_list_success(self):
        user = User.objects.get(username=self.other_username)
        folder = ChrisFolder.objects.get(path=self.path)
        folder.grant_user_permission(user, 'r')

        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.create_read_url)
        self.assertContains(response, self.other_username)

    def test_filebrowserfolderuserpermission_list_failure_unauthenticated(self):
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfolderuserpermission_list_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_filebrowserfolderuserpermission_shared_user_list_success(self):
        user = User.objects.get(username=self.other_username)
        folder = ChrisFolder.objects.get(path=self.path)
        folder.grant_user_permission(user, 'w')

        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class FileBrowserFolderUserPermissionListQuerySearchViewTests(FileBrowserViewTests):
    """
    Test the 'folderuserpermission-list-query-search' view.
    """

    def setUp(self):
        super(FileBrowserFolderUserPermissionListQuerySearchViewTests, self).setUp()

        user = User.objects.get(username=self.username)

        # create folder
        self.path = f'home/{self.username}/test5'
        folder = ChrisFolder.objects.create(path=self.path, owner=user)

        self.read_url = reverse('folderuserpermission-list-query-search',
                                kwargs={"pk": folder.id})

        other_user = User.objects.get(username=self.other_username)
        folder.grant_user_permission(other_user, 'r')

    def tearDown(self):
        # delete folder tree
        folder = ChrisFolder.objects.get(path=self.path)
        folder.delete()

        super(FileBrowserFolderUserPermissionListQuerySearchViewTests, self).tearDown()

    def test_filebrowserfolderuserpermission_list_query_search_success(self):
        read_url = f'{self.read_url}?username={self.other_username}'

        self.client.login(username=self.username, password=self.password)
        response = self.client.get(read_url)
        self.assertContains(response, self.other_username)

    def test_filebrowserfolderuserpermission_list_query_search_success_shared(self):
        read_url = f'{self.read_url}?username={self.other_username}'

        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(read_url)
        self.assertContains(response, self.other_username)

    def test_filebrowserfolderuserpermission_list_query_search_failure_unauthenticated(self):
        read_url = f'{self.read_url}?username={self.other_username}'

        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class FileBrowserFolderUserPermissionDetailViewTests(FileBrowserViewTests):
    """
    Test the folderuserpermission-detail view.
    """

    def setUp(self):
        super(FileBrowserFolderUserPermissionDetailViewTests, self).setUp()

        user = User.objects.get(username=self.username)

        # create folder
        self.path = f'home/{self.username}/test6'
        folder = ChrisFolder.objects.create(path=self.path, owner=user)

        other_user = User.objects.get(username=self.other_username)
        folder.grant_user_permission(other_user, 'r')

        up = FolderUserPermission.objects.get(user=other_user, folder=folder)

        self.read_update_delete_url = reverse("folderuserpermission-detail",
                                              kwargs={"pk": up.id})

        self.put = json.dumps({
            "template": {"data": [{"name": "permission", "value": "w"}]}})


    def tearDown(self):
        # delete folder tree
        folder = ChrisFolder.objects.get(path=self.path)
        folder.delete()

        super(FileBrowserFolderUserPermissionDetailViewTests, self).tearDown()

    def test_filebrowserfolderuserpermission_detail_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_update_delete_url)
        self.assertContains(response, self.other_username)
        self.assertContains(response, self.path)

    def test_filebrowserfolderuserpermission_detail_shared_success(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.read_update_delete_url)
        self.assertContains(response, self.other_username)
        self.assertContains(response, self.path)

    def test_filebrowserfolderuserpermission_detail_failure_unauthenticated(self):
        response = self.client.get(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfolderuserpermission_update_success(self):
        user = User.objects.get(username=self.username)

        # create inner folder
        inner_folder = ChrisFolder.objects.create(path=f'{self.path}/inner', owner=user)

        # create a file in the inner folder
        storage_manager = connect_storage(settings)

        upload_path = f'{self.path}/inner/file8.txt'
        with io.StringIO("test file") as file8:
            storage_manager.upload_obj(upload_path, file8.read(),
                                       content_type='text/plain')
        f = UserFile(owner=user, parent_folder=inner_folder)
        f.fname.name = upload_path
        f.save()

        # create link file in the inner folder
        lf = ChrisLinkFile(path='SERVICES/PACS', owner=user,
                                       parent_folder=inner_folder)
        lf.save(name='SERVICES_PACS')

        self.client.login(username=self.username, password=self.password)
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["permission"], 'w')

        self.assertEqual(FileUserPermission.objects.get(file=f).permission, 'w')
        self.assertEqual(LinkFileUserPermission.objects.get(link_file=lf).permission,
                         'w')

    def test_filebrowserfolderuserpermission_update_failure_unauthenticated(self):
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfolderuserpermission_update_failure_user_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_filebrowserfolderuserpermission_delete_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_filebrowserfolderuserpermission_delete_failure_unauthenticated(self):
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfolderuserpermission_delete_failure_user_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class FileBrowserFolderFileListViewTests(FileBrowserViewTests):
    """
    Test the 'chrisfolder-file-list' view.
    """

    def setUp(self):
        super(FileBrowserFolderFileListViewTests, self).setUp()

        # create compute resource
        (compute_resource, tf) = ComputeResource.objects.get_or_create(
            name="host", compute_url=COMPUTE_RESOURCE_URL)

        # create 'fs' plugin
        (pl_meta, tf) = PluginMeta.objects.get_or_create(name='pacspull', type='fs')
        (plugin, tf) = Plugin.objects.get_or_create(meta=pl_meta, version='0.1')
        plugin.compute_resources.set([compute_resource])
        plugin.save()

        self.plugin = plugin

        # create a file in the DB "already uploaded" to the server)
        self.storage_manager = connect_storage(settings)
        # upload file to storage
        self.upload_path = f'home/{self.username}/uploads/file2.txt'
        with io.StringIO("test file") as file1:
            self.storage_manager.upload_obj(self.upload_path, file1.read(),
                                         content_type='text/plain')
        user = User.objects.get(username=self.username)

        folder_path = os.path.dirname(self.upload_path)
        (file_parent_folder, _) = ChrisFolder.objects.get_or_create(path=folder_path,
                                                                    owner=user)
        self.file = UserFile(owner=user, parent_folder=file_parent_folder)
        self.file.fname.name = self.upload_path
        self.file.save()

        self.read_url = reverse("chrisfolder-file-list",
                                kwargs={"pk": file_parent_folder.id})

    def tearDown(self):
        # delete file from storage
        self.file.delete()

        super(FileBrowserFolderFileListViewTests, self).tearDown()

    def test_filebrowserfolderfile_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_url)
        self.assertContains(response, 'file_resource')
        self.assertContains(response, self.upload_path)

    def test_filebrowserfolderfile_list_success_public_feed_unauthenticated(self):
        user = User.objects.get(username=self.username)

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=self.plugin, owner=user,
                                                title='test',
                                                compute_resource=
                                                self.plugin.compute_resources.all()[0])

        # create file
        file_path = f'{pl_inst.output_folder.path}/file3.txt'
        with io.StringIO("test file") as file3:
            self.storage_manager.upload_obj(file_path, file3.read(),
                                         content_type='text/plain')

        userfile = UserFile(owner=user, parent_folder=pl_inst.output_folder)
        userfile.fname.name = file_path
        userfile.save()

        # make feed public
        pl_inst.feed.grant_public_access()

        read_url = reverse("chrisfolder-file-list",
                           kwargs={"pk": pl_inst.output_folder.id})

        response = self.client.get(read_url)
        self.assertContains(response, 'file_resource')
        self.assertContains(response, file_path)

        pl_inst.feed.remove_public_access()
        userfile.delete()

    def test_filebrowserfolderfile_list_failure_unauthenticated(self):
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfolderfile_list_failure_not_found(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(reverse("chrisfolder-file-list", kwargs={"pk": 111111111}))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_filebrowserfolderfile_list_file_folder_success(self):
        folder_path = os.path.dirname(self.upload_path)
        folder = ChrisFolder.objects.get(path=folder_path)
        read_url = reverse("chrisfolder-file-list", kwargs={"pk": folder.id})
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(read_url)
        self.assertContains(response, self.upload_path)

    def test_fileBrowserfile_list_success_shared_feed(self):
        other_user = User.objects.get(username=self.other_username)
        plugin = self.plugin

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=plugin, owner=other_user,
                                                title='test',
                                                compute_resource=
                                                plugin.compute_resources.all()[0])
        pl_inst.feed.name = 'shared_feed'
        pl_inst.feed.save()

        file_path = f'{pl_inst.output_folder.path}/file3.txt'
        with io.StringIO("test file") as file3:
            self.storage_manager.upload_obj(file_path, file3.read(),
                                         content_type='text/plain')

        userfile = UserFile(owner=other_user, parent_folder=pl_inst.output_folder)
        userfile.fname.name = file_path
        userfile.save()

        # share feed
        pl_inst.feed.grant_user_permission(User.objects.get(username=self.username))

        read_url = reverse("chrisfolder-file-list",
                           kwargs={"pk": pl_inst.output_folder.id})
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(read_url)
        self.assertContains(response, file_path)

        userfile.delete()

    def test_fileBrowserfile_list_failure_shared_feed_unauthenticated(self):
        other_user = User.objects.get(username=self.other_username)
        plugin = self.plugin

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=plugin, owner=other_user,
                                                title='test',
                                                compute_resource=
                                                plugin.compute_resources.all()[0])
        pl_inst.feed.name = 'shared_feed'
        pl_inst.feed.save()

        file_path = f'{pl_inst.output_folder.path}/file3.txt'
        with io.StringIO("test file") as file3:
            self.storage_manager.upload_obj(file_path, file3.read(),
                                         content_type='text/plain')

        userfile = UserFile(owner=other_user, parent_folder=pl_inst.output_folder)
        userfile.fname.name = file_path
        userfile.save()

        # share feed
        pl_inst.feed.grant_user_permission(User.objects.get(username=self.username))

        read_url = reverse("chrisfolder-file-list",
                           kwargs={"pk": pl_inst.output_folder.id})

        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        userfile.delete()


class FileBrowserFileDetailViewTests(FileBrowserViewTests):
    """
    Test the chrisfile-detail view.
    """

    def setUp(self):
        super(FileBrowserFileDetailViewTests, self).setUp()

        # create compute resource
        (compute_resource, tf) = ComputeResource.objects.get_or_create(
            name="host", compute_url=COMPUTE_RESOURCE_URL)

        # create 'fs' plugin
        (pl_meta, tf) = PluginMeta.objects.get_or_create(name='pacspull', type='fs')
        (plugin, tf) = Plugin.objects.get_or_create(meta=pl_meta, version='0.1')
        plugin.compute_resources.set([compute_resource])
        plugin.save()

        self.plugin = plugin

        # create a file in the DB "already uploaded" to the server)
        self.storage_manager = connect_storage(settings)
        # upload file to storage
        self.upload_path = f'home/{self.username}/uploads/file2.txt'
        with io.StringIO("test file") as file1:
            self.storage_manager.upload_obj(self.upload_path, file1.read(),
                                         content_type='text/plain')
        user = User.objects.get(username=self.username)

        folder_path = os.path.dirname(self.upload_path)
        (file_parent_folder, _) = ChrisFolder.objects.get_or_create(path=folder_path,
                                                                    owner=user)
        self.file = UserFile(owner=user, parent_folder=file_parent_folder)
        self.file.fname.name = self.upload_path
        self.file.save()

        self.read_update_delete_url = reverse("chrisfile-detail",
                                              kwargs={"pk": self.file.id})
        self.put = json.dumps({
            "template": {"data": [{"name": "public", "value": True}]}})

    def tearDown(self):
        self.file.delete()
        super(FileBrowserFileDetailViewTests, self).tearDown()

    def test_fileBrowserfile_detail_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_update_delete_url)
        self.assertContains(response, self.upload_path)

    def test_fileBrowserfile_detail_success_user_chris(self):
        self.client.login(username=self.chris_username, password=self.chris_password)
        response = self.client.get(self.read_update_delete_url)
        self.assertContains(response, self.upload_path)

    def test_fileBrowserfile_detail_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
    def test_fileBrowserfile_detail_failure_unauthenticated(self):
        response = self.client.get(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_fileBrowserfile_detail_success_shared_feed(self):
        other_user = User.objects.get(username=self.other_username)
        plugin = self.plugin

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=plugin, owner=other_user,
                                                title='test',
                                                compute_resource=
                                                plugin.compute_resources.all()[0])
        pl_inst.feed.name = 'shared_feed'
        pl_inst.feed.save()

        # create file in the output folder
        file_path = f'{pl_inst.output_folder.path}/file4.txt'
        with io.StringIO("test file") as file4:
            self.storage_manager.upload_obj(file_path, file4.read(),
                                         content_type='text/plain')

        userfile = UserFile(owner=other_user, parent_folder=pl_inst.output_folder)
        userfile.fname.name = file_path
        userfile.save()

        # share feed
        user = User.objects.get(username=self.username)
        pl_inst.feed.grant_user_permission(user)

        read_update_delete_url = reverse("chrisfile-detail", kwargs={"pk": userfile.id})
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(read_update_delete_url)
        self.assertContains(response, file_path)

        userfile.delete()

    def test_fileBrowserfile_detail_failure_unauthorized_shared_feed_unauthenticated(self):
        other_user = User.objects.get(username=self.other_username)
        plugin = self.plugin

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=plugin, owner=other_user,
                                                title='test',
                                                compute_resource=
                                                plugin.compute_resources.all()[0])
        pl_inst.feed.name = 'shared_feed'
        pl_inst.feed.save()
        pl_inst.feed.grant_user_permission(User.objects.get(username=self.username))

        # create file in the output folder
        file_path = f'{pl_inst.output_folder.path}/file5.txt'
        with io.StringIO("test file") as file5:
            self.storage_manager.upload_obj(file_path, file5.read(),
                                         content_type='text/plain')

        userfile = UserFile(owner=other_user, parent_folder=pl_inst.output_folder)
        userfile.fname.name = file_path
        userfile.save()

        read_update_delete_url = reverse("chrisfile-detail", kwargs={"pk": userfile.id})
        response = self.client.get(read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        userfile.delete()

    def test_filebrowserfile_update_success(self):
        self.client.login(username=self.username, password=self.password)
        new_file_path = f'home/{self.username}/uploads/mytestfolder/mytestfile.txt'
        put = json.dumps({
            "template": {"data": [{"name": "public", "value": True},
                                  {"name": "new_file_path", "value": new_file_path}]}})
        response = self.client.put(self.read_update_delete_url, data=put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["public"],True)
        self.assertEqual(response.data["fname"], new_file_path)
        self.assertTrue(self.storage_manager.obj_exists(new_file_path))
        self.assertFalse(self.storage_manager.obj_exists(self.upload_path))
        self.file.refresh_from_db()
        self.file.remove_public_link()
        self.file.remove_public_access()

    def test_filebrowserfile_update_failure_unauthenticated(self):
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfile_update_failure_user_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_filebrowserfile_delete_success(self):
        # create a file
        upload_path = f'home/{self.username}/uploads/mytestfile.txt'

        with io.StringIO("test file") as f:
            self.storage_manager.upload_obj(upload_path, f.read(),
                                         content_type='text/plain')
        user = User.objects.get(username=self.username)

        folder_path = os.path.dirname(upload_path)
        (file_parent_folder, _) = ChrisFolder.objects.get_or_create(path=folder_path,
                                                                    owner=user)
        f = UserFile(owner=user, parent_folder=file_parent_folder)
        f.fname.name = upload_path
        f.save()

        read_update_delete_url = reverse("chrisfile-detail",
                                         kwargs={"pk": f.id})

        self.client.login(username=self.username, password=self.password)
        response = self.client.delete(read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_filebrowserfile_delete_failure_unauthenticated(self):
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfile_delete_failure_user_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class FileBrowserFileResourceViewTests(FileBrowserViewTests):
    """
    Test the chrisfile-resource view.
    """

    def setUp(self):
        super(FileBrowserFileResourceViewTests, self).setUp()

        # create compute resource
        (compute_resource, tf) = ComputeResource.objects.get_or_create(
            name="host", compute_url=COMPUTE_RESOURCE_URL)

        # create 'fs' plugin
        (pl_meta, tf) = PluginMeta.objects.get_or_create(name='pacspull', type='fs')
        (plugin, tf) = Plugin.objects.get_or_create(meta=pl_meta, version='0.1')
        plugin.compute_resources.set([compute_resource])
        plugin.save()

        self.plugin = plugin
        user = User.objects.get(username=self.username)

        # create a feed by creating a "fs" plugin instance
        self.pl_inst = PluginInstance.objects.create(plugin=plugin, owner=user,
                                                title='test',
                                                compute_resource=
                                                plugin.compute_resources.all()[0])

        # create a file in the DB "already uploaded" to the server)
        self.storage_manager = connect_storage(settings)

        # upload file to storage
        self.upload_path = f'home/{self.username}/uploads/file2.txt'
        with io.StringIO("test file") as file1:
            self.storage_manager.upload_obj(self.upload_path, file1.read(),
                                         content_type='text/plain')
        user = User.objects.get(username=self.username)

        folder_path = os.path.dirname(self.upload_path)
        (file_parent_folder, _) = ChrisFolder.objects.get_or_create(path=folder_path,
                                                                    owner=user)
        self.file = UserFile(owner=user, parent_folder=file_parent_folder)
        self.file.fname.name = self.upload_path
        self.file.save()

        self.download_url = (reverse("chrisfile-resource",
                                     kwargs={"pk": self.file.id}) + 'file2.txt')

    def tearDown(self):
        self.file.delete()
        super(FileBrowserFileResourceViewTests, self).tearDown()

    def test_fileBrowserfile_resource_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.download_url)
        self.assertEqual(response.status_code, 200)
        content = [c for c in response.streaming_content][0].decode('utf-8')
        self.assertEqual(content, "test file")

    def test_fileBrowserfile_resource_success_user_chris(self):
        self.client.login(username=self.chris_username, password=self.chris_password)
        response = self.client.get(self.download_url)
        self.assertEqual(response.status_code, 200)
        content = [c for c in response.streaming_content][0].decode('utf-8')
        self.assertEqual(content, "test file")

    def test_fileBrowserfile_resource_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.download_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
    def test_fileBrowserfile_resource_failure_unauthenticated(self):
        response = self.client.get(self.download_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_fileBrowserfile_resource_success_shared_file(self):
        other_user = User.objects.get(username=self.other_username)

        # create a file in the uploads folder
        path = f'home/{self.other_username}/uploads'
        uploads_folder = ChrisFolder.objects.get(path=path)

        file_path = f'{path}/file6.txt'
        with io.StringIO("test file") as file6:
            self.storage_manager.upload_obj(file_path, file6.read(),
                                         content_type='text/plain')

        userfile = UserFile(owner=other_user, parent_folder=uploads_folder)
        userfile.fname.name = file_path
        userfile.save()

        userfile.grant_user_permission(User.objects.get(username=self.username), 'r')

        download_url = (reverse("chrisfile-resource",
                                     kwargs={"pk": userfile.id}) + 'file6.txt')

        self.client.login(username=self.username, password=self.password)
        response = self.client.get(download_url)
        self.assertEqual(response.status_code, 200)
        content = [c for c in response.streaming_content][0].decode('utf-8')
        self.assertEqual(content, "test file")

        userfile.delete()

    def test_fileBrowserfile_resource_failure_unauthorized_shared_feed_unauthenticated(self):
        other_user = User.objects.get(username=self.other_username)
        plugin = self.plugin

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=plugin, owner=other_user,
                                                title='test',
                                                compute_resource=
                                                plugin.compute_resources.all()[0])
        pl_inst.feed.name = 'shared_feed'
        pl_inst.feed.save()

        # create a file in the output folder
        file_path = f'{pl_inst.output_folder}/file6.txt'
        with io.StringIO("test file") as file6:
            self.storage_manager.upload_obj(file_path, file6.read(),
                                         content_type='text/plain')

        userfile = UserFile(owner=other_user, parent_folder=pl_inst.output_folder)
        userfile.fname.name = file_path
        userfile.save()

        # share feed
        pl_inst.feed.grant_user_permission(User.objects.get(username=self.username))

        download_url = reverse("chrisfile-resource", kwargs={"pk": userfile.id})

        response = self.client.get(download_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        userfile.delete()



class FileBrowserFileGroupPermissionListViewTests(FileBrowserViewTests):
    """
    Test the 'filegrouppermission-list' view.
    """

    def setUp(self):
        super(FileBrowserFileGroupPermissionListViewTests, self).setUp()

        user = User.objects.get(username=self.username)
        self.grp_name = 'all_users'

        # create file
        self.path = f'home/{self.username}/test7/file9.txt'
        folder = ChrisFolder.objects.create(path=f'home/{self.username}/test7', owner=user)

        self.storage_manager = connect_storage(settings)

        with io.StringIO("test file") as file9:
            self.storage_manager.upload_obj(self.path, file9.read(),
                                            content_type='text/plain')
        f = UserFile(owner=user, parent_folder=folder)
        f.fname.name = self.path
        f.save()

        self.create_read_url = reverse('filegrouppermission-list',
                                       kwargs={"pk": f.id})
        self.post = json.dumps(
            {"template":
                 {"data": [{"name": "grp_name", "value": self.grp_name},
                           {"name": "permission", "value": "r"}]}})

    def tearDown(self):
        # delete file
        f = ChrisFile.objects.get(fname=self.path)
        f.delete()

        super(FileBrowserFileGroupPermissionListViewTests, self).tearDown()

    def test_filebrowserfilegrouppermission_create_success(self):

        self.client.login(username=self.username, password=self.password)

        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        f = ChrisFile.objects.get(fname=self.path)
        self.assertIn(self.grp_name, [g.name for g in f.shared_groups.all()])

        f.remove_shared_link()

    def test_filebrowserfilegrouppermission_create_failure_unauthenticated(self):
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfilegrouppermission_create_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_filebrowserfilegrouppermission_shared_create_failure_access_denied(self):
        user = User.objects.get(username=self.other_username)

        f = ChrisFile.objects.get(fname=self.path)
        f.grant_user_permission(user, 'w')

        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_filebrowserfilegrouppermission_list_success(self):
        grp = Group.objects.get(name=self.grp_name)

        f = ChrisFile.objects.get(fname=self.path)
        f.grant_group_permission(grp, 'r')

        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.create_read_url)
        self.assertContains(response, self.grp_name)

    def test_filebrowserfilegrouppermission_list_failure_unauthenticated(self):
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfilegrouppermission_list_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_filebrowserfilegrouppermission_shared_user_list_success(self):
        user = User.objects.get(username=self.other_username)

        f = ChrisFile.objects.get(fname=self.path)
        f.grant_user_permission(user, 'w')

        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class FileBrowserFileGroupPermissionListQuerySearchViewTests(FileBrowserViewTests):
    """
    Test the 'filegrouppermission-list-query-search' view.
    """

    def setUp(self):
        super(FileBrowserFileGroupPermissionListQuerySearchViewTests, self).setUp()

        user = User.objects.get(username=self.username)
        self.grp_name = 'all_users'

        # create file
        self.path = f'home/{self.username}/test8/file10.txt'
        folder = ChrisFolder.objects.create(path=f'home/{self.username}/test8',
                                            owner=user)

        self.storage_manager = connect_storage(settings)

        with io.StringIO("test file") as file10:
            self.storage_manager.upload_obj(self.path, file10.read(),
                                            content_type='text/plain')
        f = UserFile(owner=user, parent_folder=folder)
        f.fname.name = self.path
        f.save()

        self.read_url = reverse('filegrouppermission-list-query-search',
                                kwargs={"pk": f.id})

        grp = Group.objects.get(name=self.grp_name)
        f.grant_group_permission(grp, 'r')

    def tearDown(self):
        # delete file
        f = ChrisFile.objects.get(fname=self.path)
        f.delete()

        super(FileBrowserFileGroupPermissionListQuerySearchViewTests, self).tearDown()

    def test_filebrowserfilegrouppermission_list_query_search_success(self):
        read_url = f'{self.read_url}?group_name={self.grp_name}'

        self.client.login(username=self.username, password=self.password)
        response = self.client.get(read_url)
        self.assertContains(response, self.grp_name)

    def test_filebrowserfilegrouppermission_list_query_search_success_shared(self):
        read_url = f'{self.read_url}?group_name={self.grp_name}'

        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(read_url)
        self.assertContains(response, self.grp_name)

    def test_filebrowserfilegrouppermission_list_query_search_failure_unauthenticated(self):
        read_url = f'{self.read_url}?group_name={self.grp_name}'

        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfilegrouppermission_list_query_search_failure_other_user(self):
        grp = Group.objects.get(name=self.grp_name)
        f = ChrisFile.objects.get(fname=self.path)
        f.remove_group_permission(grp, 'r')

        read_url = f'{self.read_url}?group_name={self.grp_name}'

        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['results'])


class FileBrowserFileGroupPermissionDetailViewTests(FileBrowserViewTests):
    """
    Test the filegrouppermission-detail view.
    """

    def setUp(self):
        super(FileBrowserFileGroupPermissionDetailViewTests, self).setUp()

        user = User.objects.get(username=self.username)
        self.grp_name = 'all_users'

        # create file
        self.path = f'home/{self.username}/test9/file11.txt'
        folder = ChrisFolder.objects.create(path=f'home/{self.username}/test9',
                                            owner=user)

        self.storage_manager = connect_storage(settings)

        with io.StringIO("test file") as file11:
            self.storage_manager.upload_obj(self.path, file11.read(),
                                            content_type='text/plain')
        f = UserFile(owner=user, parent_folder=folder)
        f.fname.name = self.path
        f.save()

        grp = Group.objects.get(name=self.grp_name)
        f.grant_group_permission(grp, 'r')

        gp = FileGroupPermission.objects.get(group=grp, file=f)

        self.read_update_delete_url = reverse("filegrouppermission-detail",
                                              kwargs={"pk": gp.id})

        self.put = json.dumps({
            "template": {"data": [{"name": "permission", "value": "w"}]}})


    def tearDown(self):
        # delete file
        f = ChrisFile.objects.get(fname=self.path)
        f.delete()

        super(FileBrowserFileGroupPermissionDetailViewTests, self).tearDown()

    def test_filebrowserfilegrouppermission_detail_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_update_delete_url)
        self.assertContains(response, 'all_users')
        self.assertContains(response, self.path)

    def test_filebrowserfilegrouppermission_detail_shared_success(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.read_update_delete_url)
        self.assertContains(response, 'all_users')
        self.assertContains(response, self.path)

    def test_filebrowserfilegrouppermission_detail_failure_unauthenticated(self):
        response = self.client.get(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfilegrouppermission_update_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["permission"], 'w')

    def test_filebrowserfilegrouppermission_update_failure_unauthenticated(self):
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfilegrouppermission_update_failure_user_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_filebrowserfilegrouppermission_delete_success(self):
        f = ChrisFile.objects.get(fname=self.path)
        grp = Group.objects.get(name='pacs_users')

        # create a group permission
        f.grant_group_permission(grp, 'r')
        gp = FileGroupPermission.objects.get(group=grp, file=f)

        read_update_delete_url = reverse("filegrouppermission-detail",
                                         kwargs={"pk": gp.id})
        self.client.login(username=self.username, password=self.password)
        response = self.client.delete(read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_filebrowserfilegrouppermission_delete_failure_unauthenticated(self):
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfilegrouppermission_delete_failure_user_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class FileBrowserFileUserPermissionListViewTests(FileBrowserViewTests):
    """
    Test the 'fileuserpermission-list' view.
    """

    def setUp(self):
        super(FileBrowserFileUserPermissionListViewTests, self).setUp()

        user = User.objects.get(username=self.username)

        # create file
        self.path = f'home/{self.username}/test9/file11.txt'
        folder = ChrisFolder.objects.create(path=f'home/{self.username}/test9',
                                            owner=user)

        self.storage_manager = connect_storage(settings)

        with io.StringIO("test file") as file11:
            self.storage_manager.upload_obj(self.path, file11.read(),
                                            content_type='text/plain')
        f = UserFile(owner=user, parent_folder=folder)
        f.fname.name = self.path
        f.save()

        self.create_read_url = reverse('fileuserpermission-list',
                                       kwargs={"pk": f.id})
        self.post = json.dumps(
            {"template":
                 {"data": [{"name": "username", "value": self.other_username},
                           {"name": "permission", "value": "r"}]}})

    def tearDown(self):
        # delete file
        f = ChrisFile.objects.get(fname=self.path)
        f.delete()

        super(FileBrowserFileUserPermissionListViewTests, self).tearDown()

    def test_filebrowserfileuserpermission_create_success(self):
        user = User.objects.get(username=self.username)

        self.client.login(username=self.username, password=self.password)

        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        f = ChrisFile.objects.get(fname=self.path)
        self.assertIn(self.other_username, [u.username for u in f.shared_users.all()])

        f.remove_shared_link()

    def test_filebrowserfileuserpermission_create_failure_unauthenticated(self):
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfileuserpermission_create_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_filebrowserfileuserpermission_shared_create_failure_access_denied(self):
        user = User.objects.get(username=self.other_username)
        f = ChrisFile.objects.get(fname=self.path)
        f.grant_user_permission(user, 'w')

        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_filebrowserfileuserpermission_list_success(self):
        user = User.objects.get(username=self.other_username)
        f = ChrisFile.objects.get(fname=self.path)
        f.grant_user_permission(user, 'r')

        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.create_read_url)
        self.assertContains(response, self.other_username)

    def test_filebrowserfileuserpermission_list_failure_unauthenticated(self):
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfileuserpermission_list_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_filebrowserfileuserpermission_shared_user_list_success(self):
        user = User.objects.get(username=self.other_username)
        f = ChrisFile.objects.get(fname=self.path)
        f.grant_user_permission(user, 'w')

        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class FileBrowserFileUserPermissionListQuerySearchViewTests(FileBrowserViewTests):
    """
    Test the 'fileuserpermission-list-query-search' view.
    """

    def setUp(self):
        super(FileBrowserFileUserPermissionListQuerySearchViewTests, self).setUp()

        user = User.objects.get(username=self.username)

        # create file
        self.path = f'home/{self.username}/test10/file12.txt'
        folder = ChrisFolder.objects.create(path=f'home/{self.username}/test10',
                                            owner=user)

        self.storage_manager = connect_storage(settings)

        with io.StringIO("test file") as file12:
            self.storage_manager.upload_obj(self.path, file12.read(),
                                            content_type='text/plain')
        f = UserFile(owner=user, parent_folder=folder)
        f.fname.name = self.path
        f.save()

        self.read_url = reverse('fileuserpermission-list-query-search',
                                kwargs={"pk": f.id})

        other_user = User.objects.get(username=self.other_username)
        f.grant_user_permission(other_user, 'r')

    def tearDown(self):
        # delete file
        f = ChrisFile.objects.get(fname=self.path)
        f.delete()

        super(FileBrowserFileUserPermissionListQuerySearchViewTests, self).tearDown()

    def test_filebrowserfileuserpermission_list_query_search_success(self):
        read_url = f'{self.read_url}?username={self.other_username}'

        self.client.login(username=self.username, password=self.password)
        response = self.client.get(read_url)
        self.assertContains(response, self.other_username)

    def test_filebrowserfileuserpermission_list_query_search_success_shared(self):
        read_url = f'{self.read_url}?username={self.other_username}'

        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(read_url)
        self.assertContains(response, self.other_username)

    def test_filebrowserfileuserpermission_list_query_search_failure_unauthenticated(self):
        read_url = f'{self.read_url}?username={self.other_username}'

        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class FileBrowserFileUserPermissionDetailViewTests(FileBrowserViewTests):
    """
    Test the fileuserpermission-detail view.
    """

    def setUp(self):
        super(FileBrowserFileUserPermissionDetailViewTests, self).setUp()

        user = User.objects.get(username=self.username)

        # create file
        self.path = f'home/{self.username}/test10/file12.txt'
        folder = ChrisFolder.objects.create(path=f'home/{self.username}/test10',
                                            owner=user)

        self.storage_manager = connect_storage(settings)

        with io.StringIO("test file") as file12:
            self.storage_manager.upload_obj(self.path, file12.read(),
                                            content_type='text/plain')
        f = UserFile(owner=user, parent_folder=folder)
        f.fname.name = self.path
        f.save()

        other_user = User.objects.get(username=self.other_username)
        f.grant_user_permission(other_user, 'r')

        up = FileUserPermission.objects.get(user=other_user, file=f)

        self.read_update_delete_url = reverse("fileuserpermission-detail",
                                              kwargs={"pk": up.id})

        self.put = json.dumps({
            "template": {"data": [{"name": "permission", "value": "w"}]}})


    def tearDown(self):
        # delete file
        f = ChrisFile.objects.get(fname=self.path)
        f.delete()

        super(FileBrowserFileUserPermissionDetailViewTests, self).tearDown()

    def test_filebrowserfileuserpermission_detail_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_update_delete_url)
        self.assertContains(response, self.other_username)
        self.assertContains(response, self.path)

    def test_filebrowserfileuserpermission_detail_shared_success(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.read_update_delete_url)
        self.assertContains(response, self.other_username)
        self.assertContains(response, self.path)

    def test_filebrowserfileuserpermission_detail_failure_unauthenticated(self):
        response = self.client.get(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfileuserpermission_update_success(self):
        user = User.objects.get(username=self.username)

        self.client.login(username=self.username, password=self.password)
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["permission"], 'w')

    def test_filebrowserfileuserpermission_update_failure_unauthenticated(self):
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfileuserpermission_update_failure_user_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_filebrowserfileuserpermission_delete_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_filebrowserfileuserpermission_delete_failure_unauthenticated(self):
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfileuserpermission_delete_failure_user_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class FileBrowserFolderLinkFileListViewTests(FileBrowserViewTests):
    """
    Test the 'chrisfolder-linkfile-list' view.
    """

    def setUp(self):
        super(FileBrowserFolderLinkFileListViewTests, self).setUp()

        # create compute resource
        (compute_resource, tf) = ComputeResource.objects.get_or_create(
            name="host", compute_url=COMPUTE_RESOURCE_URL)

        # create 'fs' plugin
        (pl_meta, tf) = PluginMeta.objects.get_or_create(name='pacspull', type='fs')
        (plugin, tf) = Plugin.objects.get_or_create(meta=pl_meta, version='0.1')
        plugin.compute_resources.set([compute_resource])
        plugin.save()

        self.plugin = plugin

        # create link file
        self.link_path = f'home/{self.username}/feeds/feed_1/out/SERVICES_PACS.chrislink'
        user = User.objects.get(username=self.username)

        folder_path = os.path.dirname(self.link_path)
        (parent_folder, _) = ChrisFolder.objects.get_or_create(path=folder_path,
                                                               owner=user)
        self.link_file = ChrisLinkFile(path='SERVICES/PACS', owner=user,
                                       parent_folder=parent_folder)
        self.link_file.save(name='SERVICES_PACS')
        self.read_url = reverse("chrisfolder-linkfile-list",
                                kwargs={"pk": parent_folder.id})

    def tearDown(self):
        self.link_file.delete()
        super(FileBrowserFolderLinkFileListViewTests, self).tearDown()

    def test_filebrowserfolderlinkfile_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_url)
        self.assertContains(response, 'file_resource')
        self.assertContains(response, self.link_path)

    def test_filebrowserfolderlinkfile_list_success_public_feed_unauthenticated(self):
        user = User.objects.get(username=self.username)

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=self.plugin, owner=user,
                                                title='test',
                                                compute_resource=
                                                self.plugin.compute_resources.all()[0])

        # create link file
        link_path = f'{pl_inst.output_folder.path}/SERVICES_PACS.chrislink'

        link_file = ChrisLinkFile(path='SERVICES/PACS', owner=user,
                                  parent_folder=pl_inst.output_folder)
        link_file.save(name='SERVICES_PACS')

        # make feed public
        pl_inst.feed.grant_public_access()

        read_url = reverse("chrisfolder-linkfile-list",
                           kwargs={"pk": pl_inst.output_folder.id})
        response = self.client.get(read_url)
        self.assertContains(response, 'file_resource')
        self.assertContains(response, link_path)

        pl_inst.feed.remove_public_access()
        link_file.delete()

    def test_filebrowserfolderlinkfile_list_failure_unauthenticated(self):
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserfolderlinkfile_list_failure_not_found(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(reverse("chrisfolder-linkfile-list",
                                           kwargs={"pk": 111111111}))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_filebrowserfolderlinkfile_list_file_folder_success(self):
        folder_path = os.path.dirname(self.link_path)
        folder = ChrisFolder.objects.get(path=folder_path)
        read_url = reverse("chrisfolder-linkfile-list", kwargs={"pk": folder.id})
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(read_url)
        self.assertContains(response, self.link_path)

    def test_fileBrowserlinkfile_list_success_shared_feed(self):
        other_user = User.objects.get(username=self.other_username)
        plugin = self.plugin

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=plugin, owner=other_user,
                                                title='test',
                                                compute_resource=
                                                plugin.compute_resources.all()[0])
        pl_inst.feed.name = 'shared_feed'
        pl_inst.feed.save()

        # create link file in the output folder
        link_path = f'{pl_inst.output_folder.path}/SERVICES_PACS.chrislink'

        link_file = ChrisLinkFile(path='SERVICES/PACS', owner=other_user,
                                  parent_folder=pl_inst.output_folder)
        link_file.save(name='SERVICES_PACS')

        # share feed
        pl_inst.feed.grant_user_permission(User.objects.get(username=self.username))

        read_url = reverse("chrisfolder-linkfile-list", kwargs={"pk": pl_inst.output_folder.id})
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(read_url)
        self.assertContains(response, link_path)

        link_file.delete()

    def test_fileBrowserlinkfile_list_failure_shared_feed_unauthenticated(self):
        other_user = User.objects.get(username=self.other_username)
        plugin = self.plugin

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=plugin, owner=other_user,
                                                title='test',
                                                compute_resource=
                                                plugin.compute_resources.all()[0])
        pl_inst.feed.name = 'shared_feed'
        pl_inst.feed.save()

        # create link file in the output folder
        link_file = ChrisLinkFile(path='SERVICES/PACS', owner=other_user,
                                  parent_folder=pl_inst.output_folder)
        link_file.save(name='SERVICES_PACS')

        # share feed
        pl_inst.feed.grant_user_permission(User.objects.get(username=self.username))

        read_url = reverse("chrisfolder-linkfile-list", kwargs={"pk": pl_inst.output_folder.id})
        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        link_file.delete()

class FileBrowserLinkFileDetailViewTests(FileBrowserViewTests):
    """
    Test the chrislinkfile-detail view.
    """

    def setUp(self):
        super(FileBrowserLinkFileDetailViewTests, self).setUp()

        self.storage_manager = connect_storage(settings)

        # create compute resource
        (compute_resource, tf) = ComputeResource.objects.get_or_create(
            name="host", compute_url=COMPUTE_RESOURCE_URL)

        # create 'fs' plugin
        (pl_meta, tf) = PluginMeta.objects.get_or_create(name='pacspull', type='fs')
        (plugin, tf) = Plugin.objects.get_or_create(meta=pl_meta, version='0.1')
        plugin.compute_resources.set([compute_resource])
        plugin.save()

        self.plugin = plugin
        user = User.objects.get(username=self.username)

        # create a feed by creating a "fs" plugin instance
        self.pl_inst = PluginInstance.objects.create(plugin=plugin, owner=user,
                                                title='test',
                                                compute_resource=
                                                plugin.compute_resources.all()[0])

        # create link file
        self.link_path = f'{self.pl_inst.output_folder.path}/SERVICES_PACS.chrislink'

        self.link_file = ChrisLinkFile(path='SERVICES/PACS', owner=user,
                                  parent_folder=self.pl_inst.output_folder)
        self.link_file.save(name='SERVICES_PACS')

        self.read_update_delete_url = reverse("chrislinkfile-detail",
                                              kwargs={"pk": self.link_file.id})
        self.put = json.dumps({
            "template": {"data": [{"name": "public", "value": True}]}})

    def tearDown(self):
        self.link_file.delete()
        super(FileBrowserLinkFileDetailViewTests, self).tearDown()

    def test_fileBrowserlinkfile_detail_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_update_delete_url)
        self.assertContains(response, self.link_path)

    def test_fileBrowserlinkfile_detail_success_user_chris(self):
        self.client.login(username=self.chris_username, password=self.chris_password)
        response = self.client.get(self.read_update_delete_url)
        self.assertContains(response, self.link_path)

    def test_fileBrowserlinkfile_detail_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
    def test_fileBrowserlinkfile_detail_failure_unauthenticated(self):
        response = self.client.get(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_fileBrowserlinkfile_detail_success_public_feed_unauthenticated(self):
        self.pl_inst.feed.grant_public_access()
        response = self.client.get(self.read_update_delete_url)
        self.assertContains(response, self.link_path)
        self.pl_inst.feed.remove_public_access()

    def test_fileBrowserlinkfile_detail_success_shared_feed(self):
        other_user = User.objects.get(username=self.other_username)
        plugin = self.plugin

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=plugin, owner=other_user,
                                                title='test',
                                                compute_resource=
                                                plugin.compute_resources.all()[0])
        pl_inst.feed.name = 'shared_feed'
        pl_inst.feed.save()

        # create link file in the output folder
        link_path = f'{pl_inst.output_folder.path}/SERVICES_PACS.chrislink'

        link_file = ChrisLinkFile(path='SERVICES/PACS', owner=other_user,
                                  parent_folder=pl_inst.output_folder)
        link_file.save(name='SERVICES_PACS')

        # share feed
        user = User.objects.get(username=self.username)
        pl_inst.feed.grant_user_permission(user)

        read_update_delete_url = reverse("chrislinkfile-detail",
                                         kwargs={"pk": link_file.id})
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(read_update_delete_url)
        self.assertContains(response, link_path)

        link_file.delete()

    def test_fileBrowserlinkfile_detail_failure_unauthorized_shared_feed_unauthenticated(self):
        other_user = User.objects.get(username=self.other_username)
        plugin = self.plugin

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=plugin, owner=other_user,
                                                title='test',
                                                compute_resource=
                                                plugin.compute_resources.all()[0])
        pl_inst.feed.name = 'shared_feed'
        pl_inst.feed.save()

        # create link file in the output folder

        link_file = ChrisLinkFile(path='SERVICES/PACS', owner=other_user,
                                  parent_folder=pl_inst.output_folder)
        link_file.save(name='SERVICES_PACS')

        # share feed
        pl_inst.feed.grant_user_permission(User.objects.get(username=self.username))

        read_update_delete_url = reverse("chrislinkfile-detail",
                                         kwargs={"pk": link_file.id})
        response = self.client.get(read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        link_file.delete()

    def test_filebrowserlinkfile_update_success(self):
        self.client.login(username=self.username, password=self.password)
        new_file_path = f'home/{self.username}/uploads/mytestfolder/SERVICES_PACS.chrislink'

        put = json.dumps({
            "template": {"data": [{"name": "public", "value": True},
                                  {"name": "new_link_file_path", "value": new_file_path}]}})

        response = self.client.put(self.read_update_delete_url, data=put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["public"],True)
        self.assertEqual(response.data["fname"], new_file_path)
        self.assertTrue(self.storage_manager.obj_exists(new_file_path))
        self.assertFalse(self.storage_manager.obj_exists(self.link_path))
        self.link_file.refresh_from_db()
        self.link_file.remove_public_link()
        self.link_file.remove_public_access()

    def test_filebrowserlinkfile_update_failure_unauthenticated(self):
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserlinkfile_update_failure_user_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_filebrowserlinkfile_delete_success(self):
        user = User.objects.get(username=self.username)

        # create link file
        lf_path = f'home/{self.username}/uploads/SERVICES_PACS.chrislink'
        (parent_folder, _) = ChrisFolder.objects.get_or_create(owner=user,
                                                               path=f'home/{self.username}/uploads')
        lf = ChrisLinkFile(path='SERVICES/PACS', owner=user, parent_folder=parent_folder)
        lf.save(name='SERVICES_PACS')

        read_update_delete_url = reverse("chrislinkfile-detail",
                                         kwargs={"pk": lf.id})

        self.client.login(username=self.username, password=self.password)
        response = self.client.delete(read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(self.storage_manager.obj_exists(lf_path))

    def test_filebrowserlinkfile_delete_failure_unauthenticated(self):
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filebrowserlinkfile_delete_failure_user_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

class FileBrowserLinkFileResourceViewTests(FileBrowserViewTests):
    """
    Test the chrislinkfile-resource view.
    """

    def setUp(self):
        super(FileBrowserLinkFileResourceViewTests, self).setUp()

        # create compute resource
        (compute_resource, tf) = ComputeResource.objects.get_or_create(
            name="host", compute_url=COMPUTE_RESOURCE_URL)

        # create 'fs' plugin
        (pl_meta, tf) = PluginMeta.objects.get_or_create(name='pacspull', type='fs')
        (plugin, tf) = Plugin.objects.get_or_create(meta=pl_meta, version='0.1')
        plugin.compute_resources.set([compute_resource])
        plugin.save()

        self.plugin = plugin
        user = User.objects.get(username=self.username)

        # create a feed by creating a "fs" plugin instance
        self.pl_inst = PluginInstance.objects.create(plugin=plugin, owner=user,
                                                title='test',
                                                compute_resource=
                                                plugin.compute_resources.all()[0])

        # create link file
        self.link_file = ChrisLinkFile(path='SERVICES/PACS', owner=user,
                                  parent_folder=self.pl_inst.output_folder)
        self.link_file.save(name='SERVICES_PACS')

        self.download_url = (reverse("chrislinkfile-resource",
                                    kwargs={"pk": self.link_file.id}) +
                             'SERVICES_PACS.chrislink')

    def tearDown(self):
        self.link_file.delete()
        super(FileBrowserLinkFileResourceViewTests, self).tearDown()

    def test_fileBrowserlinkfile_resource_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.download_url)
        self.assertEqual(response.status_code, 200)
        content = [c for c in response.streaming_content][0].decode('utf-8')
        self.assertEqual(content, 'SERVICES/PACS')

    def test_fileBrowserlinkfile_resource_success_user_chris(self):
        self.client.login(username=self.chris_username, password=self.chris_password)
        response = self.client.get(self.download_url)
        self.assertEqual(response.status_code, 200)
        content = [c for c in response.streaming_content][0].decode('utf-8')
        self.assertEqual(content, 'SERVICES/PACS')

    def test_fileBrowserlinkfile_resource_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.download_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
    def test_fileBrowserlinkfile_resource_failure_unauthenticated(self):
        response = self.client.get(self.download_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_fileBrowserlinkfile_resource_success_public_feed_unauthenticated(self):
        self.pl_inst.feed.grant_public_access()
        response = self.client.get(self.download_url)
        self.assertEqual(response.status_code, 200)
        content = [c for c in response.streaming_content][0].decode('utf-8')
        self.assertEqual(content, 'SERVICES/PACS')
        self.pl_inst.feed.remove_public_access()

    def test_fileBrowserlinkfile_resource_success_shared_link_file(self):
        other_user = User.objects.get(username=self.other_username)

        # create link file in the uploads folder
        path = f'home/{self.other_username}/uploads'
        uploads_folder = ChrisFolder.objects.get(path=path)

        link_file = ChrisLinkFile(path='SERVICES/PACS', owner=other_user,
                                  parent_folder=uploads_folder)
        link_file.save(name='SERVICES_PACS')

        link_file.grant_user_permission(User.objects.get(username=self.username), 'r')

        download_url = reverse("chrislinkfile-resource",
                               kwargs={"pk": link_file.id}) + 'SERVICES_PACS.chrislink'

        self.client.login(username=self.username, password=self.password)
        response = self.client.get(download_url)
        self.assertEqual(response.status_code, 200)
        content = [c for c in response.streaming_content][0].decode('utf-8')
        self.assertEqual(content, 'SERVICES/PACS')

        link_file.delete()

    def test_fileBrowserlinkfile_resource_failure_unauthorized_shared_feed_unauthenticated(self):
        other_user = User.objects.get(username=self.other_username)
        plugin = self.plugin

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=plugin, owner=other_user,
                                                title='test',
                                                compute_resource=
                                                plugin.compute_resources.all()[0])
        pl_inst.feed.name = 'shared_feed'
        pl_inst.feed.save()


        # create link file in the output folder
        link_file = ChrisLinkFile(path='SERVICES/PACS', owner=other_user,
                                  parent_folder=pl_inst.output_folder)
        link_file.save(name='SERVICES_PACS')

        # share feed
        pl_inst.feed.grant_user_permission(User.objects.get(username=self.username))

        download_url = reverse("chrislinkfile-resource",
                               kwargs={"pk": link_file.id}) + 'SERVICES_PACS.chrislink'

        response = self.client.get(download_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        link_file.delete()
