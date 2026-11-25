
import logging
import json
import io
import os
from unittest import mock

from django.test import TestCase, tag
from django.conf import settings
from django.contrib.auth.models import User
from django.utils import timezone
from django.urls import reverse

from rest_framework import status
import jwt

from core.models import ChrisFolder, FileDownloadToken
from core.storage.helpers import connect_storage, mock_storage
from core.utils import download_token_signing_key
from userfiles.models import UserFile
from userfiles import views


CHRIS_SUPERUSER_PASSWORD = settings.CHRIS_SUPERUSER_PASSWORD


class UserFileViewTests(TestCase):
    """
    Generic userfile view tests' setup and tearDown.
    """

    def setUp(self):
        # avoid cluttered console output (for instance logging all the http requests)
        logging.disable(logging.WARNING)

        # create superuser chris (owner of root folders)
        self.chris_username = 'chris'
        self.chris_password = CHRIS_SUPERUSER_PASSWORD

        self.content_type = 'application/vnd.collection+json'

        self.username = 'test'
        self.password = 'testpass'
        self.other_username = 'boo'
        self.other_password = 'far'

        # create users
        User.objects.create_user(username=self.other_username,
                                 password=self.other_password)
        user = User.objects.create_user(username=self.username,
                                 password=self.password)

        # create a file in the DB "already uploaded" to the server)
        self.storage_manager = connect_storage(settings)
        # upload file to storage
        self.upload_path = f'home/{self.username}/uploads/file1.txt'
        with io.StringIO("test file") as file1:
            self.storage_manager.upload_obj(self.upload_path, file1.read(),
                                          content_type='text/plain')
            folder_path = os.path.dirname(self.upload_path)
            (file_parent_folder, _) = ChrisFolder.objects.get_or_create(path=folder_path,
                                                                        owner=user)
            self.userfile = UserFile(owner=user, parent_folder=file_parent_folder)
            self.userfile.fname.name = self.upload_path
            self.userfile.save()

    def tearDown(self):
        # delete file from storage
        self.storage_manager.delete_obj(self.upload_path)
        # re-enable logging
        logging.disable(logging.NOTSET)


class UserFileListViewTests(UserFileViewTests):
    """
    Test the userfile-list view.
    """

    def setUp(self):
        super(UserFileListViewTests, self).setUp()
        self.create_read_url = reverse("userfile-list")

    def tearDown(self):
        super(UserFileListViewTests, self).tearDown()

    @tag('integration')
    def test_integration_userfile_create_success(self):

        # POST request using multipart/form-data to be able to upload file
        self.client.login(username=self.username, password=self.password)
        upload_path = f'home/{self.username}/uploads/file2.txt'

        with io.StringIO("test file") as f:
            post = {"fname": f, "upload_path": upload_path}
            response = self.client.post(self.create_read_url, data=post)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # delete file from storage
        self.storage_manager.delete_obj(upload_path)

    def test_userfile_create_failure_unauthenticated(self):
        upload_path = f'home/{self.username}/uploads/file2.txt'
        response = self.client.post(self.create_read_url,
                                    data={"fname": {}, "upload_path": upload_path})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_userfile_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.create_read_url)
        self.assertContains(response, "file1.txt")

    def test_userfile_list_failure_unauthenticated(self):
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class UserFileDetailViewTests(UserFileViewTests):
    """
    Test the userfile-detail view.
    """

    def setUp(self):
        super(UserFileDetailViewTests, self).setUp()
        self.read_update_delete_url = reverse("userfile-detail",
                                              kwargs={"pk": self.userfile.id})
        upload_path = f'home/{self.username}/uploads/myfolder/myfile1.txt'
        self.put = json.dumps({
            "template": {"data": [{"name": "upload_path", "value": upload_path}]}})

    def test_userfile_detail_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_update_delete_url)
        self.assertContains(response, "file1.txt")

    def test_userfile_detail_success_user_chris(self):
        self.client.login(username=self.chris_username, password=self.chris_password)
        response = self.client.get(self.read_update_delete_url)
        self.assertContains(response, "file1.txt")

    def test_userfile_detail_failure_unauthenticated(self):
        response = self.client.get(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_userfile_update_failure_unauthenticated(self):
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_userfile_update_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_userfile_delete_success(self):
        self.client.login(username=self.username, password=self.password)

        storage_path = self.userfile.fname.name
        storage_manager_mock = mock.Mock()
        storage_manager_mock.delete_obj = mock.Mock()

        with mock.patch('core.models.connect_storage') as connect_storage_mock:
            connect_storage_mock.return_value=storage_manager_mock
            response = self.client.delete(self.read_update_delete_url)
            self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
            self.assertEqual(UserFile.objects.count(), 0)
            connect_storage_mock.assert_called_with(settings)
            storage_manager_mock.delete_obj.assert_called_with(storage_path)

    def test_userfile_delete_failure_unauthenticated(self):
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_userfile_delete_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class UserFileResourceViewTests(UserFileViewTests):
    """
    Test the userfile-resource view.
    """

    def setUp(self):
        super(UserFileResourceViewTests, self).setUp()
        self.download_url = reverse("userfile-resource",
                                    kwargs={"pk": self.userfile.id}) + 'file1.txt'

    def tearDown(self):
        super(UserFileResourceViewTests, self).tearDown()

    def test_userfileresource_get(self):
        userfile = self.userfile
        fileresource_view_inst = mock.Mock()
        fileresource_view_inst.get_object = mock.Mock(return_value=userfile)
        request_mock = mock.Mock()
        with mock.patch('userfiles.views.FileResponse') as response_mock:
            views.UserFileResource.get(fileresource_view_inst, request_mock)
            response_mock.assert_called_with(userfile.fname)

    @tag('integration')
    def test_integration_userfileresource_download_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.download_url)
        self.assertEqual(response.status_code, 200)
        content = [c for c in response.streaming_content][0].decode('utf-8')
        self.assertEqual(content, "test file")

    @tag('integration')
    def test_integration_userfileresource_download_with_token_success(self):
        user = User.objects.get(username=self.chris_username)
        dt = timezone.now() + timezone.timedelta(minutes=10)
        token = jwt.encode({'user': user.username, 'exp': dt},
                           download_token_signing_key(), algorithm='HS512')
        FileDownloadToken.objects.get_or_create(token=token, owner=user)
        response = self.client.get(f'{self.download_url}?download_token={token}')
        self.assertEqual(response.status_code, 200)
        content = [c for c in response.streaming_content][0].decode('utf-8')
        self.assertEqual(content, "test file")

    def test_fileresource_download_failure_not_related_feed_owner(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.download_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_fileresource_download_failure_unauthenticated(self):
        response = self.client.get(self.download_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
