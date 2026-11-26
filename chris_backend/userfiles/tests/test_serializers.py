
import logging
import os
from unittest import mock

from django.test import TestCase, tag
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.conf import settings
from rest_framework import serializers

from core.models import ChrisFile, ChrisFolder
from userfiles.models import UserFile
from userfiles.serializers import UserFileSerializer


CHRIS_SUPERUSER_PASSWORD = settings.CHRIS_SUPERUSER_PASSWORD


class UserFileSerializerTests(TestCase):

    def setUp(self):
        # avoid cluttered console output (for instance logging all the http requests)
        logging.disable(logging.WARNING)

        # create superuser chris (owner of root folders)
        self.chris_username = 'chris'
        self.chris_password = CHRIS_SUPERUSER_PASSWORD

        self.username = 'test'
        self.password = 'testpass'

        # create user and its home folder
        user = User.objects.create_user(username=self.username,
                                        password=self.password)
        ChrisFolder.objects.get_or_create(path=f'home/{self.username}', owner=user)

    def tearDown(self):
        # re-enable logging
        logging.disable(logging.NOTSET)

    def test_create(self):
        """
        Test whether overriden 'create' method successfully creates a new UserFile with
        the correct path, parent folder and permissions.
        """
        chris_user = User.objects.get(username=self.chris_username)
        user = User.objects.get(username=self.username)
        ancestor_folder_path = f'home/{self.username}/uploads/ancestor'
        (ancestor_folder, _) = ChrisFolder.objects.get_or_create(path=ancestor_folder_path,
                                                               owner=user)
        ancestor_folder.grant_public_access()
        ancestor_folder.grant_user_permission(chris_user, 'w')

        f = ContentFile('Test file'.encode())
        f.name = 'file1.txt'
        validated_data = {'upload_path': ancestor_folder_path + '/upload_folder/file1.txt',
                          'owner': user, 'fname': f}

        userfiles_serializer = UserFileSerializer()
        user_file = userfiles_serializer.create(validated_data)

        self.assertEqual(user_file.fname.name, ancestor_folder_path + '/upload_folder/file1.txt')
        self.assertEqual(user_file.parent_folder.path, ancestor_folder_path + '/upload_folder')
        self.assertTrue(user_file.public)
        self.assertTrue(user_file.has_user_permission(chris_user, 'w'))
        user_file.delete()

    def test_update(self):
        """
        Test whether overriden 'update' method successfully moves a UserFile to the
        correct path.
        """
        owner = User.objects.get(username=self.username)
        upload_path = f'home/{self.username}/uploads/file1.txt'
        folder_path = os.path.dirname(upload_path)
        (parent_folder, _) = ChrisFolder.objects.get_or_create(path=folder_path,
                                                               owner=owner)
        user_file = UserFile(parent_folder=parent_folder, owner=owner)
        user_file.fname.name = upload_path
        user_file.save()

        userfiles_serializer = UserFileSerializer()
        validated_data = {'upload_path': f'home/{self.username}/tests/file1.txt'}

        user_file.move = mock.Mock()
        user_file = userfiles_serializer.update(user_file, validated_data)
        user_file.move.assert_called_with(f'home/{self.username}/tests/file1.txt')

    def test_validate_upload_path_failure_contains_commas(self):
        """
        Test whether overriden validate_upload_path method validates submitted path
        does not contain commas.
        """
        userfiles_serializer = UserFileSerializer()
        request = mock.Mock()
        request.user = User.objects.get(username=self.username)

        with mock.patch.dict(userfiles_serializer.context,
                             {'request': request}, clear=True):
            with self.assertRaises(serializers.ValidationError):
                upload_path = f'home/{self.username}/uploads/fol,der/fil,e1.txt'
                userfiles_serializer.validate_upload_path(upload_path)

    def test_validate_upload_path_failure_uploading_link_file(self):
        """
        Test whether overriden validate_upload_path method validates submitted path
        does not end with the .chrislink string.
        """
        userfiles_serializer = UserFileSerializer()
        request = mock.Mock()
        request.user = User.objects.get(username=self.username)
        with mock.patch.dict(userfiles_serializer.context,
                             {'request': request}, clear=True):
            with self.assertRaises(serializers.ValidationError):
                upload_path = f'home/{self.username}/uploads/mylink.chrislink'
                userfiles_serializer.validate_upload_path(upload_path)

    def test_validate_upload_path_failure_does_not_start_with_home(self):
        """
        Test whether overriden validate_upload_path method validates submitted path
        must start with the 'home/' string.
        """
        userfiles_serializer = UserFileSerializer()
        request = mock.Mock()
        request.user = User.objects.get(username=self.username)
        with mock.patch.dict(userfiles_serializer.context,
                             {'request': request}, clear=True):
            with self.assertRaises(serializers.ValidationError):
                userfiles_serializer.validate_upload_path('home')
            with self.assertRaises(serializers.ValidationError):
                userfiles_serializer.validate_upload_path('random/file1.txt')

    def test_validate_upload_path_failure_does_not_have_write_permission(self):
        """
        Test whether overriden validate_upload_path method validates submitted path
        must start with the 'home/<username>/' string.
        """
        userfiles_serializer = UserFileSerializer()
        request = mock.Mock()
        request.user = User.objects.get(username=self.username)
        with mock.patch.dict(userfiles_serializer.context,
                             {'request': request}, clear=True):
            with self.assertRaises(serializers.ValidationError):
                userfiles_serializer.validate_upload_path('SERVICES/PACS/random/file1.txt')

    def test_validate_upload_path_failure_path_not_plain(self):
        """
        Test whether overriden validate_upload_path method refuses a path with an empty,
        '.' or '..' name in it.
        """
        userfiles_serializer = UserFileSerializer()
        request = mock.Mock()
        request.user = User.objects.get(username=self.username)

        with mock.patch.dict(userfiles_serializer.context, {'request': request},
                             clear=True):
            for path in (f'home/{self.username}/uploads/../../chris/uploads/x.txt',
                         f'home/{self.username}/uploads/./x.txt'):
                with self.subTest(path=path), \
                        self.assertRaisesRegex(serializers.ValidationError, 'cannot be'):
                    userfiles_serializer.validate_upload_path(path)

    def test_validate_upload_path_failure_path_in_use(self):
        """
        Test whether overriden validate_upload_path method refuses a path that a file
        already has, but for that file itself.
        """
        user = User.objects.get(username=self.username)
        (folder, _) = ChrisFolder.objects.get_or_create(
            path=f'home/{self.username}/uploads', owner=user)
        taken = ChrisFile(owner=user, parent_folder=folder)
        taken.fname.name = f'home/{self.username}/uploads/taken.txt'
        taken.save()
        request = mock.Mock()
        request.user = user

        for serializer, refused in ((UserFileSerializer(), True),
                                    (UserFileSerializer(instance=taken), False)):
            with self.subTest(update=not refused), \
                    mock.patch.dict(serializer.context, {'request': request}, clear=True):
                if refused:
                    with self.assertRaisesRegex(serializers.ValidationError,
                                                'already has path'):
                        serializer.validate_upload_path(taken.fname.name)
                else:
                    self.assertEqual(serializer.validate_upload_path(taken.fname.name),
                                     taken.fname.name)

    def test_validate_upload_path_success(self):
        """
        Test whether overriden validate_upload_path method validates submitted path.
        """
        userfiles_serializer = UserFileSerializer()
        request = mock.Mock()
        request.user = User.objects.get(username=self.username)

        with mock.patch.dict(userfiles_serializer.context,
                             {'request': request}, clear=True):
            upload_path = f'home/{self.username}/uploads/file1.txt'
            self.assertEqual(userfiles_serializer.validate_upload_path(upload_path),
                             upload_path)

    def test_validate_failure_missing_required_fields_on_create(self):
        """
        Test whether overriden validate method validates that required fields are in the
        passed data on create.
        """
        userfiles_serializer = UserFileSerializer()

        data = {'upload_path': f'home/{self.username}/uploads/file1.txt'}
        with self.assertRaises(serializers.ValidationError):
            userfiles_serializer.validate(data)

        f = ContentFile('Test file'.encode())
        f.name = 'file1.txt'
        data = {'fname': f}
        with self.assertRaises(serializers.ValidationError):
            userfiles_serializer.validate(data)

    def test_validate_removes_public_field_on_create(self):
        """
        Test whether overriden validate method removes the 'public' field from the
        passed data on create.
        """
        userfiles_serializer = UserFileSerializer()

        f = ContentFile('Test file'.encode())
        f.name = 'file1.txt'
        data = {'upload_path': f'home/{self.username}/uploads/file1.txt', 'fname': f,
                'public': True}
        validated_data = userfiles_serializer.validate(data)
        self.assertNotIn('public', validated_data)
