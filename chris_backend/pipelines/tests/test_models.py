
import logging
from unittest import mock

from django.test import TestCase
from django.contrib.auth.models import User
from django.conf import settings

from core.models import ChrisFolder
from plugins.models import PluginMeta, Plugin
from plugins.models import ComputeResource
from plugins.models import PluginParameter, DefaultIntParameter
from pipelines.models import (Pipeline, PluginPiping, PipelineSourceFile,
                              PipelineSourceFileMeta)
from pipelines.models import DEFAULT_PIPING_PARAMETER_MODELS


COMPUTE_RESOURCE_URL = settings.COMPUTE_RESOURCE_URL


class ModelTests(TestCase):

    def setUp(self):
        # avoid cluttered console output (for instance logging all the http requests)
        logging.disable(logging.WARNING)

        self.plugin_ds_name = "simpledsapp"
        self.plugin_ds_parameters = {'prefix': {'type': 'string', 'optional': False}}
        self.username = 'foo'
        self.password = 'foo-pass'

        (self.compute_resource, tf) = ComputeResource.objects.get_or_create(
            name="host", compute_url=COMPUTE_RESOURCE_URL)

        # create plugin
        (pl_meta, tf) = PluginMeta.objects.get_or_create(name=self.plugin_ds_name, type='ds')
        (plugin_ds, tf) = Plugin.objects.get_or_create(meta=pl_meta, version='0.1')
        plugin_ds.compute_resources.set([self.compute_resource])
        plugin_ds.save()

        # add plugin's parameters
        (plg_param_ds, tf)= PluginParameter.objects.get_or_create(
            plugin=plugin_ds,
            name='prefix',
            type=self.plugin_ds_parameters['prefix']['type'],
            optional=self.plugin_ds_parameters['prefix']['optional'])  # this plugin parameter has no default

        # create user
        user = User.objects.create_user(username=self.username, password=self.password)

        # create a pipeline
        self.pipeline_name = 'Pipeline1'
        (pipeline, tf) = Pipeline.objects.get_or_create(name=self.pipeline_name, owner=user)

        # create two plugin pipings
        self.pips = []
        (pip, tf) = PluginPiping.objects.get_or_create(title='pip1', plugin=plugin_ds,
                                                       pipeline=pipeline)
        self.pips.append(pip)
        (pip, tf) = PluginPiping.objects.get_or_create(title='pip2', plugin=plugin_ds,
                                                       previous=pip, pipeline=pipeline)
        self.pips.append(pip)

        # create default values for the piping parameters as the corresponding plugin
        # didn't set a default
        param_type = self.plugin_ds_parameters['prefix']['type']
        default_model_class = DEFAULT_PIPING_PARAMETER_MODELS[param_type]
        for i in range(2):
            (default_piping_param, tf) = default_model_class.objects.get_or_create(
                plugin_piping=self.pips[i], plugin_param=plg_param_ds)
            if i == 0:
                default_piping_param.value = ""
            else:
                default_piping_param.value = "test" + str(i)
            default_piping_param.save()


    def tearDown(self):
        # re-enable logging
        logging.disable(logging.NOTSET)


