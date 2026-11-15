
import json
import logging
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse
from rest_framework import status

from core.tests.query_counts import QueryCountAssertionsMixin
from pipelines.models import Pipeline, PluginPiping, DEFAULT_PIPING_PARAMETER_MODELS
from plugininstances.models import PluginInstance
from plugins.models import ComputeResource
from plugins.models import PluginMeta, Plugin
from plugins.models import PluginParameter, DefaultStrParameter, DefaultIntParameter
from workflows.models import Workflow
from workflows.views import WorkflowList

COMPUTE_RESOURCE_URL = settings.COMPUTE_RESOURCE_URL
CHRIS_SUPERUSER_PASSWORD = settings.CHRIS_SUPERUSER_PASSWORD


class ViewTests(QueryCountAssertionsMixin, TestCase):
    
    def setUp(self):
        # avoid cluttered console output (for instance logging all the http requests)
        logging.disable(logging.WARNING)

        # create superuser chris (owner of root folders)
        self.chris_username = 'chris'
        self.chris_password = CHRIS_SUPERUSER_PASSWORD

        self.content_type = 'application/vnd.collection+json'

        self.plugin_fs_name = "simplefsapp"
        self.plugin_fs_parameters = {'dir': {'type': 'string', 'optional': True,
                                             'default': "./"}}
        self.plugin_ds_name = "simpledsapp"
        self.plugin_ds_parameters = {'dummyInt': {'type': 'integer', 'optional': True,
                                                  'default': 111111}}
        self.username = 'foo'
        self.password = 'foo-pass'

        (self.compute_resource, tf) = ComputeResource.objects.get_or_create(
            name="host", compute_url=COMPUTE_RESOURCE_URL,
            compute_requires_copy_job=False)

        # create plugins
        (pl_meta, tf) = PluginMeta.objects.get_or_create(name=self.plugin_fs_name, type='fs')
        (plugin_fs, tf) = Plugin.objects.get_or_create(meta=pl_meta, version='0.1')
        plugin_fs.compute_resources.set([self.compute_resource])
        plugin_fs.save()

        # add plugins' parameters
        (plg_param_fs, tf) = PluginParameter.objects.get_or_create(
            plugin=plugin_fs,
            name='dir',
            type=self.plugin_fs_parameters['dir']['type'],
            optional=self.plugin_fs_parameters['dir']['optional'])
        default = self.plugin_fs_parameters['dir']['default']
        DefaultStrParameter.objects.get_or_create(plugin_param=plg_param_fs,
                                                   value=default)  # set plugin parameter default

        (pl_meta, tf) = PluginMeta.objects.get_or_create(name=self.plugin_ds_name, type='ds')
        (plugin_ds, tf) = Plugin.objects.get_or_create(meta=pl_meta, version='0.1')
        plugin_ds.compute_resources.set([self.compute_resource])
        plugin_ds.save()

        # add a parameter with a default
        (plg_param_ds, tf)= PluginParameter.objects.get_or_create(
            plugin=plugin_ds,
            name='dummyInt',
            type=self.plugin_ds_parameters['dummyInt']['type'],
            optional=self.plugin_ds_parameters['dummyInt']['optional']
        )
        default = self.plugin_ds_parameters['dummyInt']['default']
        DefaultIntParameter.objects.get_or_create(plugin_param=plg_param_ds,
                                                  value=default)  # set plugin parameter default

        (pl_meta, tf) = PluginMeta.objects.get_or_create(name='ts_copy', type='ts')
        (plugin_ts, tf) = Plugin.objects.get_or_create(meta=pl_meta, version='0.1')
        plugin_ts.compute_resources.set([self.compute_resource])
        plugin_ts.save()

        # add a parameter with a default
        (plg_param_ts, tf)= PluginParameter.objects.get_or_create(
            plugin=plugin_ts,
            name='plugininstances',
            type='string',
            optional=True
        )
        default = ""
        DefaultStrParameter.objects.get_or_create(plugin_param=plg_param_ts,
                                                  value=default)  # set plugin parameter default

        # create user
        user = User.objects.create_user(username=self.username, password=self.password)

        # create a pipeline
        self.pipeline_name = 'Pipeline1'
        (pipeline, tf) = Pipeline.objects.get_or_create(name=self.pipeline_name,
                                                        owner=user, category='test')

        # create three plugin pipings
        self.pips = []
        (pip1, tf) = PluginPiping.objects.get_or_create(title='pip_ds1', plugin=plugin_ds,
                                                       pipeline=pipeline)
        self.pips.append(pip1)
        (pip2, tf) = PluginPiping.objects.get_or_create(title='pip_ds2', plugin=plugin_ds,
                                                       previous=pip1, pipeline=pipeline)
        self.pips.append(pip2)
        (pip3, tf) = PluginPiping.objects.get_or_create(title='pip_ts', plugin=plugin_ts,
                                                        previous=pip1, pipeline=pipeline)
        self.pips.append(pip3)
        default_model_class = DEFAULT_PIPING_PARAMETER_MODELS['string']
        (default_piping_param, tf) = default_model_class.objects.get_or_create(
                plugin_piping=pip3, plugin_param=plg_param_ts)
        default_piping_param.value = f"{pip1.id},{pip2.id}"
        default_piping_param.save()

        # create another user
        self.other_username = 'boo'
        self.other_password = 'far'
        User.objects.create_user(username=self.other_username, password=self.other_password)

    def tearDown(self):
        # re-enable logging
        logging.disable(logging.NOTSET)


