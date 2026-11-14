
import logging
import json

from django.test import TestCase, tag
from django.contrib.auth.models import User, Group
from django.urls import reverse
from django.conf import settings

from rest_framework import status

from userfiles.models import UserFile
from core.storage.helpers import mock_storage, connect_storage


CHRIS_SUPERUSER_PASSWORD = settings.CHRIS_SUPERUSER_PASSWORD


class ViewTests(TestCase):
    """
    Generic user view tests' setup and tearDown.
    """

    def setUp(self):
        # avoid cluttered console output (for instance logging all the http requests)
        logging.disable(logging.WARNING)

        # create superuser chris (owner of root folders)
        self.chris_username = 'chris'
        self.chris_password = CHRIS_SUPERUSER_PASSWORD

        self.content_type = 'application/vnd.collection+json'
        self.username = 'fooo'
        self.password = 'fooopass'
        self.email = 'foo@gmail.com'

    def tearDown(self):
        # re-enable logging
        logging.disable(logging.NOTSET)


class UserCreateViewTests(ViewTests):
    """
    Test the user-create view.
    """

    def setUp(self):
        super(UserCreateViewTests, self).setUp()
        self.create_url = reverse("user-create")
        self.post = json.dumps(
            {"template": {"data": [{"name": "username", "value": self.username},
                                   {"name": "password", "value": self.password},
                                   {"name": "email", "value": self.email}]}})

    def test_user_create_success(self):
        with mock_storage('users.models.settings') as storage_manager:
            response = self.client.post(self.create_url, data=self.post,
                                        content_type=self.content_type)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            self.assertEqual(response.data["username"], self.username)
            self.assertEqual(response.data["email"], self.email)

            welcome_file_path = f'home/{self.username}/uploads/welcome.txt'
            self.assertTrue(storage_manager.obj_exists(welcome_file_path))

    @tag('integration')
    def test_integration_user_create_success(self):
        response = self.client.post(self.create_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["username"], self.username)
        self.assertEqual(response.data["email"], self.email)

        user = User.objects.get(username=self.username)
        welcome_file_path = f'home/{self.username}/uploads/welcome.txt'
        welcome_file = UserFile.objects.get(owner=user)
        self.assertEqual(welcome_file.fname.name, welcome_file_path)

        # delete user and it's home tree
        user.delete()

    def test_user_create_failure_already_exists(self):
        User.objects.create_user(username=self.username,
                                 email=self.email,
                                 password=self.password)
        response = self.client.post(self.create_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_create_failure_bad_password(self):
        post = json.dumps(
            {"template": {"data": [{"name": "username", "value": "new_user"},
                                   {"name": "email", "value": self.email},
                                   {"name": "password", "value": "small"}]}})
        response = self.client.post(self.create_url, data=post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class UserListViewTests(ViewTests):
    """
    Test the user-create view's GET (users collection).
    """

    def setUp(self):
        super(UserListViewTests, self).setUp()
        self.read_url = reverse("user-create")
        User.objects.create_user(username=self.username, email=self.email,
                                 password=self.password)
        User.objects.create_user(username='other_username', password='other_password')

    def test_user_list_success_admin(self):
        self.client.login(username=self.chris_username, password=self.chris_password)
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], User.objects.count())
        usernames = [u['username'] for u in response.data['results']]
        self.assertEqual(sorted(usernames),
                         sorted(User.objects.values_list('username', flat=True)))
        self.assertIn(self.chris_username, usernames)
        self.assertIn(self.username, usernames)
        self.assertIn('other_username', usernames)
        for item in response.data['results']:
            self.assertNotIn('password', item)
        self.assertNotContains(response, self.password)

    def test_user_list_success_admin_pagination(self):
        self.client.login(username=self.chris_username, password=self.chris_password)
        response = self.client.get(self.read_url, {'limit': 1, 'offset': 1})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], User.objects.count())
        self.assertEqual(len(response.data['results']), 1)
        second_user = User.objects.order_by('id')[1]
        self.assertEqual(response.data['results'][0]['username'], second_user.username)
        self.assertIsNotNone(response.data['next'])

    def test_user_list_failure_access_denied(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_user_list_unauthenticated_empty(self):
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)
        self.assertNotContains(response, self.username)