class PipelineModelTests(ModelTests):

    def test_get_pipings_parameters_names(self):
        """
        Test whether custom get_pipings_parameters_names method returns the list of all
        the plugin parameter names for all the associated plugin pipings. The name of the
        parameters should be transformed to have the plugin id, piping id and previous
        piping id as a prefix.
        """
        plugin_ds = Plugin.objects.get(meta__name=self.plugin_ds_name)
        param = plugin_ds.parameters.get(name='prefix')
        pipeline = Pipeline.objects.get(name=self.pipeline_name)
        param_names = pipeline.get_pipings_parameters_names()
        self.assertEqual(len(param_names), 2)
        self.assertEqual(param_names[0], "%s_%s_%s_%s" %
                         (plugin_ds.id, self.pips[0].id, "null", param.name))
        self.assertEqual(param_names[1], "%s_%s_%s_%s" %
                         (plugin_ds.id, self.pips[1].id, self.pips[0].id, param.name))

    def test_get_pipings_tree(self):
        """
        Test whether custom get_pipings_tree method returns a dictionary containing a
        dictionary representing a tree of pipings and the id of the piping that is the
        root of the tree. The keys of the dictionary tree should be the pipings' ids and
        the values the dictionaries containing the piping and the list of child pipings' ids.
        """
        pipeline = Pipeline.objects.get(name=self.pipeline_name)
        tree_dict = pipeline.get_pipings_tree()
        root_id = tree_dict['root_id']
        tree = tree_dict['tree']
        self.assertEqual(root_id, self.pips[0].id)
        self.assertEqual(tree[root_id], {'piping': self.pips[0], 'child_ids': [self.pips[1].id]})

    def test_check_parameter_defaults(self):
        """
        Test whether custom check_parameter_defaults method raises an exception if
        any of the plugin parameters associated to any of the pipings in the pipeline
        doesn't have a default value.
        """
        pipeline = Pipeline.objects.get(name=self.pipeline_name)
        plugin_ds = Plugin.objects.get(meta__name=self.plugin_ds_name)
        # add a new plugin piping to pipeline but do not set a default value for
        # the plugin parameter
        PluginPiping.objects.get_or_create(plugin=plugin_ds, pipeline=pipeline,
                                           previous=self.pips[1])
        with self.assertRaises(ValueError):
            pipeline.check_parameter_defaults()

    def test_get_accesible_pipelines(self):
        """
        Test whether custom get_accesible_pipelines method returns a filtered queryset
        with all the pipelines that are accessible to a given user (not locked or
        otherwise own by the user).
        """
        user1 = User.objects.get(username=self.username)
        # create new user
        user2 = User.objects.create_user(username='testuser', password='testuser-pass')
        accessible_pipelines_user1 = Pipeline.get_accesible_pipelines(user1)
        self.assertEqual(len(accessible_pipelines_user1),1)
        self.assertEqual(accessible_pipelines_user1[0], Pipeline.objects.all()[0])
        self.assertEqual(len(Pipeline.get_accesible_pipelines(user2)), 0)

    def test_get_accesible_pipelines_chris_user_returns_all(self):
        """
        Test whether custom get_accesible_pipelines method returns all pipelines
        (including locked ones owned by other users) for the 'chris' superuser.
        """
        chris_user, _ = User.objects.get_or_create(username='chris')
        # the fixture pipeline is locked and owned by self.username, not chris
        accessible = Pipeline.get_accesible_pipelines(chris_user)
        self.assertEqual(len(accessible), Pipeline.objects.count())

    def test_get_accesible_pipelines_unauthenticated_user(self):
        """
        Test that get_accesible_pipelines for an unauthenticated user only returns
        non-locked pipelines.
        """
        from django.contrib.auth.models import AnonymousUser
        # the only fixture pipeline is locked, so anonymous sees nothing
        self.assertEqual(len(Pipeline.get_accesible_pipelines(AnonymousUser())), 0)

        owner = User.objects.get(username=self.username)
        Pipeline.objects.create(name='OpenPipeline', owner=owner, locked=False)
        accessible = Pipeline.get_accesible_pipelines(AnonymousUser())
        self.assertEqual(len(accessible), 1)
        self.assertEqual(accessible[0].name, 'OpenPipeline')

    def test_get_default_parameters_returns_mixed_types(self):
        """
        Test that get_default_parameters returns the union of typed default-piping
        parameter rows associated with the pipeline.
        """
        pipeline = Pipeline.objects.get(name=self.pipeline_name)
        plugin_ds = Plugin.objects.get(meta__name=self.plugin_ds_name)
        # add an int-typed parameter so the pipeline ends up with both string and int
        # default-piping rows
        (int_param, _) = PluginParameter.objects.get_or_create(
            plugin=plugin_ds, name='dummyInt', type='integer', optional=True)
        DefaultIntParameter.objects.get_or_create(plugin_param=int_param, value=7)
        # re-saving any piping populates the int default for that piping
        self.pips[0].save()

        defaults = pipeline.get_default_parameters()
        self.assertTrue(any(d.__class__.__name__ == 'DefaultPipingStrParameter'
                            for d in defaults))
        self.assertTrue(any(d.__class__.__name__ == 'DefaultPipingIntParameter'
                            for d in defaults))

    def test__build_title_adjacency_prev_title_inserted_first(self):
        """
        Test the branch in _build_title_adjacency where a node is processed before
        its previous title has been added to the adjacency dict, so the previous
        is created with the current title as its only child.
        """
        # iteration order matters: 'b' must be visited before 'a' to exercise the
        # 'prev_title not in adjacency' branch. dict preserves insertion order.
        nodes = {
            'b': {'plugin_parameter_defaults': [], 'previous': 'a'},
            'a': {'plugin_parameter_defaults': [], 'previous': None},
        }
        is_ts = {'a': False, 'b': False}
        root, adjacency = Pipeline._build_title_adjacency(nodes, is_ts, {})
        self.assertEqual(root, 'a')
        self.assertEqual(adjacency['a'], ['b'])
        self.assertEqual(adjacency['b'], [])

    def test__build_title_adjacency_creates_prev_lazily(self):
        """
        Test the branch where a node is processed whose previous title has not
        yet been added to the adjacency dict. The previous entry is created on
        the fly with the current title as its only child.
        """
        # iteration order: 'c' first; its previous is 'b', which is not yet in
        # adjacency (only the root 'a' is pre-seeded). When 'b' itself is then
        # iterated it is already in adjacency, so it isn't re-linked into 'a'.
        nodes = {
            'c': {'plugin_parameter_defaults': [], 'previous': 'b'},
            'b': {'plugin_parameter_defaults': [], 'previous': 'a'},
            'a': {'plugin_parameter_defaults': [], 'previous': None},
        }
        is_ts = {'a': False, 'b': False, 'c': False}
        root, adjacency = Pipeline._build_title_adjacency(nodes, is_ts, {})
        self.assertEqual(root, 'a')
        # 'b' was created lazily with 'c' as its only child
        self.assertEqual(adjacency['b'], ['c'])
        self.assertIn('c', adjacency)

    def test_get_plugin_tree(self):
        """
        Test whether get_plugin_tree returns the BFS-ordered list of canonical
        node dicts for the pipeline (one per piping, with plugin_id, title,
        previous title, and plugin_parameter_defaults).
        """
        pipeline = Pipeline.objects.get(name=self.pipeline_name)
        plugin_tree = pipeline.get_plugin_tree()
        self.assertEqual(len(plugin_tree), 2)
        self.assertEqual(plugin_tree[0]['title'], 'pip1')
        self.assertIsNone(plugin_tree[0]['previous'])
        self.assertEqual(plugin_tree[1]['title'], 'pip2')
        self.assertEqual(plugin_tree[1]['previous'], 'pip1')

    def test__build_tree_nodes_from_defaults(self):
        """
        Test that _build_tree_nodes_from_defaults aggregates default-parameter
        rows into the per-piping node dicts and the auxiliary id/title/ts maps.
        """
        pipeline = Pipeline.objects.get(name=self.pipeline_name)
        defaults = pipeline.get_default_parameters()
        nodes, id_to_title, is_ts = Pipeline._build_tree_nodes_from_defaults(defaults)
        self.assertEqual(set(nodes.keys()), {'pip1', 'pip2'})
        self.assertIsNone(nodes['pip1']['previous'])
        self.assertEqual(nodes['pip2']['previous'], 'pip1')
        self.assertEqual(id_to_title[self.pips[0].id], 'pip1')
        self.assertEqual(id_to_title[self.pips[1].id], 'pip2')
        # neither piping is a 'ts' plugin in the fixture
        self.assertFalse(is_ts['pip1'])
        self.assertFalse(is_ts['pip2'])
        # one default parameter ('prefix') per piping
        self.assertEqual(len(nodes['pip1']['plugin_parameter_defaults']), 1)
        self.assertEqual(nodes['pip1']['plugin_parameter_defaults'][0]['name'],
                         'prefix')

    def test__resolve_ts_parent_titles_rewrites_ids(self):
        """
        Test that _resolve_ts_parent_titles replaces the comma-separated piping
        ids in the 'plugininstances' default with the corresponding piping titles.
        """
        node = {'plugin_parameter_defaults': [
            {'name': 'plugininstances', 'default': '7,9'},
            {'name': 'other', 'default': 'x'},
        ]}
        Pipeline._resolve_ts_parent_titles(node, {7: 'a', 9: 'b'})
        self.assertEqual(node['plugin_parameter_defaults'][0]['default'], 'a,b')
        # other entries are untouched
        self.assertEqual(node['plugin_parameter_defaults'][1]['default'], 'x')

    def test__resolve_ts_parent_titles_empty_default(self):
        """
        Test that _resolve_ts_parent_titles is a no-op when 'plugininstances'
        default is the empty string.
        """
        node = {'plugin_parameter_defaults': [
            {'name': 'plugininstances', 'default': ''},
        ]}
        Pipeline._resolve_ts_parent_titles(node, {})
        self.assertEqual(node['plugin_parameter_defaults'][0]['default'], '')

    def test__build_title_adjacency(self):
        """
        Test that _build_title_adjacency identifies the root title and produces
        the expected child-list dict.
        """
        nodes = {
            'a': {'plugin_parameter_defaults': [], 'previous': None},
            'b': {'plugin_parameter_defaults': [], 'previous': 'a'},
            'c': {'plugin_parameter_defaults': [], 'previous': 'a'},
            'd': {'plugin_parameter_defaults': [], 'previous': 'b'},
        }
        is_ts = {'a': False, 'b': False, 'c': False, 'd': False}
        root, adjacency = Pipeline._build_title_adjacency(nodes, is_ts, {})
        self.assertEqual(root, 'a')
        self.assertEqual(set(adjacency['a']), {'b', 'c'})
        self.assertEqual(adjacency['b'], ['d'])
        self.assertEqual(adjacency['c'], [])
        self.assertEqual(adjacency['d'], [])

    def test__bfs_order_plugin_tree(self):
        """
        Test that _bfs_order_plugin_tree visits root first, then its children in
        the order given by the adjacency, then their children, etc.
        """
        nodes = {'a': {'title': 'a'}, 'b': {'title': 'b'},
                 'c': {'title': 'c'}, 'd': {'title': 'd'}}
        adjacency = {'a': ['b', 'c'], 'b': ['d'], 'c': [], 'd': []}
        ordered = Pipeline._bfs_order_plugin_tree('a', adjacency, nodes)
        self.assertEqual([n['title'] for n in ordered], ['a', 'b', 'c', 'd'])