class WorkflowListViewTests(ViewTests):
    """
    Test the workflow-list view.
    """

    def setUp(self):
        super(WorkflowListViewTests, self).setUp()
        pipeline = Pipeline.objects.get(name=self.pipeline_name)
        self.create_read_url = reverse("workflow-list", kwargs={"pk": pipeline.id})
        owner = User.objects.get(username=self.username)
        plugin_fs = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (self.pl_inst, tf) = PluginInstance.objects.get_or_create(
            status='finishedSuccessfully',
            plugin=plugin_fs, owner=owner,
            compute_resource=plugin_fs.compute_resources.all()[0]
        )

    def test_workflow_create_success(self):
        post = json.dumps(
            {"template": {"data": [{"name": "previous_plugin_inst_id", "value": self.pl_inst.id},
                                   {"name": "nodes_info",
                                    "value": json.dumps([{"piping_id": self.pips[0].id,
                                           "compute_resource_name": "host", "title": "Inst_ds1",
                                           "plugin_parameter_defaults": [
                                               {"name": "dummyInt", "default": 3}]},
                                          {"piping_id": self.pips[1].id, "title": "Inst_ds2",
                                           "compute_resource_name": "host"},
                                          {"piping_id": self.pips[2].id, "title": "Inst_ts",
                                            "compute_resource_name": "host"}])}]}})

        plg_instances_count = PluginInstance.objects.count()
        with mock.patch('plugininstances.tasks.run_plugin_instance_job'):
            self.client.login(username=self.username, password=self.password)
            response = self.client.post(self.create_read_url, data=post,
                                        content_type=self.content_type)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            self.assertEqual(PluginInstance.objects.count(), plg_instances_count + 3)

            inst_ds1 = PluginInstance.objects.get(title="Inst_ds1")
            inst_ds2 = PluginInstance.objects.get(title="Inst_ds2")
            inst_ts = PluginInstance.objects.get(title="Inst_ts")
            param = inst_ts.string_param.filter(
                plugin_param__name='plugininstances').first()
            parent_ids = [int(parent_id) for parent_id in param.value.split(',')]
            for inst_id in parent_ids:
                self.assertIn(inst_id, [inst_ds1.id, inst_ds2.id])

    def test_workflow_create_with_resource_overrides(self):
        """
        cpu_limit/memory_limit/number_of_workers/gpu_limit overrides on the
        nodes_info entry propagate to the created plugin instance.
        """
        post = json.dumps(
            {"template": {"data": [{"name": "previous_plugin_inst_id", "value": self.pl_inst.id},
                                   {"name": "nodes_info",
                                    "value": json.dumps([
                                        {"piping_id": self.pips[0].id,
                                         "title": "Inst_ds1",
                                         "compute_resource_name": "host",
                                         "cpu_limit": "2000m",
                                         "memory_limit": "1Gi",
                                         "number_of_workers": 2,
                                         "gpu_limit": 0,
                                         "plugin_parameter_defaults": [
                                             {"name": "dummyInt", "default": 3}]},
                                        {"piping_id": self.pips[1].id,
                                         "title": "Inst_ds2",
                                         "compute_resource_name": "host"},
                                        {"piping_id": self.pips[2].id,
                                         "title": "Inst_ts",
                                         "compute_resource_name": "host"}])}]}})

        with mock.patch('plugininstances.tasks.run_plugin_instance_job'):
            self.client.login(username=self.username, password=self.password)
            response = self.client.post(self.create_read_url, data=post,
                                        content_type=self.content_type)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)

            inst_ds1 = PluginInstance.objects.get(title="Inst_ds1")
            self.assertEqual(int(inst_ds1.cpu_limit), 2000)
            self.assertEqual(int(inst_ds1.memory_limit), 1024)
            self.assertEqual(inst_ds1.number_of_workers, 2)
            self.assertEqual(inst_ds1.gpu_limit, 0)

            # the piping with no override gets the piping's stored defaults
            inst_ds2 = PluginInstance.objects.get(title="Inst_ds2")
            self.assertEqual(int(inst_ds2.cpu_limit), int(self.pips[1].cpu_limit))
            self.assertEqual(int(inst_ds2.memory_limit), int(self.pips[1].memory_limit))
            self.assertEqual(inst_ds2.number_of_workers, self.pips[1].number_of_workers)
            self.assertEqual(inst_ds2.gpu_limit, self.pips[1].gpu_limit)

    def test_workflow_create_rejects_out_of_range_resource(self):
        """
        A resource override outside the plugin's allowed range yields HTTP 400.
        """
        post = json.dumps(
            {"template": {"data": [{"name": "previous_plugin_inst_id", "value": self.pl_inst.id},
                                   {"name": "nodes_info",
                                    "value": json.dumps([
                                        {"piping_id": self.pips[0].id,
                                         "cpu_limit": 500},  # below default min 1000m
                                    ])}]}})

        self.client.login(username=self.username, password=self.password)
        response = self.client.post(self.create_read_url, data=post,
                                    content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_workflow_list_success(self):
        pipeline = Pipeline.objects.get(name=self.pipeline_name)
        owner = User.objects.get(username=self.username)
        Workflow.objects.get_or_create(title='Workflow1', pipeline=pipeline, owner=owner)
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.create_read_url)
        self.assertContains(response, 'Workflow1')

    def test_workflow_list_failure_unauthenticated(self):
        response = self.client.get(self.create_read_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class WorkflowListQuerySearchViewTests(ViewTests):
    """
    Test the workflow-list-query-search view.
    """

    def setUp(self):
        super(WorkflowListQuerySearchViewTests, self).setUp()

        self.query_url = reverse("allworkflow-list-query-search") + f"?owner_username={self.username}"

    def test_workflow_query_search_list_success(self):
        pipeline = Pipeline.objects.get(name=self.pipeline_name)
        owner = User.objects.get(username=self.username)
        Workflow.objects.get_or_create(pipeline=pipeline, owner=owner)
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.query_url)
        self.assertContains(response, self.username)

    def test_workflow_query_search_list_failure_unauthenticated(self):
        response = self.client.get(self.query_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class WorkflowDetailViewTests(ViewTests):
    """
    Test the workflow-detail view.
    """

    def setUp(self):
        super(WorkflowDetailViewTests, self).setUp()
        pipeline = Pipeline.objects.get(name=self.pipeline_name)
        owner = User.objects.get(username=self.username)
        (workflow, tf) = Workflow.objects.get_or_create(
            title='Workflow2', pipeline=pipeline, owner=owner)
        self.workflow = workflow
        self.read_update_delete_url = reverse("workflow-detail",
                                              kwargs={"pk": workflow.id})

    def test_workflow_detail_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.read_update_delete_url)
        self.assertContains(response, 'Workflow2')

    def test_workflow_detail_failure_unauthenticated(self):
        response = self.client.get(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_workflow_update_success(self):
        put = json.dumps({
            "template": {"data": [{"name": "title", "value": "Workflow3"}]}})

        self.client.login(username=self.username, password=self.password)
        response = self.client.put(self.read_update_delete_url, data=put,
                                   content_type=self.content_type)
        self.assertContains(response, "Workflow3")

    def test_workflow_update_failure_unauthenticated(self):
        put = json.dumps({
            "template": {"data": [{"name": "title", "value": "Workflow3"}]}})

        response = self.client.put(self.read_update_delete_url, data=put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_workflow_update_failure_access_denied(self):
        put = json.dumps({
            "template": {"data": [{"name": "title", "value": "Workflow3"}]}})

        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.put(self.read_update_delete_url, data=put,
                                   content_type=self.content_type)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_workflow_delete_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Workflow.objects.count(), 0)

    def test_workflow_delete_cancels_its_instances_and_arms_their_remote_cleanup(self):
        owner = User.objects.get(username=self.username)
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name)
        instances = {}
        for status_ in ('started', 'finishedSuccessfully'):
            inst = PluginInstance.objects.create(
                plugin=plugin, owner=owner, workflow=self.workflow, title=status_,
                compute_resource=self.compute_resource)
            PluginInstance.objects.filter(pk=inst.pk).update(status=status_)
            instances[status_] = inst.pk

        self.client.login(username=self.username, password=self.password)
        response = self.client.delete(self.read_update_delete_url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        rows = dict((pk, (s, c)) for pk, s, c in PluginInstance.objects.filter(
            pk__in=instances.values()).values_list('pk', 'status', 'remote_cleanup_status'))
        # the one that had a job in the remote gets its cleanup armed in the same UPDATE
        self.assertEqual(rows, {instances['started']: ('cancelled', 'deletingData'),
                                instances['finishedSuccessfully']: ('cancelled',
                                                                    'notStarted')})

    def test_workflow_delete_failure_unauthenticated(self):
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_workflow_delete_failure_access_denied(self):
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.delete(self.read_update_delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class WorkflowPluginInstanceListViewTests(ViewTests):
    """
    Test the workflow-plugininstance-list view.
    """

    def setUp(self):
        super(WorkflowPluginInstanceListViewTests, self).setUp()
        pipeline = Pipeline.objects.get(name=self.pipeline_name)
        owner = User.objects.get(username=self.username)
        (self.workflow, tf) = Workflow.objects.get_or_create(title='Workflow4',
                                                             pipeline=pipeline,
                                                             owner=owner)
        self.list_url = reverse("workflow-plugininstance-list",
                                kwargs={"pk": self.workflow.id})

        # the workflow's plugin instances run after an 'fs' plugin instance, in a
        # private feed of the owner
        plugin_fs = Plugin.objects.get(meta__name=self.plugin_fs_name)
        self.root = PluginInstance.objects.create(
            plugin=plugin_fs, owner=owner, title='root',
            compute_resource=plugin_fs.compute_resources.all()[0])
        self.create_workflow_instances(2)

    def create_workflow_instances(self, count):
        """
        Custom method to create count 'ds' plugin instances of the workflow after the
        root plugin instance.
        """
        owner = User.objects.get(username=self.username)
        plugin_ds = Plugin.objects.get(meta__name=self.plugin_ds_name)
        for i in range(count):
            PluginInstance.objects.create(
                plugin=plugin_ds, owner=owner, previous=self.root, workflow=self.workflow,
                title=f'ds{i}', compute_resource=plugin_ds.compute_resources.all()[0])

    def test_workflow_plugin_instance_list_success(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 2)

    def test_workflow_plugin_instance_list_query_count_does_not_grow_with_instances(self):
        # the related objects the serializer reads for every instance, the workflow's
        # pipeline among them, are fetched in the list query itself
        self.client.login(username=self.username, password=self.password)
        response = self.assertQueriesPerListItem(
            self.list_url, lambda: self.create_workflow_instances(3))
        self.assertEqual(response.data['results'][0]['pipeline_name'], self.pipeline_name)

    def test_workflow_plugin_instance_list_success_chris_user_lists_all(self):
        self.client.login(username=self.chris_username, password=self.chris_password)
        response = self.client.get(self.list_url)
        self.assertEqual(len(response.data['results']), 2)

    def test_workflow_plugin_instance_list_success_hides_instances_of_unshared_feed(self):
        # any authenticated user can read the workflow, but not the plugin instances
        # in a feed that is neither public nor shared with the user
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['results'], [])

    def test_workflow_plugin_instance_list_success_shared_feed(self):
        other_user = User.objects.get(username=self.other_username)
        self.root.feed.grant_user_permission(other_user)
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.list_url)
        self.assertEqual(len(response.data['results']), 2)

    def test_workflow_plugin_instance_list_success_public_feed(self):
        self.root.feed.public = True
        self.root.feed.save()
        self.client.login(username=self.other_username, password=self.other_password)
        response = self.client.get(self.list_url)
        self.assertEqual(len(response.data['results']), 2)

    def test_workflow_plugin_instance_list_lists_once_an_instance_shared_many_times(self):
        other_user = User.objects.get(username=self.other_username)
        group = Group.objects.create(name='lab')
        other_user.groups.add(group)
        third_user = User.objects.create_user(username='third',
                                              password=self.other_password)
        feed = self.root.feed
        feed.grant_user_permission(other_user)
        feed.grant_user_permission(third_user)
        feed.grant_group_permission(group)

        for (username, password) in ((self.username, self.password),
                                     (self.other_username, self.other_password)):
            with self.subTest(username=username):
                self.client.login(username=username, password=password)
                response = self.client.get(self.list_url)
                ids = [plg_inst['id'] for plg_inst in response.data['results']]
                self.assertEqual(len(ids), 2)
                self.assertEqual(len(set(ids)), 2)

    def test_workflow_plugin_instance_list_failure_unauthenticated(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class WorkflowListPerformCreateHelperTests(ViewTests):
    """
    Direct unit tests for the private helpers extracted from
    ``WorkflowList.perform_create``.
    """

    def setUp(self):
        super(WorkflowListPerformCreateHelperTests, self).setUp()
        self.view = WorkflowList()
        self.view.request = mock.Mock()
        self.view.request.user = User.objects.get(username=self.username)
        self.pipeline = Pipeline.objects.get(name=self.pipeline_name)

        plugin_fs = Plugin.objects.get(meta__name=self.plugin_fs_name)
        (self.previous_plugin_inst, _) = PluginInstance.objects.get_or_create(
            status='finishedSuccessfully', plugin=plugin_fs,
            owner=self.view.request.user,
            compute_resource=plugin_fs.compute_resources.all()[0])

        (self.workflow, _) = Workflow.objects.get_or_create(
            title='HelperWf', pipeline=self.pipeline, owner=self.view.request.user)

    # ---- _resolve_workflow_title -------------------------------------------------

    def test__resolve_workflow_title_uses_supplied(self):
        serializer = mock.Mock()
        serializer.validated_data = {'title': 'CustomTitle'}
        self.assertEqual(
            WorkflowList._resolve_workflow_title(serializer, self.pipeline),
            'CustomTitle')

    def test__resolve_workflow_title_falls_back_to_pipeline_name(self):
        serializer = mock.Mock()
        serializer.validated_data = {'title': None}
        self.assertEqual(
            WorkflowList._resolve_workflow_title(serializer, self.pipeline),
            self.pipeline.name)

    def test__resolve_workflow_title_missing_key_falls_back(self):
        serializer = mock.Mock()
        serializer.validated_data = {}
        self.assertEqual(
            WorkflowList._resolve_workflow_title(serializer, self.pipeline),
            self.pipeline.name)

    # ---- _build_inst_templates ---------------------------------------------------

    def _make_canonical_nodes(self):
        """Mimic the canonical output of WorkflowSerializer.validate_nodes_info."""
        return [{"piping_id": pip.id,
                 "compute_resource_name": "host",
                 "title": pip.title,
                 "plugin_parameter_defaults": [],
                 "cpu_limit": None,
                 "memory_limit": None,
                 "number_of_workers": None,
                 "gpu_limit": None}
                for pip in self.pips]

    def test__build_inst_templates_returns_one_template_per_piping(self):
        nodes = self._make_canonical_nodes()
        tree, root_id, inst_data = WorkflowList._build_inst_templates(
            self.pipeline, nodes)
        self.assertEqual(set(inst_data.keys()),
                         {pip.id for pip in self.pips})
        self.assertIn(root_id, tree)
        # root_id corresponds to the only piping with no previous (pip1).
        self.assertEqual(root_id, self.pips[0].id)

    # ---- _create_plugin_instance_tree --------------------------------------------

    def test__create_plugin_instance_tree_creates_one_per_piping(self):
        nodes = self._make_canonical_nodes()
        tree, root_id, inst_data = WorkflowList._build_inst_templates(
            self.pipeline, nodes)
        before = PluginInstance.objects.count()
        result = self.view._create_plugin_instance_tree(
            tree, root_id, inst_data, self.previous_plugin_inst, self.workflow)
        self.assertEqual(PluginInstance.objects.count(), before + len(self.pips))
        self.assertEqual(set(result.keys()), {pip.id for pip in self.pips})
        # The root's previous is the supplied previous instance.
        self.assertEqual(result[root_id].previous, self.previous_plugin_inst)
        # Every non-root child's previous is its parent piping's plugin instance.
        for parent_id, node in tree.items():
            for child_id in node['child_ids']:
                self.assertEqual(result[child_id].previous, result[parent_id])

    # ---- _rewrite_ts_parent_ids --------------------------------------------------

    def test__rewrite_ts_parent_ids_translates_piping_ids(self):
        nodes = self._make_canonical_nodes()
        tree, root_id, inst_data = WorkflowList._build_inst_templates(
            self.pipeline, nodes)
        plugin_instances_dict = self.view._create_plugin_instance_tree(
            tree, root_id, inst_data, self.previous_plugin_inst, self.workflow)

        ts_pip = self.pips[2]  # the 'ts' piping built in ViewTests.setUp
        ts_inst = plugin_instances_dict[ts_pip.id]
        # Before: param value is the comma-separated piping ids set in setUp.
        param_before = ts_inst.string_param.filter(
            plugin_param__name='plugininstances').first()
        self.assertEqual(param_before.value,
                         f"{self.pips[0].id},{self.pips[1].id}")

        WorkflowList._rewrite_ts_parent_ids(ts_inst, plugin_instances_dict)

        param_after = ts_inst.string_param.filter(
            plugin_param__name='plugininstances').first()
        expected_ids = (f"{plugin_instances_dict[self.pips[0].id].id},"
                        f"{plugin_instances_dict[self.pips[1].id].id}")
        self.assertEqual(param_after.value, expected_ids)

    def test__rewrite_ts_parent_ids_no_op_when_param_missing(self):
        """If no 'plugininstances' string param exists, the helper is a no-op."""
        plugin_ds = Plugin.objects.get(meta__name=self.plugin_ds_name)
        ds_inst = PluginInstance.objects.create(
            plugin=plugin_ds, owner=self.view.request.user,
            compute_resource=plugin_ds.compute_resources.all()[0],
            previous=self.previous_plugin_inst,
            workflow=self.workflow)
        # Should not raise even though plugin_instances_dict is empty.
        WorkflowList._rewrite_ts_parent_ids(ds_inst, {})

    # ---- _dispatch_runs ----------------------------------------------------------

    def test__dispatch_runs_calls_run_if_ready_for_every_instance(self):
        nodes = self._make_canonical_nodes()
        tree, root_id, inst_data = WorkflowList._build_inst_templates(
            self.pipeline, nodes)
        plugin_instances_dict = self.view._create_plugin_instance_tree(
            tree, root_id, inst_data, self.previous_plugin_inst, self.workflow)

        with mock.patch('workflows.views.run_if_ready') as run_mock:
            self.view._dispatch_runs(plugin_instances_dict)
        self.assertEqual(run_mock.call_count, len(plugin_instances_dict))