class UserGroupListViewTests(ViewTests):
    """
    Test the user-group-list view.
    """

    def setUp(self):
        super(UserGroupListViewTests, self).setUp()
        user = User.objects.create_user(username=self.username,
                                        email=self.email,
                                        password=self.password)
        group, _ = Group.objects.get_or_create(name='test_group')
        user.groups.set([group])

        self.read_url = reverse("user-group-list", kwargs={"pk": user.id})

    def test_user_group_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_url)
        self.assertContains(response, 'test_group')

    def test_user_group_list_failure_unauthenticated(self):
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class UserDetailViewTests(ViewTests):
    """
    Test the user-detail view.
    """

    def setUp(self):
        super(UserDetailViewTests, self).setUp()
        user = User.objects.create_user(username=self.username,
                                        email=self.email,
                                        password=self.password)
        self.read_update_url = reverse("user-detail", kwargs={"pk": user.id})
        self.put = json.dumps({
            "template": {"data": [{"name": "password", "value": "updated_pass"},
                                  {"name": "email", "value": "dev1@babymri.org"}]}})

    def test_user_detail_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_update_url)
        self.assertContains(response, self.username)
        self.assertContains(response, self.email)

    def test_user_detail_failure_unauthenticated(self):
        response = self.client.get(self.read_update_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_user_update_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.put(self.read_update_url, data=self.put,
                                   content_type=self.content_type)
        self.assertContains(response, self.username)
        self.assertContains(response, "dev1@babymri.org")

    def test_user_update_failure_unauthenticated(self):
        response = self.client.put(self.read_update_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_user_update_failure_access_denied(self):
        User.objects.create_user(username="other_username", email="dev2@babymri.org",
                                 password="other_password")
        self.client.login(username="other_username", password="other_password")
        response = self.client.put(self.read_update_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class GroupListViewTests(ViewTests):
    """
    Test the group-list view.
    """

    def setUp(self):
        super(GroupListViewTests, self).setUp()

        self.create_read_url = reverse("group-list")
        self.post = json.dumps(
            {"template": {"data": [{"name": "name", "value": "test_group"}]}})

        user = User.objects.create_user(username=self.username,
                                        email=self.email,
                                        password=self.password)

        # create two groups
        Group.objects.get_or_create(name='G1')
        Group.objects.get_or_create(name='G2')
    def test_group_create_success(self):
        self.client.login(username=self.chris_username, password=self.chris_password)
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["name"], "test_group")

    def test_group_create_failure_unauthenticated(self):
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_group_create_failure_access_denied(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_group_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.create_read_url)
        self.assertContains(response, "G1")
        self.assertContains(response, "G2")

    def test_group_list_failure_unauthenticated(self):
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class GroupDetailViewTests(ViewTests):
    """
    Test the group-detail view.
    """

    def setUp(self):
        super(GroupDetailViewTests, self).setUp()

        User.objects.create_user(username=self.username, email=self.email,
                                 password=self.password)

        # create a group
        group, _ = Group.objects.get_or_create(name="G3")

        self.read_delete_url = reverse("group-detail", kwargs={"pk": group.id})

    def test_group_detail_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_delete_url)
        self.assertContains(response, 'G3')

    def test_group_detail_failure_unauthenticated(self):
        response = self.client.get(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_group_delete_success(self):
        self.client.login(username=self.chris_username, password=self.chris_password)
        response = self.client.delete(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Group.objects.count(), 2)

    def test_group_delete_failure_unauthenticated(self):
        response = self.client.delete(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_group_delete_failure_access_denied(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.delete(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class GroupUserListViewTests(ViewTests):
    """
    Test the group-user-list view.
    """

    def setUp(self):
        super(GroupUserListViewTests, self).setUp()

        self.post = json.dumps(
            {"template": {"data": [{"name": "username", "value": "fooboo"}]}})

        User.objects.create_user(username='fooboo', email='foo@gmail.com',
                                 password='foopass')

        user = User.objects.create_user(username=self.username, email=self.email,
                                        password=self.password)
        group, _ = Group.objects.get_or_create(name='mygroup')
        user.groups.set([group])

        self.create_read_url = reverse("group-user-list", kwargs={"pk": group.id})

    def test_group_user_create_success(self):
        self.client.login(username=self.chris_username, password=self.chris_password)
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["user_username"], "fooboo")

    def test_group_user_create_failure_unauthenticated(self):
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_group_user_create_failure_access_denied(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_group_user_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.create_read_url)

        self.assertContains(response, self.username)
        self.assertContains(response, "mygroup")

    def test_group_user_list_failure_unauthenticated(self):
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class GroupUserDetailViewTests(ViewTests):
    """
    Test the user_groups-detail view.
    """

    def setUp(self):
        super(GroupUserDetailViewTests, self).setUp()

        user = User.objects.create_user(username=self.username, email=self.email,
                                        password=self.password)
        # create a group
        group, _ = Group.objects.get_or_create(name="G4")

        user.groups.set([group])
        group_user = User.groups.through.objects.get(user=user, group=group)

        self.read_delete_url = reverse("user_groups-detail", kwargs={"pk": group_user.id})

    def test_group_user_detail_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_delete_url)
        self.assertContains(response, 'G4')
        self.assertContains(response, self.username)

    def test_group_user_detail_failure_unauthenticated(self):
        response = self.client.get(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_group_user_delete_success(self):
        self.client.login(username=self.chris_username, password=self.chris_password)
        response = self.client.delete(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(User.groups.through.objects.count(), 0)

    def test_group_user_delete_failure_unauthenticated(self):
        response = self.client.delete(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_group_user_delete_failure_access_denied(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.delete(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