class PluginPipingModelTests(ModelTests):

    def test_save(self):
        """
        Test whether overriden save method saves the default plugin parameters' values
        associated with this piping.
        """
        plugin_ds = Plugin.objects.get(meta__name=self.plugin_ds_name)
        # add a parameter with a default
        (plg_param_ds, tf)= PluginParameter.objects.get_or_create(
            plugin=plugin_ds,
            name='dummyInt',
            type='integer',
            optional=True
        )
        DefaultIntParameter.objects.get_or_create(plugin_param=plg_param_ds, value=1)  # set plugin parameter default
        pipeline = Pipeline.objects.get(name=self.pipeline_name)
        pip = PluginPiping()
        pip.plugin = plugin_ds
        pip.pipeline = pipeline
        pip.save()
        defaults = pip.integer_param.all()
        self.assertEqual(len(defaults), 1)
        self.assertEqual(defaults[0].value, 1)

    def test_check_parameter_defaults(self):
        """
        Test whether custom check_parameter_defaults method raises an exception if
        any of the plugin parameters associated to the piping doesn't have a default value.
        """
        pipeline = Pipeline.objects.get(name=self.pipeline_name)
        plugin_ds = Plugin.objects.get(meta__name=self.plugin_ds_name)
        # add a new plugin piping to pipeline but do not set a default value for
        # the plugin parameter
        (pip, tf) = PluginPiping.objects.get_or_create(plugin=plugin_ds, pipeline=pipeline,
                                           previous=self.pips[1])
        with self.assertRaises(ValueError):
            pip.check_parameter_defaults()

    def test_save_sets_compute_defaults_from_plugin(self):
        """
        Test that PluginPiping.save populates cpu_limit, memory_limit,
        number_of_workers and gpu_limit from the plugin's min_* values when those
        fields are not explicitly set on the piping.
        """
        plugin_ds = Plugin.objects.get(meta__name=self.plugin_ds_name)
        pipeline = Pipeline.objects.get(name=self.pipeline_name)
        pip = PluginPiping(plugin=plugin_ds, pipeline=pipeline,
                           previous=self.pips[1], title='compute-defaults')
        pip.save()
        self.assertEqual(int(pip.cpu_limit), int(plugin_ds.min_cpu_limit))
        self.assertEqual(int(pip.memory_limit), int(plugin_ds.min_memory_limit))
        self.assertEqual(pip.number_of_workers, plugin_ds.min_number_of_workers)
        self.assertEqual(pip.gpu_limit, plugin_ds.min_gpu_limit)

    def test_save_keeps_explicitly_set_compute_overrides(self):
        """
        Test that PluginPiping.save does not overwrite explicitly-set per-piping
        cpu_limit / memory_limit / number_of_workers / gpu_limit values.
        """
        plugin_ds = Plugin.objects.get(meta__name=self.plugin_ds_name)
        pipeline = Pipeline.objects.get(name=self.pipeline_name)
        cpu = plugin_ds.min_cpu_limit + 10
        mem = plugin_ds.min_memory_limit + 32
        n_workers = plugin_ds.min_number_of_workers
        gpu = plugin_ds.min_gpu_limit
        pip = PluginPiping(plugin=plugin_ds, pipeline=pipeline,
                           previous=self.pips[1], title='compute-overrides',
                           cpu_limit=cpu, memory_limit=mem,
                           number_of_workers=n_workers, gpu_limit=gpu)
        pip.save()
        self.assertEqual(int(pip.cpu_limit), int(cpu))
        self.assertEqual(int(pip.memory_limit), int(mem))
        self.assertEqual(pip.number_of_workers, n_workers)
        self.assertEqual(pip.gpu_limit, gpu)

    def test_save_updates_existing_default_when_param_supplied(self):
        """
        Test the update branch in PluginPiping.save: when an existing default-piping
        parameter row is present and a parameter_defaults entry is supplied, the
        existing row's value is updated rather than a new row being created.
        """
        plugin_ds = Plugin.objects.get(meta__name=self.plugin_ds_name)
        pipeline = Pipeline.objects.get(name=self.pipeline_name)
        pip = self.pips[0]
        existing = pip.string_param.get(plugin_param__name='prefix')
        existing.value = 'original'
        existing.save()
        # supply a new default for the same parameter via save(parameter_defaults=...)
        pip.save(parameter_defaults=[{'name': 'prefix', 'default': 'updated'}])
        existing.refresh_from_db()
        self.assertEqual(existing.value, 'updated')


