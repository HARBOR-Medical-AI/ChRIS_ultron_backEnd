
import logging
from unittest import mock

from django.test import TestCase
from django.conf import settings
from django.contrib.auth.models import User

from plugininstances.models import PluginInstance
from plugins.models import PluginMeta, Plugin
from plugins.models import PluginParameter, DefaultStrParameter
from plugins.models import ComputeResource
from plugins.services import manager


COMPUTE_RESOURCE_URL = settings.COMPUTE_RESOURCE_URL


class PluginManagerTests(TestCase):
    
    def setUp(self):
        # avoid cluttered console output (for instance logging all the http requests)
        logging.disable(logging.WARNING)
        self.plugin_fs_name = "simplecopyapp"

        plugin_parameters = [{'name': 'dir', 'type': 'string', 'action': 'store',
                              'optional': True, 'flag': '--dir', 'short_flag': '-d',
                              'default': '/', 'help': 'test plugin', 'ui_exposed': True}]

        self.plg_data = {'description': 'A simple chris fs app demo',
                         'version': '0.1',
                         'dock_image': 'fnndsc/pl-simplecopyapp',
                         'execshell': 'python3',
                         'selfpath': '/usr/local/bin',
                         'selfexec': 'simplefsapp'}

        self.plg_meta_data = {'name':  self.plugin_fs_name,
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


        self.pl_manager = manager.PluginManager()

        (self.compute_resource, tf) = ComputeResource.objects.get_or_create(
            name="host", compute_url=COMPUTE_RESOURCE_URL)

        # create a plugin
        data = self.plg_meta_data.copy()
        (pl_meta, tf) = PluginMeta.objects.get_or_create(**data)
        data = self.plg_data.copy()
        (plugin, tf) = Plugin.objects.get_or_create(meta=pl_meta, **data)
        plugin.compute_resources.set([self.compute_resource])
        plugin.save()

        # add plugin's parameters
        parameters = plugin_parameters
        (plg_param, tf) = PluginParameter.objects.get_or_create(
            plugin=plugin,
            name=parameters[0]['name'],
            type=parameters[0]['type'],
            flag=parameters[0]['flag'],
            short_flag=parameters[0]['short_flag'],
            optional=parameters[0]['optional']
        )
        default = parameters[0]['default']
        DefaultStrParameter.objects.get_or_create(plugin_param=plg_param, value=default)

    def tearDown(self):
        # re-enable logging
        logging.disable(logging.NOTSET)

    def test_mananger_can_get_plugin(self):
        """
        Test whether the manager can return a plugin object.
        """
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name, version="0.1")
        self.assertEqual(plugin, self.pl_manager.get_plugin(self.plugin_fs_name, "0.1"))

    def test_mananger_can_register_plugin(self):
        """
        Test whether the manager can add a new plugin to the system given its name
        and version.
        """
        self.plugin_repr['name'] = 'testapp'
        # mock manager's get_plugin_representation_from_store static method
        self.pl_manager.get_plugin_representation_from_store = mock.Mock(
            return_value=self.plugin_repr)

        plugin = self.pl_manager.register_plugin('testapp', '0.1', 'host')
        self.assertEqual(Plugin.objects.count(), 2)
        self.assertEqual(plugin.meta.name, 'testapp')
        self.assertTrue(PluginParameter.objects.count() > 1)
        self.pl_manager.get_plugin_representation_from_store.assert_called_with(
            'testapp', '0.1', 30)

        self.pl_manager.register_plugin('testapp', '', 'host')
        self.pl_manager.get_plugin_representation_from_store.assert_called_with(
            'testapp', None, 30)

    def test_mananger_register_plugin_raises_name_error_if_compute_resource_does_not_exist(self):
        """
        Test whether the manager's register_plugin method raises NameError exception
        when the compute respource argument doesn't exist in the DB.
        """
        with self.assertRaises(NameError):
            self.pl_manager.register_plugin('testapp', '0.1', 'dummy')

    def test_mananger_can_register_plugin_by_url(self):
        """
        Test whether the manager can add a new plugin to the system given its url.
        """
        self.plugin_repr['name'] = 'testapp'
        # mock manager's get_plugin_representation_from_store static method
        self.pl_manager.get_plugin_representation_from_store_by_url = mock.Mock(
            return_value=self.plugin_repr)
        plugin = self.pl_manager.register_plugin_by_url(
            'http://127.0.0.1:8010/api/v1/1/', 'host')
        self.assertEqual(Plugin.objects.count(), 2)
        self.assertEqual(plugin.meta.name, 'testapp')
        self.assertTrue(PluginParameter.objects.count() > 1)
        self.pl_manager.get_plugin_representation_from_store_by_url.assert_called_with(
            'http://127.0.0.1:8010/api/v1/1/', 30)

    def test_mananger_register_plugin_by_url_raises_name_error_if_compute_resource_does_not_exist(self):
        """
        Test whether the manager's register_plugin_by_url method raises NameError
        exception when the compute respource argument doesn't exist in the DB.
        """
        with self.assertRaises(NameError):
            self.pl_manager.register_plugin('testapp', '0.1', 'dummy')

    def test_mananger_can_remove_plugin(self):
        """
        Test whether the manager can remove an existing plugin from the system.
        """
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name, version="0.1")
        self.pl_manager.remove_plugin(plugin.id)
        self.assertEqual(Plugin.objects.count(), 0)
        self.assertEqual(PluginMeta.objects.count(), 0)
        self.assertEqual(PluginParameter.objects.count(), 0)

    def test_mananger_cancels_a_removed_plugin_s_instances_before_deleting_them(self):
        """
        Test whether removing a plugin cancels its instances (arming the remote cleanup
        of one with a job) before the cascade deletes them.
        """
        plugin = Plugin.objects.get(meta__name=self.plugin_fs_name, version="0.1")
        user = User.objects.create_user(username='foo',
                                        password='bar')  # nosec B106 - a test user
        for status in ('created', 'started', 'finishedSuccessfully'):
            inst = PluginInstance.objects.create(plugin=plugin, owner=user,
                                                 compute_resource=self.compute_resource)
            PluginInstance.objects.filter(pk=inst.pk).update(status=status)
        original = PluginInstance.cancel
        cancels = []

        def cancel(inst, *args, **kwargs):
            written = original(inst, *args, **kwargs)
            cancels.append((inst.status, inst.remote_cleanup_status, written.won))
            return written

        with mock.patch.object(PluginInstance, 'cancel', cancel):
            self.pl_manager.remove_plugin(plugin.id)

        self.assertCountEqual(cancels, [('cancelled', 'notStarted', True),
                                        ('cancelled', 'deletingData', True),
                                        ('finishedSuccessfully', 'notStarted', False)])
        self.assertEqual(PluginInstance.objects.count(), 0)
