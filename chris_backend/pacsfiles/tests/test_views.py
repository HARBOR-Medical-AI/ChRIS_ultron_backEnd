
import logging
import json
import time
import io
from unittest import mock

from django.test import TestCase, TransactionTestCase, tag
from django.conf import settings
from django.contrib.auth.models import User, Group
from django.urls import reverse
from rest_framework import status

from celery.contrib.testing.worker import start_worker
from core.celery import app as celery_app

from core.models import ChrisFolder
from core.storage import connect_storage
from pacsfiles.models import PACS, PACSQuery, PACSRetrieve, PACSSeries, PACSFile
from pacsfiles import views


CHRIS_SUPERUSER_PASSWORD = settings.CHRIS_SUPERUSER_PASSWORD


class PACSViewTests(TestCase):
    """
    Generic pacs view tests' setup and tearDown.
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
        self.another_username = 'loo'
        self.another_password = 'tar'

        pacs_grp, _ = Group.objects.get_or_create(name='pacs_users')

        user = User.objects.create_user(username=self.another_username,
                                        password=self.another_password)
        user.groups.set([pacs_grp])

        user = User.objects.create_user(username=self.username, password=self.password)
        user.groups.set([pacs_grp])

        User.objects.create_user(username=self.other_username, password=self.other_password)


        # create a PACS file in the DB "already registered" to the server)
        self.storage_manager = connect_storage(settings)
        # upload file to storage
        self.path = 'SERVICES/PACS/MyPACS/123456-crazy/brain_crazy_study/SAG_T1_MPRAGE'
        with io.StringIO("test file") as file1:
            self.storage_manager.upload_obj(self.path + '/file1.dcm', file1.read(),
                                          content_type='text/plain')

        self.pacs_name = 'MyPACS'
        folder_path = f'SERVICES/PACS/{self.pacs_name}'
        owner = User.objects.get(username=self.chris_username)
        (pacs_folder, _) = ChrisFolder.objects.get_or_create(path=folder_path,
                                                             owner=owner)
        pacs = PACS(folder=pacs_folder, identifier=self.pacs_name)
        pacs.save()

        (series_folder, _) = ChrisFolder.objects.get_or_create(path=self.path,
                                                                    owner=owner)

        PACSSeries.objects.get_or_create(PatientID='123456',
                             PatientName='crazy',
                             PatientSex='O',
                             StudyDate='2020-07-15',
                             StudyInstanceUID='1.1.3432.54.6545674765.765434',
                             StudyDescription='brain_crazy_study',
                             SeriesInstanceUID='2.4.3432.54.845674765.763345',
                             SeriesDescription='SAG T1 MPRAGE',
                             pacs=pacs,
                             folder=series_folder)

        pacs_file = PACSFile(owner=owner, parent_folder=series_folder)
        pacs_file.fname.name = self.path + '/file1.dcm'
        pacs_file.save()

    def tearDown(self):
        # delete file from storage
        self.storage_manager.delete_obj(self.path + '/file1.dcm')
        # re-enable logging
        logging.disable(logging.NOTSET)


class PACSTasksViewTests(TransactionTestCase):

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
        # create superuser chris (owner of root folders)
        self.chris_username = 'chris'
        self.chris_password = CHRIS_SUPERUSER_PASSWORD

        self.content_type = 'application/vnd.collection+json'
        self.username = 'test'
        self.password = 'testpass'
        self.other_username = 'boo'
        self.other_password = 'far'
        self.another_username = 'loo'
        self.another_password = 'tar'

        pacs_grp, _ = Group.objects.get_or_create(name='pacs_users')

        user = User.objects.create_user(username=self.another_username,
                                        password=self.another_password)
        user.groups.set([pacs_grp])

        user = User.objects.create_user(username=self.username, password=self.password)
        user.groups.set([pacs_grp])

        User.objects.create_user(username=self.other_username,
                                 password=self.other_password)

        # create a PACS file in the DB "already registered" to the server)
        self.storage_manager = connect_storage(settings)
        # upload file to storage
        self.path = 'SERVICES/PACS/MyPACS/123456-crazy/brain_crazy_study/SAG_T1_MPRAGE'
        with io.StringIO("test file") as file1:
            self.storage_manager.upload_obj(self.path + '/file1.dcm', file1.read(),
                                            content_type='text/plain')

        self.pacs_name = 'MyPACS'
        folder_path = f'SERVICES/PACS/{self.pacs_name}'
        owner = User.objects.get(username=self.chris_username)
        (pacs_folder, _) = ChrisFolder.objects.get_or_create(path=folder_path,
                                                             owner=owner)
        pacs = PACS(folder=pacs_folder, identifier=self.pacs_name)
        pacs.save()

        (series_folder, _) = ChrisFolder.objects.get_or_create(path=self.path,
                                                               owner=owner)

        PACSSeries.objects.get_or_create(PatientID='123456',
                                         PatientName='crazy',
                                         PatientSex='O',
                                         StudyDate='2020-07-15',
                                         StudyInstanceUID='1.1.3432.54.6545674765.765434',
                                         StudyDescription='brain_crazy_study',
                                         SeriesInstanceUID='2.4.3432.54.845674765.763345',
                                         SeriesDescription='SAG T1 MPRAGE',
                                         pacs=pacs,
                                         folder=series_folder)

        pacs_file = PACSFile(owner=owner, parent_folder=series_folder)
        pacs_file.fname.name = self.path + '/file1.dcm'
        pacs_file.save()


class PACSListViewTests(PACSViewTests):
    """
    Test the pacs-list view.
    """

    def setUp(self):
        super(PACSListViewTests, self).setUp()
        self.read_url = reverse("pacs-list")

    @tag('integration')
    def test_integration_pacs_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_url)
        self.assertContains(response, self.pacs_name)
        self.assertContains(response, 'MINICHRISORTHANC')

    def test_pacs_list_failure_unauthenticated(self):
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class PACSListQuerySearchViewTests(PACSViewTests):
    """
    Test the pacs-list-query-search view.
    """

    def setUp(self):
        super(PACSListQuerySearchViewTests, self).setUp()

        self.read_url = reverse("pacs-list-query-search") + '?name=' + self.pacs_name

    def test_pacs_list_query_search_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_url)
        self.assertContains(response, self.pacs_name)

    def test_pacs_list_query_search_failure_unauthenticated(self):
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class PACSDetailViewTests(PACSViewTests):
    """
    Test the pacs-detail view.
    """

    def setUp(self):
        super(PACSDetailViewTests, self).setUp()
        pacs = PACS.objects.get(identifier=self.pacs_name)
        self.read_url = reverse("pacs-detail", kwargs={"pk": pacs.id})

    def test_pacs_detail_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_url)
        self.assertContains(response, self.pacs_name)
        self.assertEqual(response.data["active"], True)

    def test_pacs_detail_failure_unauthenticated(self):
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class PACSSpecificSeriesListViewTests(PACSViewTests):
    """
    Test the pacs-specific-series-list view.
    """

    def setUp(self):
        super(PACSSpecificSeriesListViewTests, self).setUp()
        pacs = PACS.objects.get(identifier=self.pacs_name)
        self.read_url = reverse("pacs-specific-series-list", kwargs={"pk": pacs.id})

    def test_pacs_specific_series_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_url)
        self.assertContains(response, 'brain_crazy_study')

    def test_pacs_list_failure_unauthenticated(self):
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class PACSQueryListViewTests(PACSViewTests):
    """
    Test the pacsquery-list view.
    """

    def setUp(self):
        super(PACSQueryListViewTests, self).setUp()

        pacs = PACS.objects.get(identifier=self.pacs_name)
        user = User.objects.get(username=self.username)

        self.create_read_url = reverse("pacsquery-list", kwargs={"pk": pacs.id})

        self.query = {'SeriesInstanceUID': '2.3.15.2.1057'}
        pacs_query, _ = PACSQuery.objects.get_or_create(title='query10', query=self.query,
                                                        owner=user, pacs=pacs)

        self.post = json.dumps(
            {"template": {"data": [{"name": "title", "value": 'test1'},
                                   {"name": "query", "value": json.dumps(self.query)}]}})

    def test_pacs_query_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.create_read_url)
        self.assertContains(response, 'query10')

    def test_pacs_query_list_success_readonly(self):
        pacs = PACS.objects.get(identifier=self.pacs_name)
        user = User.objects.get(username=self.other_username)

        query = {'SeriesInstanceUID': '2.3.15.2.1158'}
        pacs_query, _ = PACSQuery.objects.get_or_create(title='query2', query=query,
                                                        owner=user, pacs=pacs)
        self.client.login(username=self.other_username, password=self.other_password) # not a member of pacs_users
        response = self.client.get(self.create_read_url)
        self.assertContains(response, 'query2')  # can see its own queries
        self.assertNotContains(response, 'query10')  # cannot see other users' queries

    def test_pacs_query_list_failure_unauthenticated(self):
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_pacs_query_create_success_execute(self):
        with mock.patch.object(views.send_pacs_query, 'delay',
                               return_value=None) as delay_mock:
            # make API request
            self.client.login(username=self.username, password=self.password)
            response = self.client.post(self.create_read_url, data=self.post,
                                        content_type=self.content_type)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)

            # check that the send_pacs_query task was called with appropriate args
            delay_mock.assert_called_with(response.data['id'])
            self.assertEqual(response.data['status'], 'created')

    def test_pacs_query_create_success_do_not_execute(self):

        post = json.dumps(
            {"template": {"data": [{"name": "title", "value": 'test2'},
                                   {"name": "execute", "value": False},
                                   {"name": "query", "value": json.dumps(self.query)}]}})

        with mock.patch.object(views.send_pacs_query, 'delay',
                               return_value=None) as delay_mock:
            # make API request
            self.client.login(username=self.username, password=self.password)
            response = self.client.post(self.create_read_url, data=post,
                                        content_type=self.content_type)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)

            # check that the send_pacs_query task was not called
            delay_mock.assert_not_called()
            self.assertEqual(response.data['status'], 'created')

    def test_pacs_query_create_failure_unauthenticated(self):
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_pacs_query_create_failure_forbidden(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class AllPACSQueryListViewTests(PACSViewTests):
    """
    Test the allpacsquery-list view.
    """

    def setUp(self):
        super(AllPACSQueryListViewTests, self).setUp()

        pacs = PACS.objects.get(identifier=self.pacs_name)
        user = User.objects.get(username=self.username)
        query = {'SeriesInstanceUID': '2.3.15.2.1057'}
        pacs_query, _ = PACSQuery.objects.get_or_create(title='query1', query=query,
                                                        owner=user, pacs=pacs)

        self.read_url = reverse("allpacsquery-list")

    def test_all_pacs_query_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_url)
        self.assertContains(response, 'query1')

    def test_all_pacs_query_list_success_readonly(self):
        pacs = PACS.objects.get(identifier=self.pacs_name)
        user = User.objects.get(username=self.other_username)

        query = {'SeriesInstanceUID': '2.3.15.2.1158'}
        pacs_query, _ = PACSQuery.objects.get_or_create(title='query2', query=query,
                                                        owner=user, pacs=pacs)
        self.client.login(username=self.other_username, password=self.other_password) # not a member of pacs_users
        response = self.client.get(self.read_url)
        self.assertContains(response, 'query2')  # can see its own queries
        self.assertNotContains(response, 'query1')  # cannot see other users' queries

    def test_all_pacs_query_list_failure_unauthenticated(self):
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class AllPACSQueryListQuerySearchViewTests(PACSViewTests):
    """
    Test the allpacsquery-list-query-search view.
    """

    def setUp(self):
        super(AllPACSQueryListQuerySearchViewTests, self).setUp()

        pacs = PACS.objects.get(identifier=self.pacs_name)
        user = User.objects.get(username=self.username)
        query = {'SeriesInstanceUID': '2.3.15.2.1057'}
        pacs_query, _ = PACSQuery.objects.get_or_create(title='query1', query=query,
                                                        owner=user, pacs=pacs)

        self.read_url = reverse("allpacsquery-list-query-search") + '?name=query1'

    def test_all_pacs_query_list_query_search_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_url)
        self.assertContains(response, 'query1')

    def test_all_pacs_query_list_query_search_success_readonly(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['results'], [])

    def test_all_pacs_query_list_query_search_failure_unauthenticated(self):
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class PACSQueryDetailViewTests(PACSViewTests):
    """
    Test the pacsquery-detail view.
    """

    def setUp(self):
        super(PACSQueryDetailViewTests, self).setUp()

        pacs = PACS.objects.get(identifier=self.pacs_name)
        user = User.objects.get(username=self.username)
        query = {'SeriesInstanceUID': '2.3.15.2.1057'}

        pacs_query, _ = PACSQuery.objects.get_or_create(title='query1', query=query,
                                                        owner=user, pacs=pacs)
        self.read_update_delete_url = reverse("pacsquery-detail",
                                              kwargs={"pk":pacs_query.id})

        self.put = json.dumps({
            "template": {"data": [{"name": "title", "value": "Test query"}]}})

    def test_pacs_query_detail_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_update_delete_url)
        self.assertContains(response, 'query1')

    def test_pacs_query_detail_success_other_pacs_user(self):
        self.client.login(username=self.another_username,
                          password=self.another_password)
        response = self.client.get(self.read_update_delete_url)
        self.assertContains(response, 'query1')

    def test_pacs_query_detail_failure_unauthenticated(self):
        response = self.client.get(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_pacs_query_detail_failure_forbidden(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_pacs_query_update_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertContains(response, "Test query")

    def test_pacs_query_update_success_execute(self):
        pacs = PACS.objects.get(identifier=self.pacs_name)
        user = User.objects.get(username=self.username)
        query = {'SeriesInstanceUID': '2.3.15.2.1057'}

        pacs_query, _ = PACSQuery.objects.get_or_create(title='query2', execute=False,
                                                        query=query, owner=user, pacs=pacs)
        read_update_delete_url = reverse("pacsquery-detail", kwargs={"pk":pacs_query.id})

        put = json.dumps({
            "template": {"data": [{"name": "title", "value": "Test query2"},
                                  {"name": "execute", "value": True}]}})

        self.client.login(username=self.username, password=self.password)

        with mock.patch.object(views.send_pacs_query, 'delay',
                               return_value=None) as delay_mock:
            response = self.client.put(read_update_delete_url, data=put,
                                       content_type=self.content_type)
            # check that the send_pacs_query task was called with appropriate args
            delay_mock.assert_called_with(response.data['id'])
            self.assertContains(response, "Test query2")

    def test_pacs_query_update_success_do_not_execute_again(self):
        put = json.dumps({
            "template": {"data": [{"name": "title", "value": "Test query"},
                                  {"name": "execute", "value": True}]}})

        self.client.login(username=self.username, password=self.password)

        with mock.patch.object(views.send_pacs_query, 'delay',
                               return_value=None) as delay_mock:
            response = self.client.put(self.read_update_delete_url, data=put,
                                       content_type=self.content_type)
            # check that the send_pacs_query task was not called
            delay_mock.assert_not_called()
            self.assertContains(response, "Test query")

    def test_pacs_query_update_failure_unauthenticated(self):
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_pacs_query_update_failure_access_denied_non_pacs_user(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_pacs_query_update_failure_access_denied_other_pacs_user(self):
        self.client.login(username=self.another_username,
                          password=self.another_password)
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_pacs_query_delete_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_pacs_query_delete_failure_unauthenticated(self):
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_pacs_query_delete_failure_access_denied_non_pacs_user(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_pacs_query_delete_failure_access_denied_other_pacs_user(self):
        self.client.login(username=self.another_username,
                          password=self.another_password)
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class PACSRetrieveListViewTests(PACSViewTests):
    """
    Test the pacsretrieve-list view.
    """

    def setUp(self):
        super(PACSRetrieveListViewTests, self).setUp()

        pacs = PACS.objects.get(identifier=self.pacs_name)
        user = User.objects.get(username=self.username)

        query = {'SeriesInstanceUID': '2.3.15.2.1057'}
        pacs_query, _ = PACSQuery.objects.get_or_create(title='query1', query=query,
                                                        owner=user, pacs=pacs)

        PACSRetrieve.objects.get_or_create(pacs_query=pacs_query, owner=user)

        self.create_read_url = reverse("pacsretrieve-list", kwargs={"pk": pacs_query.id})

    def test_pacs_retrieve_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.create_read_url)
        self.assertContains(response, 'query1')

    def test_pacs_retrieve_list_success_readonly(self):
        self.client.login(username=self.another_username, password=self.another_password) # a member of pacs_users
        response = self.client.get(self.create_read_url)
        self.assertContains(response, 'query1')

    def test_pacs_retrieve_list_failure_unauthenticated(self):
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_pacs_retrieve_list_failure_forbiden(self):
        self.client.login(username=self.other_username, password=self.other_password) # not a member of pacs_users
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_pacs_retrieve_create_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.post(self.create_read_url)  # empty data POST do not use content_type
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_pacs_retrieve_create_failure_unauthenticated(self):
        response = self.client.post(self.create_read_url)  # empty data POST do not use content_type
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_pacs_retrieve_create_failure_forbidden(self):
        self.client.login(username=self.another_username, password=self.another_password)
        response = self.client.post(self.create_read_url)  # empty data POST do not use content_type
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class PACSRetrieveListQuerySearchViewTests(PACSViewTests):
    """
    Test the pacsretrieve-list-query-search view.
    """

    def setUp(self):
        super(PACSRetrieveListQuerySearchViewTests, self).setUp()

        pacs = PACS.objects.get(identifier=self.pacs_name)
        user = User.objects.get(username=self.username)

        query = {'SeriesInstanceUID': '2.3.15.2.1057'}
        pacs_query, _ = PACSQuery.objects.get_or_create(title='query1', query=query,
                                                        owner=user, pacs=pacs)

        pacs_retrieve, _ = PACSRetrieve.objects.get_or_create(pacs_query=pacs_query,
                                                              owner=user)

        self.read_url = reverse("pacsretrieve-list-query-search",
                                kwargs={"pk": pacs_query.id}) + f'?id={pacs_retrieve.id}'

    def test_pacs_retrieve_list_query_search_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_url)
        self.assertContains(response, 'query1')

    def test_pacs_retrieve_list_query_search_success_readonly(self):
        self.client.login(username=self.another_username, password=self.another_password)
        response = self.client.get(self.read_url)
        self.assertContains(response, 'query1')

    def test_pacs_retrieve_list_query_search_failure_unauthenticated(self):
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_pacs_retrieve_list_query_search_failure_forbidden(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class PACSRetrieveDetailViewTests(PACSViewTests):
    """
    Test the pacsretrieve-detail view.
    """

    def setUp(self):
        super(PACSRetrieveDetailViewTests, self).setUp()

        pacs = PACS.objects.get(identifier=self.pacs_name)
        user = User.objects.get(username=self.username)

        query = {'SeriesInstanceUID': '2.3.15.2.1057'}
        pacs_query, _ = PACSQuery.objects.get_or_create(title='query1', query=query,
                                                        owner=user, pacs=pacs)

        pacs_retrieve, _ = PACSRetrieve.objects.get_or_create(pacs_query=pacs_query,
                                                              owner=user)

        self.read_delete_url = reverse("pacsretrieve-detail", kwargs={"pk":pacs_retrieve.id})


    def test_pacs_retrieve_detail_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_delete_url)
        self.assertContains(response, 'query1')

    def test_pacs_retrieve_detail_success_other_pacs_user(self):
        self.client.login(username=self.another_username,
                          password=self.another_password)
        response = self.client.get(self.read_delete_url)
        self.assertContains(response, 'query1')

    def test_pacs_retrieve_detail_failure_unauthenticated(self):
        response = self.client.get(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_pacs_retrieve_detail_failure_forbidden(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_pacs_retrieve_delete_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.delete(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_pacs_retrieve_delete_failure_unauthenticated(self):
        response = self.client.delete(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_pacs_retrieve_delete_failure_access_denied_non_pacs_user(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.delete(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_pacs_retrieve_delete_failure_access_denied_other_pacs_user(self):
        self.client.login(username=self.another_username,
                          password=self.another_password)
        response = self.client.delete(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class PACSSeriesListViewTests(PACSViewTests):
    """
    Test the pacsseries-list view.
    """

    def setUp(self):
        super(PACSSeriesListViewTests, self).setUp()

        # create a PACS file in the DB "already registered" to the server)
        self.storage_manager = connect_storage(settings)
        
        # upload file to storage
        self.new_path = 'SERVICES/PACS/MyPACS/123456-good/brain_good_study/SAG_T1_MPRAGE'
        with io.StringIO("test file") as file1:
            self.storage_manager.upload_obj(self.new_path + '/file1.dcm', file1.read(),
                                          content_type='text/plain')
        self.post = json.dumps(
            {"template": {"data": [{"name": "path", "value": self.new_path + '/file1.dcm'},
                                   {"name": "ndicom", "value" : 1},
                                   {"name": "PatientID", "value": "123456"},
                                   {"name": "PatientName", "value": "crazy"},
                                   {"name": "PatientBirthDate", "value": "2020-07-15"},
                                   {"name": "PatientSex", "value": "O"},
                                   {"name": "StudyDate", "value": "2020-07-15"},
                                   {"name": "StudyInstanceUID", "value": "1.1.3432.54.6545674765.765478"},
                                   {"name": "StudyDescription", "value": "brain_good_study"},
                                   {"name": "SeriesInstanceUID", "value": "2.4.3432.54.845674765.763357"},
                                   {"name": "SeriesDescription", "value": "SAG T1 MPRAGE"},
                                   {"name": "pacs_name", "value": self.pacs_name}
                                   ]}})

        self.create_read_url = reverse("pacsseries-list")

    def tearDown(self):
        # delete file from storage
        self.storage_manager.delete_obj(self.new_path + '/file1.dcm')
        super(PACSSeriesListViewTests, self).tearDown()

    def test_pacs_series_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.create_read_url)
        self.assertContains(response, 'brain_crazy_study')

    def test_pacs_series_list_failure_unauthenticated(self):
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_pacs_series_list_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_pacs_series_create_success(self):
        self.client.login(username=self.chris_username, password=self.chris_password)
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_pacs_series_create_failure_unauthenticated(self):
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_pacs_series_create_failure_forbidden(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class PACSSeriesListQuerySearchViewTests(PACSViewTests):
    """
    Test the pacsseries-list-query-search view.
    """

    def setUp(self):
        super(PACSSeriesListQuerySearchViewTests, self).setUp()

        self.read_url = reverse("pacsseries-list-query-search") + '?PatientID=123456'

    def test_pacs_series_list_query_search_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_url)
        self.assertContains(response, 'crazy')

    def test_pacs_series_list_query_search_failure_unauthenticated(self):
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_pacs_series_list_query_search_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class PACSSeriesDetailViewTests(PACSTasksViewTests):
    """
    Test the pacsseries-detail view.
    """

    def setUp(self):
        super(PACSSeriesDetailViewTests, self).setUp()
        pacs_series = PACSSeries.objects.get(PatientID='123456')
        self.read_delete_url = reverse("pacsseries-detail", kwargs={"pk": pacs_series.id})

    def test_pacs_series_detail_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_delete_url)
        self.assertContains(response, 'brain_crazy_study')

    def test_pacs_series_detail_failure_unauthenticated(self):
        response = self.client.get(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_pacs_series_detail_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_pacs_series_delete_success(self):
        self.client.login(username=self.chris_username, password=self.chris_password)

        with mock.patch.object(views.delete_pacs_series, 'delay',
                               return_value=None) as delay_mock:
            response = self.client.delete(self.read_delete_url)
            self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

            # check that the delete_feed task was called with appropriate args
            delay_mock.assert_called_with(response.data['id'])

    @tag('integration')
    def test_integration_pacs_series_delete_success(self):
        self.client.login(username=self.chris_username, password=self.chris_password)
        response = self.client.delete(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        for _ in range(10):
            time.sleep(3)
            if PACSSeries.objects.count() == 0: break
        self.assertEqual(PACSSeries.objects.count(), 0)

    def test_pacs_series_delete_failure_unauthenticated(self):
        response = self.client.delete(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_pacs_series_delete_failure_access_denied(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.delete(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class PACSFileListViewTests(PACSViewTests):
    """
    Test the pacsfile-list view.
    """

    def setUp(self):
        super(PACSFileListViewTests, self).setUp()
        self.read_url = reverse("pacsfile-list")

    def test_pacsfile_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_url)
        self.assertContains(response, self.path)

    def test_pacsfile_list_failure_unauthenticated(self):
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_pacsfile_list_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class PACSFileListQuerySearchViewTests(PACSViewTests):
    """
    Test the pacsfile-list-query-search view.
    """

    def setUp(self):
        super(PACSFileListQuerySearchViewTests, self).setUp()
        self.read_url = reverse("pacsseries-list-query-search") + '?path=' + self.path

    def test_pacsfile_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_url)
        self.assertContains(response, self.path)

    def test_pacsfile_list_failure_unauthenticated(self):
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_pacsfile_list_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class PACSFileDetailViewTests(PACSViewTests):
    """
    Test the pacsfile-detail view.
    """

    def setUp(self):
        super(PACSFileDetailViewTests, self).setUp()
        pacs_file = PACSFile.objects.get(fname=self.path + '/file1.dcm')
        self.read_url = reverse("pacsfile-detail",
                                kwargs={"pk": pacs_file.id})

    def test_pacsfile_detail_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_url)
        self.assertContains(response, self.path)

    def test_pacsfile_detail_failure_unauthenticated(self):
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_pacsfile_detail_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class PACSFileResourceViewTests(PACSViewTests):
    """
    Test the pacsfile-resource view.
    """

    def setUp(self):
        super(PACSFileResourceViewTests, self).setUp()
        pacs_file = PACSFile.objects.get(fname=self.path + '/file1.dcm')
        self.download_url = reverse("pacsfile-resource",
                                    kwargs={"pk": pacs_file.id}) + 'file1.dcm'

    def test_pacsfileresource_get(self):
        pacs_file = PACSFile.objects.get(fname=self.path + '/file1.dcm')
        fileresource_view_inst = mock.Mock()
        fileresource_view_inst.get_object = mock.Mock(return_value=pacs_file)
        request_mock = mock.Mock()
        with mock.patch('pacsfiles.views.FileResponse') as response_mock:
            views.PACSFileResource.get(fileresource_view_inst, request_mock)
            response_mock.assert_called_with(pacs_file.fname)

    @tag('integration')
    def test_integration_pacsfileresource_download_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.download_url)
        self.assertEqual(response.status_code, 200)
        content = [c for c in response.streaming_content][0].decode('utf-8')
        self.assertEqual(content, "test file")

    def test_fileresource_download_failure_unauthenticated(self):
        response = self.client.get(self.download_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_fileresource_download_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.download_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
