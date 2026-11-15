
import logging
import json
import time
import warnings
from unittest import mock

from django.test import TestCase, TransactionTestCase, tag
from django.urls import reverse
from django.contrib.auth.models import User, Group
from django.conf import settings
from rest_framework import status

from celery.contrib.testing.worker import start_worker
from core.celery import app as celery_app
from core.tests.query_counts import QueryCountAssertionsMixin

from plugins.models import PluginMeta, Plugin, ComputeResource
from plugininstances.models import PluginInstance
from feeds.models import (Note, Tag, Tagging, Feed, FeedGroupPermission,
                          FeedUserPermission, Comment)
from feeds import views


COMPUTE_RESOURCE_URL = settings.COMPUTE_RESOURCE_URL
CHRIS_SUPERUSER_PASSWORD = settings.CHRIS_SUPERUSER_PASSWORD


class ViewTests(QueryCountAssertionsMixin, TestCase):

    def setUp(self):
        # avoid cluttered console output (for instance logging all the http requests)
        logging.disable(logging.WARNING)

        # create superuser chris (owner of root folders)
        self.chris_username = 'chris'
        self.chris_password = CHRIS_SUPERUSER_PASSWORD

        self.content_type='application/vnd.collection+json'

        self.username = 'foo'
        self.password = 'foopass'
        self.other_username = 'booo'
        self.other_password = 'booopass'
             
        self.plugin_name = "pacspull"
        self.plugin_type = "fs"
        self.plugin_parameters = {'mrn': {'type': 'string', 'optional': False},
                                  'img_type': {'type': 'string', 'optional': True}}
        self.feedname = "Feed1"

        (self.compute_resource, tf) = ComputeResource.objects.get_or_create(
            name="host", compute_url=COMPUTE_RESOURCE_URL)

        # create basic models
        
        # create users
        other_user = User.objects.create_user(username=self.other_username,
                                              password=self.other_password)
        user = User.objects.create_user(username=self.username,
                                        password=self.password)

        # assign predefined group
        all_grp = Group.objects.get(name='all_users')

        other_user.groups.set([all_grp])
        user.groups.set([all_grp])
        
        # create two plugins of different types

        (pl_meta, tf) = PluginMeta.objects.get_or_create(name='mri_convert', type='ds')
        (plugin, tf) = Plugin.objects.get_or_create(meta=pl_meta, version='0.1')
        plugin.compute_resources.set([self.compute_resource])
        plugin.save()

        (pl_meta, tf) = PluginMeta.objects.get_or_create(name='pacspull', type='fs')
        (plugin, tf) = Plugin.objects.get_or_create(meta=pl_meta, version='0.1')
        plugin.compute_resources.set([self.compute_resource])
        plugin.save()
        
        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=plugin, owner=user, title='test',
            compute_resource=plugin.compute_resources.all()[0])
        pl_inst.feed.name = self.feedname
        pl_inst.feed.save()

    def tearDown(self):
        # re-enable logging
        logging.disable(logging.NOTSET)

    def create_feeds(self, count, public=False):
        """
        Custom method to create count feeds owned by the user, each by creating a "fs"
        plugin instance, and return them. Their names contain self.feedname.
        """
        plugin = Plugin.objects.get(meta__name="pacspull")
        user = User.objects.get(username=self.username)

        feeds = []
        for i in range(count):
            pl_inst = PluginInstance.objects.create(
                plugin=plugin, owner=user, title=f'{self.feedname}-{i}',
                compute_resource=plugin.compute_resources.all()[0])

            pl_inst.feed.public = public
            pl_inst.feed.save()

            feeds.append(pl_inst.feed)
        return feeds


class TasksViewTests(TransactionTestCase):

    @classmethod
    def setUpClass(cls):
        logging.disable(logging.WARNING)
        logging.getLogger('plugininstances.tasks').setLevel(logging.CRITICAL)
        warnings.filterwarnings('ignore', 'No directory at', UserWarning)
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
        # create superuser chris (owner of root folders)
        self.chris_username = 'chris'
        self.chris_password = CHRIS_SUPERUSER_PASSWORD

        self.content_type = 'application/vnd.collection+json'

        self.username = 'foo'
        self.password = 'foopass'
        self.other_username = 'booo'
        self.other_password = 'booopass'

        self.plugin_name = "pacspull"
        self.plugin_type = "fs"
        self.plugin_parameters = {'mrn': {'type': 'string', 'optional': False},
                                  'img_type': {'type': 'string', 'optional': True}}
        self.feedname = "Feed1"

        (self.compute_resource, tf) = ComputeResource.objects.get_or_create(
            name="host", compute_url=COMPUTE_RESOURCE_URL)

        # create users
        other_user = User.objects.create_user(username=self.other_username,
                                              password=self.other_password)
        user = User.objects.create_user(username=self.username,
                                        password=self.password)

        # assign predefined group
        all_grp = Group.objects.get(name='all_users')

        other_user.groups.set([all_grp])
        user.groups.set([all_grp])

        # create plugin
        (pl_meta, tf) = PluginMeta.objects.get_or_create(name='pacspull', type='fs')
        (plugin, tf) = Plugin.objects.get_or_create(meta=pl_meta, version='0.1')
        plugin.compute_resources.set([self.compute_resource])
        plugin.save()

        # create a feed by creating a "fs" plugin instance
        pl_inst = PluginInstance.objects.create(plugin=plugin, owner=user, title='test',
                                                compute_resource=
                                                plugin.compute_resources.all()[0])
        pl_inst.feed.name = self.feedname
        pl_inst.feed.save()

