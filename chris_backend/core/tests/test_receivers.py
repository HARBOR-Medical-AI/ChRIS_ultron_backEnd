"""
Tests for the signal receivers of CUBE: that they outlive Django's DEBUG checks, and what
the receivers of the deletions of files, link files and folders delete from storage.
"""

import gc
import sys
import weakref
from unittest import mock

import django.core.signals
import django.db.models.signals
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.contrib.auth.models import User
from django.db import DatabaseError, transaction
from django.db.models.signals import post_delete
from django.dispatch import Signal
from django.test import SimpleTestCase, TestCase
from django.utils import inspect

from core.models import (ChrisFile, ChrisFolder, ChrisLinkFile,
                         auto_delete_file_from_storage, delete_related)
from core.storage.helpers import mock_storage
from pacsfiles.models import PACS, PACSFile, PACSSeries
from pipelines.models import PipelineSourceFile
from plugininstances.models import PluginInstance
from plugins.models import ComputeResource, Plugin, PluginMeta
from userfiles.models import UserFile


SIGNALS = {f'{module.__name__}.{name}': signal
           for module in (django.db.models.signals, django.core.signals)
           for name, signal in vars(module).items() if isinstance(signal, Signal)}


def cube_receivers():
    """
    The receivers of CUBE's modules that are alive, as names: no reference to one is
    kept, which would keep it alive.
    """
    found = set()
    for name, signal in SIGNALS.items():
        for _, receiver, sender, _ in signal.receivers:
            if isinstance(receiver, weakref.ReferenceType):
                receiver = receiver()
            if isinstance(sender, weakref.ReferenceType):
                sender = sender()
            module = sys.modules.get(getattr(receiver, '__module__', None) or '')
            if module is None or not str(getattr(module, '__file__', '')).startswith(
                    str(settings.BASE_DIR)):
                continue
            found.add(f'{name} {module.__name__}.{receiver.__qualname__} '
                      f'sender={getattr(sender, "__name__", sender)}')
    return found


class ReceiversOutliveDebugTests(SimpleTestCase):

    def test_every_receiver_of_cube_outlives_the_checks_of_debug(self):
        """
        A signal holds its receivers by weak references. With DEBUG on when the models
        load (dev, tests), Django's check of each receiver also keeps it in a cache, which
        hides a receiver that nothing else holds (a function whose name its module then
        gives to another function, as the two ``auto_delete_file_from_storage`` did):
        without DEBUG (production) it is gone. With that cache and the signals' own
        emptied, each receiver of CUBE's must still be there.
        """
        before = cube_receivers()

        inspect._get_func_parameters.cache_clear()
        for signal in SIGNALS.values():
            signal.sender_receivers_cache.clear()
        gc.collect()

        self.assertEqual(cube_receivers(), before)

    def test_each_model_with_objects_in_storage_deletes_them(self):
        for model in (ChrisFile, ChrisLinkFile, UserFile, PACSFile, PipelineSourceFile):
            with self.subTest(model=model.__name__):
                self.assertIn(auto_delete_file_from_storage,
                              post_delete._live_receivers(model)[0])
        self.assertTrue(post_delete.has_listeners(ChrisFolder))