class PipelineSourceFileSignalTests(ModelTests):
    """
    Tests for the post_delete signals on PipelineSourceFile and
    PipelineSourceFileMeta.
    """

    def _make_source_file(self):
        """
        Build a PipelineSourceFile + PipelineSourceFileMeta for the fixture
        pipeline, with its fname pointing inside the PIPELINES space and a
        ChrisFolder parent. No bytes are uploaded to storage; tests that need
        to assert delete behaviour mock the storage manager.
        """
        owner = User.objects.get(username=self.username)
        pipeline = Pipeline.objects.get(name=self.pipeline_name)
        folder_path = f'PIPELINES/{self.username}'
        (parent_folder, _) = ChrisFolder.objects.get_or_create(path=folder_path,
                                                               owner=owner)
        source_file = PipelineSourceFile(parent_folder=parent_folder, owner=owner)
        source_file.fname.name = f'{folder_path}/test_signal.yaml'
        source_file.save()
        meta = PipelineSourceFileMeta.objects.create(
            type='yaml', pipeline=pipeline, source_file=source_file, uploader=owner)
        return source_file, meta

    def test_auto_delete_file_from_storage_when_object_exists(self):
        """
        Test the post_delete signal on PipelineSourceFile: delete_obj is called on its
        object (one that is not there counts as deleted).
        """
        source_file, _ = self._make_source_file()
        storage_path = source_file.fname.name
        storage_manager_mock = mock.Mock()
        with mock.patch('core.models.connect_storage') as connect_storage_mock:
            connect_storage_mock.return_value = storage_manager_mock
            source_file.delete()
        storage_manager_mock.delete_obj.assert_called_once_with(storage_path)

    def test_auto_delete_file_from_storage_swallows_storage_errors(self):
        """
        Test that storage errors during the post_delete signal on
        PipelineSourceFile are caught and logged rather than re-raised. Without
        this, deleting a PipelineSourceFile would fail whenever the storage
        backend is unreachable.
        """
        source_file, _ = self._make_source_file()
        storage_manager_mock = mock.Mock()
        storage_manager_mock.delete_obj = mock.Mock(side_effect=RuntimeError('boom'))
        with mock.patch('core.models.connect_storage') as connect_storage_mock:
            connect_storage_mock.return_value = storage_manager_mock
            # should not raise; the error must be logged instead
            with self.assertLogs('core.models', level='ERROR') as cm:
                source_file.delete()
        self.assertTrue(any('boom' in msg for msg in cm.output))

    def test_auto_delete_source_file_with_meta_cascade(self):
        """
        Test that deleting a PipelineSourceFileMeta triggers a delete of the
        associated PipelineSourceFile through the post_delete signal.
        """
        source_file, meta = self._make_source_file()
        source_file_pk = source_file.pk
        storage_manager_mock = mock.Mock()
        with mock.patch('core.models.connect_storage') as connect_storage_mock:
            connect_storage_mock.return_value = storage_manager_mock
            meta.delete()
        self.assertFalse(
            PipelineSourceFile.objects.filter(pk=source_file_pk).exists())