class NoteDetailViewTests(ViewTests):
    """
    Test the note-detail view.
    """

    def setUp(self):
        super(NoteDetailViewTests, self).setUp()
        feed = Feed.objects.get(name=self.feedname)
        self.read_update_url = reverse("note-detail", kwargs={"pk": feed.id})
        self.put = json.dumps({"template": {"data": [{"name": "title", "value": "Note1"},
                                          {"name": "content", "value": "My first note"}]}})

    def test_note_detail_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_update_url)
        self.assertContains(response, "title")
        self.assertContains(response, "content")

    def test_note_detail_success_user_chris(self):
        self.client.login(username=self.chris_username, password=self.chris_password)
        response = self.client.get(self.read_update_url)
        self.assertContains(response, "title")
        self.assertContains(response, "content")

    def test_note_detail_failure_unauthenticated(self):
        response = self.client.get(self.read_update_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_note_detail_failure_not_related_feed_owner(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.read_update_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_note_update_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.put(self.read_update_url, data=self.put,
                                   content_type=self.content_type)
        self.assertContains(response, "Note1")
        self.assertContains(response, "My first note")

    def test_note_update_failure_unauthenticated(self):
        response = self.client.put(self.read_update_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_note_update_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.put(self.read_update_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        

class FeedListViewTests(ViewTests):
    """
    Test the feed-list view.
    """

    def setUp(self):
        super(FeedListViewTests, self).setUp()     
              
        self.list_url = reverse("feed-list")

        # create an additional feed using a "fs" plugin instance
        plugin = Plugin.objects.get(meta__name="pacspull")
        user = User.objects.get(username=self.username)
        pl_inst = PluginInstance.objects.create(
            plugin=plugin, owner=user, compute_resource=plugin.compute_resources.all()[0])
        pl_inst.feed.name = "Feed2"
        pl_inst.feed.save()
    
    def test_feed_create_failure_post_not_allowed(self):
        self.client.login(username=self.username, password=self.password)
        # try to create a new feed with a POST request to the list of feeds
        post = json.dumps({"template": {"data": [{"name": "name", "value": "Feed2"}]}})
        response = self.client.post(self.list_url, data=post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_feed_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.list_url)
        self.assertContains(response, "Feed1")
        self.assertContains(response, "Feed2")

    def test_feed_list_query_count_does_not_grow_with_feeds(self):
        # the owner, folder and note the serializer reads for every feed are fetched in
        # the list query itself
        self.client.login(username=self.username, password=self.password)
        self.assertQueriesPerListItem(self.list_url, lambda: self.create_feeds(3))

    def test_feed_list_success_chris_user_lists_all_users_feeds(self):
        self.client.login(username=self.chris_username, password=self.chris_password)
        response = self.client.get(self.list_url)
        self.assertContains(response, "Feed1")
        self.assertContains(response, "Feed2")

    def test_feed_list_success_unauthenticated(self):
        response = self.client.get(self.list_url)
        self.assertNotContains(response, "Feed1")
        self.assertNotContains(response, "Feed2")

    def test_feed_list_from_other_users_not_listed(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.list_url)
        self.assertNotContains(response, "Feed1")
        self.assertNotContains(response, "Feed2")


class FeedListQuerySearchViewTests(ViewTests):
    """
    Test the feed-list-query-search view.
    """

    def setUp(self):
        super(FeedListQuerySearchViewTests, self).setUp()     
              
        self.list_url = reverse("feed-list-query-search") + '?name=' + self.feedname

        # create an additional feed using a "fs" plugin instance
        plugin = Plugin.objects.get(meta__name="pacspull")
        user = User.objects.get(username=self.username)
        pl_inst = PluginInstance.objects.create(
            plugin=plugin, owner=user, compute_resource=plugin.compute_resources.all()[0])
        pl_inst.feed.name = "Feed2"
        pl_inst.feed.save()

    def test_feed_list_query_search_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.list_url)
        self.assertContains(response, self.feedname)
        self.assertNotContains(response, "Feed2")

    def test_feed_list_query_search_query_count_does_not_grow_with_feeds(self):
        self.client.login(username=self.username, password=self.password)
        self.assertQueriesPerListItem(self.list_url, lambda: self.create_feeds(3))

    def test_feed_list_query_search_success_chris_user_lists_all_matching_feeds(self):
        self.client.login(username=self.chris_username, password=self.chris_password)
        response = self.client.get(self.list_url)
        self.assertContains(response, self.feedname)

    def test_feed_list_query_search_success_unauthenticated(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['results'], [])


    def test_feed_list_query_search_from_other_users_not_listed(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.list_url)
        self.assertNotContains(response, "Feed2")
        

class FeedDetailViewTests(TasksViewTests):
    """
    Test the feed-detail view.
    """

    def setUp(self):
        super(FeedDetailViewTests, self).setUp()     
        feed = Feed.objects.get(name=self.feedname)
        
        self.read_update_delete_url = reverse("feed-detail", kwargs={"pk": feed.id})
        self.put = json.dumps({
            "template": {"data": [{"name": "name", "value": "Updated"},
                                  {"name": "public", "value": True}]}})
          
    def test_feed_detail_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_update_delete_url)
        self.assertContains(response, self.feedname)

    def test_feed_detail_fetches_related_objects_with_the_feed(self):
        self.client.login(username=self.username, password=self.password)
        # two queries authenticate the session and one fetches the feed with its job
        # counts and the owner, folder and note the serializer reads
        with self.assertNumQueries(3):
            self.client.get(self.read_update_delete_url)

    def test_feed_detail_failure_unauthenticated(self):
        response = self.client.get(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_feed_detail_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_feed_update_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertContains(response, "Updated")
        feed = Feed.objects.get(name="Updated")
        self.assertTrue(feed.public)
        feed.remove_public_access()

    def test_feed_update_failure_unauthenticated(self):
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_feed_update_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_feed_delete_success(self):
        self.client.login(username=self.username, password=self.password)

        with mock.patch.object(views.delete_feed, 'delay',
                               return_value=None) as delay_mock:
            response = self.client.delete(self.read_update_delete_url)
            self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

            # check that the delete_feed task was called with appropriate args
            delay_mock.assert_called_with(response.data['id'])

    @tag('integration')
    def test_integration_feed_delete_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        for _ in range(10):
            time.sleep(3)
            if Feed.objects.count() == 0: break
        self.assertEqual(Feed.objects.count(), 0)

    def test_feed_delete_failure_unauthenticated(self):
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_feed_delete_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class PublicFeedListViewTests(ViewTests):
    """
    Test the publicfeed-list view.
    """

    def setUp(self):
        super(PublicFeedListViewTests, self).setUp()
        self.list_url = reverse("publicfeed-list")
        self.create_feeds(1, public=True)

    def test_public_feed_list_success(self):
        response = self.client.get(self.list_url)
        names = [feed['name'] for feed in response.data['results']]
        self.assertEqual(names, [f'{self.feedname}-0'])  # the private feed is not listed

    def test_public_feed_list_query_count_does_not_grow_with_feeds(self):
        self.assertQueriesPerListItem(self.list_url,
                                      lambda: self.create_feeds(3, public=True))


class PublicFeedListQuerySearchViewTests(ViewTests):
    """
    Test the publicfeed-list-query-search view.
    """

    def setUp(self):
        super(PublicFeedListQuerySearchViewTests, self).setUp()
        self.list_url = reverse("publicfeed-list-query-search") + '?name=' + self.feedname
        self.create_feeds(1, public=True)

    def test_public_feed_list_query_search_success(self):
        response = self.client.get(self.list_url)
        names = [feed['name'] for feed in response.data['results']]
        self.assertEqual(names, [f'{self.feedname}-0'])  # the private feed is not listed

    def test_public_feed_list_query_search_query_count_does_not_grow_with_feeds(self):
        self.assertQueriesPerListItem(self.list_url,
                                      lambda: self.create_feeds(3, public=True))


class FeedGroupPermissionListViewTests(ViewTests):
    """
    Test the 'feedgrouppermission-list' view.
    """

    def setUp(self):
        super(FeedGroupPermissionListViewTests, self).setUp()

        self.grp_name = 'all_users'

        feed = Feed.objects.get(name=self.feedname)

        self.create_read_url = reverse('feedgrouppermission-list',
                                       kwargs={"pk": feed.id})
        self.post = json.dumps(
            {"template":
                 {"data": [{"name": "grp_name", "value": self.grp_name}]}})

    def test_feed_group_permission_create_success(self):
        self.client.login(username=self.username, password=self.password)

        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        feed = Feed.objects.get(name=self.feedname)

        self.assertIn(self.grp_name, [g.name for g in feed.shared_groups.all()])
        self.assertIn(self.grp_name, [g.name for g in feed.folder.shared_groups.all()])

        grp = Group.objects.get(name=self.grp_name)
        feed.remove_group_permission(grp)
        feed.folder.remove_shared_link()

    def ttest_feed_group_permission_create_failure_unauthenticated(self):
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_feed_group_permission_create_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_feed_group_permission_shared_create_failure_access_denied(self):
        other_user = User.objects.get(username=self.other_username)
        feed = Feed.objects.get(name=self.feedname)
        feed.grant_user_permission(other_user)

        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        feed.remove_user_permission(other_user)

    def test_feed_group_permission_list_success(self):
        grp = Group.objects.get(name=self.grp_name)
        feed = Feed.objects.get(name=self.feedname)
        feed.grant_group_permission(grp)

        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.create_read_url)
        self.assertContains(response, self.grp_name)

        feed.remove_group_permission(grp)

    def test_feed_group_permission_list_failure_unauthenticated(self):
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_feed_group_permission_list_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_feed_group_permission_shared_user_list_success(self):
        other_user = User.objects.get(username=self.other_username)
        feed = Feed.objects.get(name=self.feedname)
        feed.grant_user_permission(other_user)

        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        feed.remove_user_permission(other_user)


class FeedGroupPermissionListQuerySearchViewTests(ViewTests):
    """
    Test the 'feedgrouppermission-list-query-search' view.
    """

    def setUp(self):
        super(FeedGroupPermissionListQuerySearchViewTests, self).setUp()

        self.grp_name = 'all_users'

        feed = Feed.objects.get(name=self.feedname)

        self.read_url = reverse('feedgrouppermission-list-query-search',
                                kwargs={"pk": feed.id})

        grp = Group.objects.get(name=self.grp_name)
        feed.grant_group_permission(grp)

    def tearDown(self):
        grp = Group.objects.get(name=self.grp_name)
        feed = Feed.objects.get(name=self.feedname)
        feed.remove_group_permission(grp)

        super(FeedGroupPermissionListQuerySearchViewTests, self).tearDown()

    def test_feed_group_permission_list_query_search_success(self):
        read_url = f'{self.read_url}?group_name={self.grp_name}'

        self.client.login(username=self.username, password=self.password)
        response = self.client.get(read_url)
        self.assertContains(response, self.grp_name)

    def test_feed_group_permission_list_query_search_success_shared(self):
        read_url = f'{self.read_url}?group_name={self.grp_name}'

        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(read_url)
        self.assertContains(response, self.grp_name)

    def test_feed_group_permission_list_query_search_failure_unauthenticated(self):
        read_url = f'{self.read_url}?group_name={self.grp_name}'

        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_feed_group_permission_list_query_search_failure_other_user(self):
        grp = Group.objects.get(name=self.grp_name)
        feed = Feed.objects.get(name=self.feedname)
        feed.remove_group_permission(grp)

        read_url = f'{self.read_url}?group_name={self.grp_name}'

        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['results'])


class FeedGroupPermissionDetailViewTests(ViewTests):
    """
    Test the feedgrouppermission-detail view.
    """

    def setUp(self):
        super(FeedGroupPermissionDetailViewTests, self).setUp()

        self.grp_name = 'all_users'

        feed = Feed.objects.get(name=self.feedname)

        grp = Group.objects.get(name=self.grp_name)
        feed.grant_group_permission(grp)

        gp = FeedGroupPermission.objects.get(group=grp, feed=feed)

        self.read_delete_url = reverse("feedgrouppermission-detail",
                                              kwargs={"pk": gp.id})

    def tearDown(self):
        grp = Group.objects.get(name=self.grp_name)
        feed = Feed.objects.get(name=self.feedname)
        feed.remove_group_permission(grp)
        #feed.folder.remove_shared_link()

        super(FeedGroupPermissionDetailViewTests, self).tearDown()

    def test_feed_group_permission_detail_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_delete_url)
        self.assertContains(response, 'all_users')
        self.assertContains(response, self.feedname)

    def test_feed_group_permission_detail_shared_success(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.read_delete_url)
        self.assertContains(response, 'all_users')
        self.assertContains(response, self.feedname)

    def test_feed_group_permission_detail_failure_unauthenticated(self):
        response = self.client.get(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_feed_group_permission_delete_success(self):
        feed = Feed.objects.get(name=self.feedname)
        grp = Group.objects.get(name='pacs_users')

        # create a group permission
        feed.grant_group_permission(grp)
        gp = FeedGroupPermission.objects.get(group=grp, feed=feed)

        read_update_delete_url = reverse("feedgrouppermission-detail",
                                         kwargs={"pk": gp.id})
        self.client.login(username=self.username, password=self.password)
        response = self.client.delete(read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_feed_group_permission_delete_failure_unauthenticated(self):
        response = self.client.delete(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_feed_group_permission_delete_failure_user_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.delete(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class FeedUserPermissionListViewTests(ViewTests):
    """
    Test the 'feeduserpermission-list' view.
    """

    def setUp(self):
        super(FeedUserPermissionListViewTests, self).setUp()

        feed = Feed.objects.get(name=self.feedname)

        self.create_read_url = reverse('feeduserpermission-list',
                                       kwargs={"pk": feed.id})
        self.post = json.dumps(
            {"template":
                 {"data": [{"name": "username", "value": self.other_username}]}})

    def test_feed_user_permission_create_success(self):
        self.client.login(username=self.username, password=self.password)

        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        feed = Feed.objects.get(name=self.feedname)

        self.assertIn(self.other_username, [u.username for u in feed.shared_users.all()])
        self.assertIn(self.other_username, [u.username for u in
                                            feed.folder.shared_users.all()])

        other_user = User.objects.get(username=self.other_username)
        feed.remove_user_permission(other_user)
        feed.folder.remove_shared_link()

    def test_feed_user_permission_create_failure_unauthenticated(self):
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_feed_user_permission_create_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_feed_user_permission_shared_create_failure_access_denied(self):
        other_user = User.objects.get(username=self.other_username)
        feed = Feed.objects.get(name=self.feedname)
        feed.grant_user_permission(other_user)

        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        feed.remove_user_permission(other_user)

    def test_feed_user_permission_list_success(self):
        other_user = User.objects.get(username=self.other_username)
        feed = Feed.objects.get(name=self.feedname)
        feed.grant_user_permission(other_user)

        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.create_read_url)
        self.assertContains(response, self.other_username)

        feed.remove_user_permission(other_user)

    def test_feed_user_permission_list_failure_unauthenticated(self):
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_feed_user_permission_list_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_feed_user_permission_shared_user_list_success(self):
        other_user = User.objects.get(username=self.other_username)
        feed = Feed.objects.get(name=self.feedname)
        feed.grant_user_permission(other_user)

        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        feed.remove_user_permission(other_user)


class FeedUserPermissionListQuerySearchViewTests(ViewTests):
    """
    Test the 'feeduserpermission-list-query-search' view.
    """

    def setUp(self):
        super(FeedUserPermissionListQuerySearchViewTests, self).setUp()

        feed = Feed.objects.get(name=self.feedname)

        self.read_url = reverse('feeduserpermission-list-query-search',
                                kwargs={"pk": feed.id})

        other_user = User.objects.get(username=self.other_username)
        feed.grant_user_permission(other_user)

    def tearDown(self):
        other_user = User.objects.get(username=self.other_username)
        feed = Feed.objects.get(name=self.feedname)
        feed.remove_user_permission(other_user)

        super(FeedUserPermissionListQuerySearchViewTests, self).tearDown()

    def test_feed_user_permission_list_query_search_success(self):
        read_url = f'{self.read_url}?username={self.other_username}'

        self.client.login(username=self.username, password=self.password)
        response = self.client.get(read_url)
        self.assertContains(response, self.other_username)

    def test_feed_user_permission_list_query_search_success_shared(self):
        read_url = f'{self.read_url}?username={self.other_username}'

        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(read_url)
        self.assertContains(response, self.other_username)

    def test_feed_user_permission_list_query_search_failure_unauthenticated(self):
        read_url = f'{self.read_url}?username={self.other_username}'

        response = self.client.get(read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class FeedUserPermissionDetailViewTests(ViewTests):
    """
    Test the feeduserpermission-detail view.
    """

    def setUp(self):
        super(FeedUserPermissionDetailViewTests, self).setUp()
        other_user = User.objects.get(username=self.other_username)

        feed = Feed.objects.get(name=self.feedname)
        feed.grant_user_permission(other_user)

        up = FeedUserPermission.objects.get(user=other_user, feed=feed)

        self.read_delete_url = reverse("feeduserpermission-detail",
                                              kwargs={"pk": up.id})

    def tearDown(self):
        other_user = User.objects.get(username=self.other_username)
        feed = Feed.objects.get(name=self.feedname)
        feed.remove_user_permission(other_user)

        super(FeedUserPermissionDetailViewTests, self).tearDown()

    def test_feed_user_permission_detail_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_delete_url)
        self.assertContains(response, self.other_username)
        self.assertContains(response, self.feedname)

    def test_feed_user_permission_detail_shared_success(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.read_delete_url)
        self.assertContains(response, self.other_username)
        self.assertContains(response, self.feedname)

    def test_feed_user_permission_detail_failure_unauthenticated(self):
        response = self.client.get(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_feed_user_permission_delete_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.delete(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_feed_user_permission_delete_failure_unauthenticated(self):
        response = self.client.delete(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_feed_user_permission_delete_failure_user_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.delete(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class CommentListViewTests(ViewTests):
    """
    Test the comment-list view.
    """

    def setUp(self):
        super(CommentListViewTests, self).setUp()
        feed = Feed.objects.get(name=self.feedname)
        self.corresponding_feed_url = reverse("feed-detail", kwargs={"pk": feed.id})
        self.create_read_url = reverse("comment-list", kwargs={"pk": feed.id})
        self.post = json.dumps(
            {"template": {"data": [{"name": "title", "value": "Comment1"}]}})

        # create two comments
        user = User.objects.get(username=self.username)
        Comment.objects.get_or_create(title="Comment2",feed=feed, owner=user)
        Comment.objects.get_or_create(title="Comment3",feed=feed, owner=user)

    def test_comment_create_success_related_feed_owner(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["title"], "Comment1")
        self.assertTrue(response.data["feed"].endswith(self.corresponding_feed_url))

    def test_comment_create_failure_not_related_feed_owner(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_comment_create_failure_unauthenticated(self):
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_comment_list_success_related_feed_owner(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.create_read_url)
        self.assertContains(response, "Comment2")
        self.assertContains(response, "Comment3")

    def test_comment_list_failure_not_related_feed_owner(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_comment_list_failure_unauthenticated(self):
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        

class CommentDetailViewTests(ViewTests):
    """
    Test the comment-detail view.
    """

    def setUp(self):
        super(CommentDetailViewTests, self).setUp()
        feed = Feed.objects.get(name=self.feedname)
        self.corresponding_feed_url = reverse("feed-detail", kwargs={"pk": feed.id})

        # create a comment
        user = User.objects.get(username=self.username)
        (comment, tf) = Comment.objects.get_or_create(title="Comment1",
                                                      feed=feed, owner=user)

        self.read_update_delete_url = reverse("comment-detail", kwargs={"pk": comment.id})       
        self.put = json.dumps({
            "template": {"data": [{"name": "title", "value": "Updated"}]}})
          
    def test_comment_detail_success_related_feed_owner(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_update_delete_url)
        self.assertContains(response, "Comment1")
        self.assertTrue(response.data["feed"].endswith(self.corresponding_feed_url))

    def test_comment_detail_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_comment_detail_failure_unauthenticated(self):
        response = self.client.get(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_comment_update_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertContains(response, "Updated")

    def test_comment_update_failure_unauthenticated(self):
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_comment_update_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_comment_delete_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Comment.objects.count(), 0)

    def test_comment_delete_failure_unauthenticated(self):
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_comment_delete_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        

class TagListViewTests(ViewTests):
    """
    Test the tag-list view.
    """

    def setUp(self):
        super(TagListViewTests, self).setUp()
        self.create_read_url = reverse("tag-list")
        self.post = json.dumps(
            {"template": {"data": [{"name": "name", "value": "Tag1"},
                                   {"name": "color", "value": "Black"}]}})

        # create two tags
        user = User.objects.get(username=self.username)
        Tag.objects.get_or_create(name="Tag2", color="blue", owner=user)
        Tag.objects.get_or_create(name="Tag3", color="red", owner=user)
        
    def test_tag_create_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["name"], "Tag1")

    def test_tag_create_failure_unauthenticated(self):
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_tag_list_success(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.create_read_url)
        self.assertContains(response, "Tag2")
        self.assertContains(response, "Tag3")

    def test_tag_list_success_unauthenticated(self):
        response = self.client.get(self.create_read_url)
        self.assertContains(response, "Tag2")
        self.assertContains(response, "Tag3")


class TagDetailViewTests(ViewTests):
    """
    Test the tag-detail view.
    """

    def setUp(self):
        super(TagDetailViewTests, self).setUp()

        # create a tag
        user = User.objects.get(username=self.username)
        (tag, tf) = Tag.objects.get_or_create(name="Tag1", color="blue", owner=user)

        self.read_update_delete_url = reverse("tag-detail", kwargs={"pk": tag.id})
        self.put = json.dumps({
            "template": {"data": [{"name": "name", "value": "Updated"},
                                  {"name": "color", "value": "black"}]}})

    def test_tag_detail_success(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.read_update_delete_url)
        self.assertContains(response, "Tag1")

    def test_tag_detail_success_unauthenticated(self):
        response = self.client.get(self.read_update_delete_url)
        self.assertContains(response, "Tag1")

    def test_tag_update_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertContains(response, "Updated")

    def test_tag_update_failure_unauthenticated(self):
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_tag_update_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.put(self.read_update_delete_url, data=self.put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_tag_delete_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Tag.objects.count(), 0)

    def test_tag_delete_failure_unauthenticated(self):
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_tag_delete_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class FeedTagListViewTests(ViewTests):
    """
    Test the feed-tag-list view.
    """

    def setUp(self):
        super(FeedTagListViewTests, self).setUp()
        feed = Feed.objects.get(name=self.feedname)
        self.list_url = reverse("feed-tag-list", kwargs={"pk": feed.id})

        # create Tag2 tag
        user = User.objects.get(username=self.username)
        (tag, tf) = Tag.objects.get_or_create(name="Tag2", color="blue", owner=user)

        # tag self.feedname with Tag2
        Tagging.objects.get_or_create(tag=tag, feed=feed)

        # create a new feed by creating a "fs" plugin instance

        plugin = Plugin.objects.get(meta__name="pacspull")
        pl_inst = PluginInstance.objects.create(
            plugin=plugin, owner=user, compute_resource=plugin.compute_resources.all()[0])
        pl_inst.feed.name = "new"
        pl_inst.feed.save()

        # create Tag3 tag
        (tag, tf) = Tag.objects.get_or_create(name="Tag3", color="red", owner=user)

        # tag the new feed with Tag3
        feed = Feed.objects.get(name="new")
        Tagging.objects.get_or_create(tag=tag, feed=feed)

    def test_feed_tag_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.list_url)
        self.assertContains(response, "Tag2")
        self.assertNotContains(response, "Tag3") # tag list is feed-specific

    def test_feed_tag_list_failure_unauthenticated(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_feed_tag_list_failure_not_owner(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class TagFeedListViewTests(ViewTests):
    """
    Test the tag-feed-list view.
    """

    def setUp(self):
        super(TagFeedListViewTests, self).setUp()

        # create Tag2 tag
        user = User.objects.get(username=self.username)
        (tag, tf) = Tag.objects.get_or_create(name="Tag2", color="blue", owner=user)

        self.list_url = reverse("tag-feed-list", kwargs={"pk": tag.id})

        # tag self.feedname with Tag2
        feed = Feed.objects.get(name=self.feedname)
        Tagging.objects.get_or_create(tag=tag, feed=feed)

        # create a new feed by creating a "fs" plugin instance
        plugin = Plugin.objects.get(meta__name="pacspull")
        pl_inst = PluginInstance.objects.create(
            plugin=plugin, owner=user, compute_resource=plugin.compute_resources.all()[0])
        pl_inst.feed.name = "new"
        pl_inst.feed.save()

        # create Tag3 tag
        (tag, tf) = Tag.objects.get_or_create(name="Tag3", color="red", owner=user)

        # tag the new feed with Tag3
        feed = Feed.objects.get(name="new")
        Tagging.objects.get_or_create(tag=tag, feed=feed)

    def test_tag_feed_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.list_url)
        self.assertContains(response, self.feedname)
        self.assertNotContains(response, "new")  # feed list is tag-specific

    def test_tag_feed_list_query_count_does_not_grow_with_feeds(self):
        self.client.login(username=self.username, password=self.password)
        tag = Tag.objects.get(name="Tag2")

        def add_tagged_feeds():
            for feed in self.create_feeds(3):
                Tagging.objects.create(tag=tag, feed=feed)

        self.assertQueriesPerListItem(self.list_url, add_tagged_feeds)

    def test_tag_feed_list_success_unauthenticated(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['results'], [])

    def test_tag_feed_list_failure_not_owner(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['results'], [])


class FeedTaggingListViewTests(ViewTests):
    """
    Test the feed-tagging-list view.
    """

    def setUp(self):
        super(FeedTaggingListViewTests, self).setUp()

        feed = Feed.objects.get(name=self.feedname)
        self.create_read_url = reverse("feed-tagging-list", kwargs={"pk": feed.id})

        # create Tag1 tag
        user = User.objects.get(username=self.username)
        (tag, tf) = Tag.objects.get_or_create(name="Tag1", color="green", owner=user)

        self.post = json.dumps(
            {"template": {"data": [{"name": "tag_id", "value": tag.id}]}})

        # create Tag2 tag
        user = User.objects.get(username=self.username)
        (tag, tf) = Tag.objects.get_or_create(name="Tag2", color="blue", owner=user)

        # tag self.feedname with Tag2
        Tagging.objects.get_or_create(tag=tag, feed=feed)

    def test_feed_tagging_create_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        tag = Tag.objects.get(name="Tag1")
        self.assertEqual(response.data["tag_id"], tag.id)
        feed = Feed.objects.get(name=self.feedname)
        self.assertEqual(response.data["feed_id"], feed.id)

    def test_feed_tagging_create_failure_unauthenticated(self):
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_feed_tagging_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.create_read_url)
        tag = Tag.objects.get(name="Tag2")
        self.assertContains(response, tag.id)

    def test_feed_tagging_list_failure_unauthenticated(self):
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_feed_tagging_list_failure_not_owner(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class TagTaggingListViewTests(ViewTests):
    """
    Test the tag-tagging-list view.
    """

    def setUp(self):
        super(TagTaggingListViewTests, self).setUp()

        feed = Feed.objects.get(name=self.feedname)

        # create Tag2 tag
        user = User.objects.get(username=self.username)
        (tag, tf) = Tag.objects.get_or_create(name="Tag2", color="blue", owner=user)

        self.create_read_url = reverse("tag-tagging-list", kwargs={"pk": tag.id})

        self.post = json.dumps(
            {"template": {"data": [{"name": "feed_id", "value": feed.id}]}})

        # create a new feed by creating a "fs" plugin instance
        plugin = Plugin.objects.get(meta__name="pacspull")
        pl_inst = PluginInstance.objects.create(
            plugin=plugin, owner=user, compute_resource=plugin.compute_resources.all()[0])
        pl_inst.feed.name = "new"
        pl_inst.feed.save()

        # tag new feed with Tag2
        feed = Feed.objects.get(name="new")
        Tagging.objects.get_or_create(tag=tag, feed=feed)

        # create Tag3 tag
        (tag, tf) = Tag.objects.get_or_create(name="Tag3", color="red", owner=user)

        # tag self.feedname with Tag3
        Tagging.objects.get_or_create(tag=tag, feed=feed)

    def test_tag_tagging_create_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        tag = Tag.objects.get(name="Tag2")
        self.assertEqual(response.data["tag_id"], tag.id)
        feed = Feed.objects.get(name=self.feedname)
        self.assertEqual(response.data["feed_id"], feed.id)

    def test_tag_tagging_create_failure_unauthenticated(self):
        response = self.client.post(self.create_read_url, data=self.post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_tag_tagging_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.create_read_url)
        feed = Feed.objects.get(name="new")
        self.assertContains(response, feed.id)
        feed = Feed.objects.get(name=self.feedname)
        self.assertNotContains(response, feed.id)

    def test_tag_tagging_list_success_unauthenticated(self):
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['results'], [])


class TaggingDetailViewTests(ViewTests):
    """
    Test the tagging-detail view.
    """

    def setUp(self):
        super(TaggingDetailViewTests, self).setUp()

        # create a tag
        user = User.objects.get(username=self.username)
        (tag, tf) = Tag.objects.get_or_create(name="Tag1", color="blue", owner=user)

        # tag self.feedname with Tag1
        feed = Feed.objects.get(name=self.feedname)
        (tagging, tf) = Tagging.objects.get_or_create(tag=tag, feed=feed)

        self.read_delete_url = reverse("tagging-detail", kwargs={"pk": tagging.id})

    def test_tagging_detail_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_delete_url)
        tag = Tag.objects.get(name="Tag1")
        self.assertEqual(response.data["tag_id"], tag.id)
        feed = Feed.objects.get(name=self.feedname)
        self.assertContains(response, feed.id)

    def test_tagging_detail_failure_unauthenticated(self):
        response = self.client.get(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_tagging_detail_failure_not_related_tag_owner(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_tagging_delete_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.delete(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Tagging.objects.count(), 0)

    def test_tagging_delete_failure_unauthenticated(self):
        response = self.client.delete(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_tagging_delete_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.delete(self.read_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class FeedPluginInstanceListViewTests(ViewTests):
    """
    Test the feed-plugininstance-list view.
    """

    def setUp(self):
        super(FeedPluginInstanceListViewTests, self).setUp()
        feed = Feed.objects.get(name=self.feedname)
        self.list_url = reverse("feed-plugininstance-list", kwargs={"pk": feed.id})

        # create two files in the DB "already uploaded" to the server from two different
        # plugin instances that write to the same feed
        plg_inst = PluginInstance.objects.get(title='test')

        # create a second 'ds' plugin instance in the same feed tree
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name="mri_convert")
        PluginInstance.objects.get_or_create(
            plugin=plugin, owner=user, previous_id=plg_inst.id, title='test1',
            compute_resource=plugin.compute_resources.all()[0])

    def test_feed_plugin_instance_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.list_url)
        # it shows all the plugin instances associated to the feed
        self.assertContains(response, "test")
        self.assertContains(response, "test1")

    def test_feed_plugin_instance_list_query_count_does_not_grow_with_instances(self):
        # the related objects the serializer reads for every instance are fetched in
        # the list query itself
        self.client.login(username=self.username, password=self.password)
        plg_inst = PluginInstance.objects.get(title='test')
        user = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name="mri_convert")

        def add_instances():
            for i in range(3):
                PluginInstance.objects.create(
                    plugin=plugin, owner=user, previous=plg_inst, title=f'more{i}',
                    compute_resource=plugin.compute_resources.all()[0])

        self.assertQueriesPerListItem(self.list_url, add_instances)

    def test_feed_plugin_instance_list_failure_not_related_feed_owner(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_feed_plugin_instance_list_failure_unauthenticated(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