class StorageReceiversTests(TestCase):
    """
    What a deletion deletes from storage: a file's or link file's own object, or a
    folder's tree at once, the objects under it left to that.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='receivers')
        self.base = 'home/receivers/uploads/tree'

        mocked = mock_storage(settings)
        self.storage = mocked.__enter__()
        self.addCleanup(mocked.__exit__, None, None, None)
        self.spy = mock.Mock(wraps=self.storage)
        patcher = mock.patch('core.models.connect_storage', return_value=self.spy)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.folder = ChrisFolder.objects.create(path=self.base, owner=self.user)
        self.sub = ChrisFolder.objects.create(path=f'{self.base}/sub', owner=self.user)
        self.file = self.chris_file(self.folder, 'a.txt')
        self.sub_file = self.chris_file(self.sub, 'b.txt')
        self.link = ChrisLinkFile(path='home/receivers/uploads', owner=self.user,
                                  parent_folder=self.folder)
        self.link.save(name='l')
        sibling = ChrisFolder.objects.create(path=f'{self.base}_2', owner=self.user)
        self.sibling_file = self.chris_file(sibling, 'c.txt')
        self.spy.reset_mock()

    def chris_file(self, folder, name):
        path = f'{folder.path}/{name}'
        self.storage.upload_obj(path, b'x')
        f = ChrisFile(parent_folder=folder, owner=self.user)
        f.fname.name = path
        f.save()
        return f

    def objects(self, path):
        return sorted(self.storage.ls(path))

    def plugin_instance(self):
        """
        An instance of an 'fs' plugin, with its feed and its output folder.
        """
        (compute_resource, _) = ComputeResource.objects.get_or_create(
            name='host', compute_url=settings.COMPUTE_RESOURCE_URL)
        (meta, _) = PluginMeta.objects.get_or_create(name='pl-receivers', type='fs')
        (plugin, _) = Plugin.objects.get_or_create(meta=meta, version='0.1')
        plugin.compute_resources.set([compute_resource])
        return PluginInstance.objects.create(plugin=plugin, owner=self.user,
                                             compute_resource=compute_resource)

    def test_a_file_deleted_on_its_own_deletes_its_object(self):
        path = self.file.fname.name

        self.file.delete()

        self.spy.delete_obj.assert_called_once_with(path)
        self.spy.delete_path.assert_not_called()
        self.assertFalse(self.storage.obj_exists(path))

    def test_a_folder_deleted_deletes_its_tree_at_once(self):
        self.folder.delete()

        self.spy.delete_path.assert_called_once_with(self.base + '/')
        self.spy.delete_obj.assert_not_called()
        self.assertEqual(self.objects(self.base), [])
        self.assertEqual(self.objects(f'{self.base}_2'), [self.sibling_file.fname.name])

    def test_a_deletion_from_a_subfolder_keeps_to_it(self):
        self.sub.delete()

        self.spy.delete_path.assert_called_once_with(self.sub.path + '/')
        self.spy.delete_obj.assert_not_called()
        self.assertEqual(self.objects(self.base),
                         [self.file.fname.name, self.link.fname.name])

    def test_files_deleted_as_a_queryset_delete_their_objects(self):
        ChrisFile.objects.filter(parent_folder=self.folder).delete()

        self.spy.delete_obj.assert_called_once_with(self.file.fname.name)
        self.assertEqual(self.objects(self.base),
                         [self.link.fname.name, self.sub_file.fname.name])

    def test_a_link_file_or_a_proxy_deleted_on_its_own_deletes_its_object(self):
        self.link.delete()
        UserFile.objects.get(pk=self.sub_file.pk).delete()

        self.assertEqual(self.spy.delete_obj.call_args_list,
                         [mock.call(self.link.fname.name),
                          mock.call(self.sub_file.fname.name)])
        self.assertEqual(self.objects(self.base), [self.file.fname.name])

    def test_the_contents_of_a_folder_are_deleted_at_once(self):
        self.folder.delete_contents()

        self.assertTrue(ChrisFolder.objects.filter(pk=self.folder.pk).exists())
        self.assertFalse(ChrisFolder.objects.filter(pk=self.sub.pk).exists())
        self.assertFalse(ChrisFile.objects.filter(fname__startswith=self.base + '/')
                         .exists())
        self.assertFalse(ChrisLinkFile.objects.filter(parent_folder=self.folder).exists())
        self.spy.delete_path.assert_called_once_with(self.base + '/')
        self.spy.delete_obj.assert_not_called()
        self.assertEqual(self.objects(self.base), [])
        self.assertEqual(self.objects(f'{self.base}_2'), [self.sibling_file.fname.name])

    def test_contents_whose_storage_fails_raise_and_a_rerun_deletes_them(self):
        self.spy.delete_path.side_effect = [RuntimeError('503'), mock.DEFAULT]

        with self.assertRaisesRegex(RuntimeError, '503'):
            self.folder.delete_contents()
        self.assertFalse(ChrisFile.objects.filter(pk=self.file.pk).exists())
        self.assertEqual(len(self.objects(self.base)), 3)  # still in storage

        self.folder.delete_contents()  # the cleanup run again
        self.assertEqual(self.objects(self.base), [])

    def test_a_storage_error_of_a_folder_is_logged_and_its_rows_stay_deleted(self):
        self.spy.delete_path.side_effect = RuntimeError('503')

        with self.assertLogs('core.models', 'ERROR') as logs:
            self.folder.delete()

        self.assertIn(self.base, logs.output[0])
        self.assertFalse(ChrisFolder.objects.filter(pk=self.folder.pk).exists())

    def test_a_user_deleted_deletes_the_objects_of_its_files(self):
        self.user.delete()  # a deletion that starts at no folder

        self.assertFalse(ChrisFile.objects.filter(fname__startswith=self.base).exists())
        self.assertEqual(self.objects(self.base), [])
        self.assertEqual(self.objects(f'{self.base}_2'), [])

    def test_a_feed_deleted_deletes_the_trees_of_its_instances_and_its_own(self):
        inst = self.plugin_instance()
        feed, plugin_folder = inst.feed, inst.output_folder.parent
        output = self.chris_file(inst.output_folder, 'out.txt')
        sibling = ChrisFolder.objects.create(path=feed.folder.path + '0', owner=self.user)
        kept = self.chris_file(sibling, 'x.txt')
        self.spy.reset_mock()

        feed.delete()

        self.spy.delete_obj.assert_not_called()
        self.assertEqual(self.spy.delete_path.call_args_list,
                         [mock.call(plugin_folder.path + '/'),
                          mock.call(feed.folder.path + '/')])
        self.assertFalse(self.storage.obj_exists(output.fname.name))
        self.assertTrue(self.storage.obj_exists(kept.fname.name))

    def test_a_feed_s_folder_deleted_is_not_deleted_again_by_the_feed_s_receiver(self):
        inst = self.plugin_instance()
        feed_folder = inst.feed.folder
        self.spy.reset_mock()

        feed_folder.delete()  # the feed goes with it

        self.assertFalse(PluginInstance.objects.filter(pk=inst.pk).exists())
        paths = [c.args[0] for c in self.spy.delete_path.call_args_list]
        self.assertEqual(paths.count(feed_folder.path + '/'), 1)

    def test_a_pacs_series_deleted_deletes_its_folder_s_tree(self):
        chris = User.objects.get(username='chris')
        pacs_folder = ChrisFolder.objects.create(path='SERVICES/PACS/RCVPACS', owner=chris)
        pacs = PACS.objects.create(folder=pacs_folder, identifier='RCVPACS')
        folder = ChrisFolder.objects.create(path=f'{pacs_folder.path}/P1/series',
                                            owner=chris)
        series = PACSSeries.objects.create(
            PatientID='P1', StudyDate='2026-01-01', StudyInstanceUID='1.rcv.study',
            SeriesInstanceUID='1.rcv.series', pacs=pacs, folder=folder)
        dicom = f'{folder.path}/001.dcm'
        self.storage.upload_obj(dicom, b'dicom')
        pacs_file = PACSFile(owner=chris, parent_folder=folder)
        pacs_file.fname.name = dicom
        pacs_file.save()
        self.spy.reset_mock()

        series.delete()

        self.spy.delete_path.assert_called_once_with(folder.path + '/')
        self.spy.delete_obj.assert_not_called()
        self.assertFalse(self.storage.obj_exists(dicom))
        self.assertTrue(ChrisFolder.objects.filter(pk=pacs_folder.pk).exists())

    def test_a_storage_error_is_logged_and_the_row_stays_deleted(self):
        self.spy.delete_obj.side_effect = RuntimeError('503')

        with self.assertLogs('core.models', 'ERROR') as logs:
            self.file.delete()

        self.assertIn('503', logs.output[0])
        self.assertFalse(ChrisFile.objects.filter(pk=self.file.pk).exists())

    def test_a_soft_time_limit_ends_the_deletion(self):
        self.spy.delete_obj.side_effect = SoftTimeLimitExceeded()

        with self.assertRaises(SoftTimeLimitExceeded), transaction.atomic():
            self.file.delete()

        self.assertTrue(ChrisFile.objects.filter(pk=self.file.pk).exists())  # rolled back


class DeleteRelatedTests(TestCase):
    """
    ``delete_related``, which the receivers of deletions use to delete what goes with
    the deleted object (a feed's folder, a plugin instance's output folder...).
    """

    def setUp(self):
        user = User.objects.create_user(username='related')
        self.parent = ChrisFolder.objects.create(path='home/related/uploads/p', owner=user)
        self.child = ChrisFolder.objects.create(path='home/related/uploads/p/c', owner=user)
        patcher = mock.patch('core.models.connect_storage')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_related_object_is_deleted(self):
        delete_related(self.child, 'parent', 'parent')  # p's parent: uploads

        self.assertFalse(ChrisFolder.objects.filter(path='home/related/uploads').exists())

    def test_a_relation_never_set_or_an_object_deleted_already_is_nothing_to_delete(self):
        root = ChrisFolder.objects.get(path='')
        child = ChrisFolder.objects.get(pk=self.child.pk)
        ChrisFolder.objects.filter(pk=self.parent.pk).delete()

        delete_related(root, 'parent')
        delete_related(child, 'parent')

        self.assertTrue(ChrisFolder.objects.filter(path='').exists())

    def test_the_object_a_deletion_started_at_is_left_to_it(self):
        with mock.patch.object(ChrisFolder, 'delete') as delete:
            delete_related(self.child, 'parent', origin=self.parent)

        delete.assert_not_called()

    def test_any_other_error_is_raised(self):
        with mock.patch.object(ChrisFolder, 'delete', side_effect=DatabaseError('lock')), \
                self.assertRaisesRegex(DatabaseError, 'lock'):
            delete_related(self.child, 'parent')