class PipelineSourceFileQuerysetTests(ModelTests):
    """
    Tests for PipelineSourceFile.get_base_queryset.
    """

    def test_get_base_queryset_filters_to_pipelines_space(self):
        """
        Test that get_base_queryset only returns rows whose fname starts with
        'PIPELINES/'.
        """
        owner = User.objects.get(username=self.username)
        (pipelines_folder, _) = ChrisFolder.objects.get_or_create(
            path=f'PIPELINES/{self.username}', owner=owner)
        in_space = PipelineSourceFile(parent_folder=pipelines_folder, owner=owner)
        in_space.fname.name = f'PIPELINES/{self.username}/in.yaml'
        in_space.save()

        (other_folder, _) = ChrisFolder.objects.get_or_create(
            path=f'home/{self.username}', owner=owner)
        out_of_space = PipelineSourceFile(parent_folder=other_folder, owner=owner)
        out_of_space.fname.name = f'home/{self.username}/out.yaml'
        out_of_space.save()

        names = list(PipelineSourceFile.get_base_queryset()
                     .values_list('fname', flat=True))
        self.assertIn(f'PIPELINES/{self.username}/in.yaml', names)
        self.assertNotIn(f'home/{self.username}/out.yaml', names)


class PipelineSourceFileMetaModelTests(ModelTests):
    """
    Tests for PipelineSourceFileMeta.
    """

    def test_str_returns_string_id(self):
        """
        Regression test: __str__ must return a string (not an int) so Django can
        render the instance in admin / shell contexts.
        """
        owner = User.objects.get(username=self.username)
        pipeline = Pipeline.objects.get(name=self.pipeline_name)
        (parent_folder, _) = ChrisFolder.objects.get_or_create(
            path=f'PIPELINES/{self.username}', owner=owner)
        source_file = PipelineSourceFile(parent_folder=parent_folder, owner=owner)
        source_file.fname.name = f'PIPELINES/{self.username}/meta_str.yaml'
        source_file.save()
        meta = PipelineSourceFileMeta.objects.create(
            type='yaml', pipeline=pipeline, source_file=source_file,
            uploader=owner)
        self.assertEqual(str(meta), str(meta.id))
