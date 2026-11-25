"""
Tests for the registration of a plugin instance's output files at scale and when it runs
again (HARBOR-planning #155), on a storage of their own (``mock_storage``): one walk of the
output, a batch of rows at a time, resumed after a worker died, stopped when overtaken.
"""

import io
import math
import os
import tracemalloc
import types
import zipfile
from pathlib import Path
from unittest import mock

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.db import connection
from django.db.models.fields.files import FieldFile
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from core import registration
from core.models import ChrisFile, ChrisFolder, ChrisLinkFile
from core.registration import BatchSizer
from core.storage import StorageManager
from core.storage.helpers import mock_storage
from plugininstances.models import PluginInstance, PluginInstanceLock
from plugininstances.services import abstractjobs, pluginjobs
from plugininstances.tests.test_abstractjobs import Clock
from plugininstances.tests.test_transitions import NEWER_RUN, PluginInstanceFixture
from userfiles.models import UserFile


class Killed(BaseException):
    """
    What a worker that dies does to the registration it runs: nothing catches it.
    """


def fixed_batches(size):
    """
    Keep every batch at ``size`` rows: each sizer starts there and learns nothing.
    """
    def init(sizer, *args, **kwargs):
        sizer.size = size

    return mock.patch.multiple(BatchSizer, __init__=init,
                               took=lambda sizer, rows, seconds: None)


def file_inserts(fail_at=None, error=None, after=None):
    """
    Have the ``fail_at``-th insert of files raise ``error`` instead, and call ``after()``
    after each one that ran (``core.registration.insert_rows``).
    """
    real, count = registration.insert_rows, []

    def insert_rows(model, rows, sizer):
        if model is not UserFile:
            return real(model, rows, sizer)
        count.append(len(rows))
        if len(count) == fail_at:
            raise error
        real(model, rows, sizer)
        if after is not None:
            after()

    return mock.patch.object(registration, 'insert_rows', insert_rows)


class OutputRegistrationFixture(PluginInstanceFixture):
    """
    A plugin instance registering its output files on a storage of its own, the files
    written there with ``write``, and ``job``/``register`` to run its registration, its
    remote mocked: the job ended successfully and its JSON lists the files written.
    """

    def setUp(self):
        super().setUp()
        storage = mock_storage(settings)
        self.storage = storage.__enter__()
        self.addCleanup(storage.__exit__, None, None, None)
        self.inst = self.instance('registeringFiles')
        self.output = self.inst.get_output_path()
        self.names = []

    def write(self, names, data=b'12345'):
        base = Path(settings.MEDIA_ROOT) / self.output
        for name in names:
            path = base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.names += names

    def job(self, remote_status='finishedSuccessfully'):
        job = pluginjobs.PluginInstanceAppJob(PluginInstance.objects.get(pk=self.inst.pk))
        job._get_status = mock.Mock(return_value={'compute': {'status': remote_status}})
        job._get_job_json_data = mock.Mock(return_value={
            'job_output_path': self.output, 'rel_file_paths': list(self.names)})
        job.enqueue_run = mock.Mock()  # the remote cleanup
        return job

    def register(self):
        job = self.job()
        job.register_output()
        return job

    def rows(self):
        return ChrisFile.objects.filter(fname__startswith=self.output + '/')

    def status(self):
        inst = PluginInstance.objects.get(pk=self.inst.pk)
        return inst.status, inst.size, inst.error_code


class ScaleTests(OutputRegistrationFixture, TestCase):

    FILES = 100_000
    FOLDERS = 400
    BATCH = 1000

    def test_a_large_output_is_registered_a_batch_at_a_time_with_one_stat_per_file(self):
        per_folder = self.FILES // self.FOLDERS
        self.write([f'd{i % 20}/e{i}/f{j}.dat' for i in range(self.FOLDERS)
                    for j in range(per_folder)], b'x')
        job = self.job()
        stats = []

        def counted(real):
            def call(path, *args, **kwargs):
                try:
                    name = str(os.fspath(path))
                except TypeError:  # a file descriptor
                    name = ''
                if self.output in name:
                    stats.append(name)
                return real(path, *args, **kwargs)
            return call

        tracemalloc.start()
        try:
            with fixed_batches(self.BATCH), \
                    mock.patch('os.stat', counted(os.stat)), \
                    mock.patch('os.lstat', counted(os.lstat)), \
                    mock.patch('os.path.getsize', counted(os.path.getsize)), \
                    mock.patch.object(FieldFile, 'size',
                                      new_callable=mock.PropertyMock) as size, \
                    mock.patch.object(job.storage_manager, 'walk',
                                      wraps=job.storage_manager.walk) as walk, \
                    CaptureQueriesContext(connection) as queries:
                job.register_output()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertEqual(self.status(), ('finishedSuccessfully', self.FILES, ''))
        self.assertEqual(self.rows().count(), self.FILES)
        # one walk, and no stat of an output file but the walk's own (DirEntry.stat)
        walk.assert_called_once_with(self.output)
        self.assertEqual((stats, size.call_count), ([], 0))

        # the savepoints of each insert's atomic block are the test transaction's
        sql = [q['sql'] for q in queries if 'SAVEPOINT' not in q['sql']]
        batches = math.ceil(self.FILES / self.BATCH)
        file_inserts = [q for q in sql if q.startswith('INSERT INTO "core_chrisfile"')]
        folder_inserts = [q for q in sql if q.startswith('INSERT INTO "core_chrisfolder"')]
        claim_checks = [q for q in sql if q.startswith('SELECT 1 AS')
                        and '"plugininstances_plugininstancelock"' in q]
        self.assertEqual(len(file_inserts), batches)
        # a batch's folders: two levels at most, a statement each
        self.assertLessEqual(len(folder_inserts), 2 * batches)
        # the claim checked before each batch
        self.assertGreaterEqual(len(claim_checks), batches)
        self.assertLess(len(sql), 4 * batches + 50)
        # what it holds grows with the paths it expects, not with a row per file
        self.assertLess(peak, 400 * self.FILES)


class ResumeTests(OutputRegistrationFixture, TestCase):

    def setUp(self):
        super().setUp()
        self.write([f'sub{i % 7}/f{i}.txt' for i in range(2500)])

    def test_a_registration_a_dead_worker_left_is_resumed_once_per_file(self):
        with fixed_batches(500), file_inserts(fail_at=3, error=Killed()), \
                self.assertRaises(Killed):
            self.register()

        self.assertEqual(self.rows().count(), 1000)  # two batches committed
        self.assertEqual(self.status(), ('registeringFiles', 0, ''))

        with fixed_batches(500), CaptureQueriesContext(connection) as queries:
            job = self.register()   # the task redelivered

        self.assertTrue(job._resume)
        self.assertEqual(self.status(), ('finishedSuccessfully', 2500 * 5, ''))
        self.assertEqual(self.rows().count(), 2500)
        inserts = [q['sql'] for q in queries
                   if q['sql'].startswith('INSERT INTO "core_chrisfile"')]
        # the batches the first run committed are not inserted again, and no insert
        # fails on a row there already
        self.assertEqual(len(inserts), 3)
        self.assertFalse([sql for sql in inserts if 'ON CONFLICT' in sql])

    def test_the_next_attempt_after_a_soft_time_limit_resumes(self):
        with fixed_batches(1000), file_inserts(fail_at=2, error=SoftTimeLimitExceeded()), \
                self.assertRaises(SoftTimeLimitExceeded):
            self.register()

        with fixed_batches(1000):
            self.register()

        self.assertEqual(self.status(), ('finishedSuccessfully', 2500 * 5, ''))
        self.assertEqual(self.rows().count(), 2500)


class RunAgainTests(OutputRegistrationFixture, TestCase):

    def test_a_run_again_finds_the_objects_the_first_run_renamed(self):
        self.write(['a,b/c,d.txt', 'e.txt'])
        first = self.job()
        first._end_registration = mock.Mock(side_effect=Killed())

        with self.assertRaises(Killed):
            first.register_output()  # renamed the objects, then its worker died

        self.assertTrue(self.storage.obj_exists(f'{self.output}/ab/cd.txt'))
        self.register()  # the job JSON names the objects as the plugin wrote them

        self.assertEqual(self.status(), ('finishedSuccessfully', 10, ''))
        self.assertEqual(sorted(self.rows().values_list('fname', flat=True)),
                         [f'{self.output}/ab/cd.txt', f'{self.output}/e.txt'])

    def test_a_run_again_after_the_renames_of_object_storage_finds_them(self):
        self.write(['a,b/c,d.txt', 'e.txt'])
        first = self.job()
        # object storage's rename: each object copied to its new name and deleted
        first.storage_manager.sanitize_obj_names = types.MethodType(
            StorageManager.sanitize_obj_names, first.storage_manager)
        first._end_registration = mock.Mock(side_effect=Killed())

        with self.assertRaises(Killed):
            first.register_output()

        self.assertFalse(self.storage.obj_exists(f'{self.output}/a,b/c,d.txt'))
        self.register()

        self.assertEqual(self.status(), ('finishedSuccessfully', 10, ''))
        self.assertEqual(self.rows().count(), 2)

    def test_the_renamed_objects_are_walked_again_at_once_after_a_slow_walk(self):
        self.write(['a,b.txt', 'c.txt'])
        job = self.job()
        job.storage_env = 'swift'
        clock, walk = Clock(), job.storage_manager.walk
        walks = []

        def slow(folder):
            walks.append(folder)
            clock.now += 70  # longer than the minute of tries after the first walk
            return walk(folder)

        job.storage_manager.walk = slow
        with mock.patch.object(abstractjobs, 'time', clock):
            job.register_output()

        self.assertEqual(self.status(), ('finishedSuccessfully', 10, ''))
        self.assertEqual((len(walks), clock.sleeps), (2, []))

    def test_a_run_overtaken_before_renaming_renames_nothing(self):
        self.write(['a,b.txt', 'c.txt'])
        job = self.job()
        walk = job.storage_manager.walk

        def walked_then_overtaken(folder):
            yield from walk(folder)
            PluginInstanceLock.objects.filter(plugin_inst_id=self.inst.pk).update(
                owner_token=NEWER_RUN)

        job.storage_manager.walk = walked_then_overtaken
        job.register_output()

        self.assertTrue(self.storage.obj_exists(f'{self.output}/a,b.txt'))
        self.assertEqual(self.status(), ('registeringFiles', 0, ''))

    def test_files_missing_after_a_job_that_failed_keep_the_rows_found(self):
        self.write(['a.txt', 'b.txt'])
        self.names.append('gone.txt')

        with mock.patch.object(abstractjobs, 'time', Clock()), \
                self.assertLogs('plugininstances.services', 'ERROR') as logs:
            self.job('finishedWithError').register_output()

        self.assertIn('[CODE14,', logs.output[0])
        self.assertEqual(self.status(), ('finishedWithError', 10, 'CODE14'))
        self.assertEqual(self.rows().count(), 2)

    def test_the_files_of_a_zip_are_registered_once_when_run_again(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as zf:
            zf.writestr('a.txt', '123')
            zf.writestr('sub/b.txt', '45')

        for _ in range(2):  # the second run resumes the first
            PluginInstance.objects.filter(pk=self.inst.pk).update(status='registeringFiles')
            job = self.job()
            job.pfcon_client.pfcon_innetwork = False
            job._get_job_zip_data = mock.Mock(return_value=archive.getvalue())
            job.register_output()

        self.assertEqual(self.status(), ('finishedSuccessfully', 5, ''))
        self.assertEqual(sorted(self.rows().values_list('fname', flat=True)),
                         [f'{self.output}/a.txt', f'{self.output}/sub/b.txt'])

    def test_an_overtaken_run_stops_before_its_next_batch_and_writes_nothing_more(self):
        self.write([f'f{i}.txt' for i in range(30)])

        def a_newer_run_claims_it():
            PluginInstanceLock.objects.filter(plugin_inst_id=self.inst.pk).update(
                owner_token=NEWER_RUN)

        with fixed_batches(10), file_inserts(after=a_newer_run_claims_it):
            self.register()

        self.assertEqual(self.rows().count(), 10)
        self.assertEqual(self.status(), ('registeringFiles', 0, ''))

    def test_names_with_commas_are_left_alone_when_every_file_is_found(self):
        self.write(['a.txt'])
        Path(settings.MEDIA_ROOT, self.output, 'stray,b.txt').write_bytes(b'x')  # not the job's
        job = self.job()

        with mock.patch.object(job.storage_manager, 'sanitize_obj_names') as rename:
            job.register_output()

        rename.assert_not_called()
        self.assertEqual(self.status(), ('finishedSuccessfully', 5, ''))

    def test_names_a_later_listing_shows_are_renamed_on_that_try(self):
        self.write(['a,b.txt', 'c,d.txt'])
        job = self.job()
        storage = job.storage_manager
        # object storage's rename, and a listing that shows c,d.txt only from the second
        storage.sanitize_obj_names = types.MethodType(StorageManager.sanitize_obj_names,
                                                      storage)
        walk, walks = storage.walk, []

        def lagging(folder):
            walks.append(folder)
            return (obj for obj in walk(folder)
                    if len(walks) > 2 or not obj.path.endswith('c,d.txt'))

        storage.walk = lagging
        with mock.patch.object(abstractjobs, 'time', Clock()):
            job.register_output()

        self.assertEqual(self.status(), ('finishedSuccessfully', 10, ''))
        self.assertEqual(sorted(self.rows().values_list('fname', flat=True)),
                         [f'{self.output}/ab.txt', f'{self.output}/cd.txt'])

    def test_an_overtaken_run_looks_no_further_for_missing_files(self):
        self.write(['a.txt'])
        self.names.append('gone.txt')
        job = self.job()
        walk, walks = job.storage_manager.walk, []

        def walked_then_overtaken(folder):
            walks.append(folder)
            yield from walk(folder)
            PluginInstanceLock.objects.filter(plugin_inst_id=self.inst.pk).update(
                owner_token=NEWER_RUN)

        job.storage_manager.walk = walked_then_overtaken
        clock = Clock()
        with mock.patch.object(abstractjobs, 'time', clock):
            job.register_output()

        self.assertEqual((len(walks), clock.sleeps), (1, [1]))
        self.assertEqual(self.status(), ('registeringFiles', 0, ''))

    def test_a_copy_an_earlier_run_made_and_renamed_is_not_made_again(self):
        source = 'home/foo/uploads/a,b.txt'
        self.storage.upload_obj(source, b'123')
        job = self.job()
        copy = f'{self.output}/home_foo_uploads/a,b.txt'

        job._copy_to_output(source, copy)
        self.storage.sanitize_obj_names(self.output)  # the earlier run's rename
        job._copy_to_output(source, copy)             # the run again

        self.assertFalse(self.storage.obj_exists(copy))
        self.assertEqual(job._expected, {'home_foo_uploads/ab.txt'})

    def test_a_plugin_link_file_another_run_deleted_is_no_failure(self):
        job = self.job()
        missing = f'{self.output}/evil.chrislink'

        job._refuse_link_file(missing)  # gone already: no CODE07

        self.assertEqual(job.c_plugin_inst.error_code, '')

    def test_the_files_of_a_zip_with_commas_are_renamed_and_registered(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as zf:
            zf.writestr('x,y/a,b.txt', '123')
        job = self.job()
        job.pfcon_client.pfcon_innetwork = False
        job._get_job_zip_data = mock.Mock(return_value=archive.getvalue())

        job.register_output()

        self.assertEqual(self.status(), ('finishedSuccessfully', 3, ''))
        self.assertEqual(list(self.rows().values_list('fname', flat=True)),
                         [f'{self.output}/xy/ab.txt'])

    def test_symbolic_links_in_the_output_are_not_registered(self):
        self.write(['a.txt'])
        base = Path(settings.MEDIA_ROOT) / self.output
        os.symlink(base / 'a.txt', base / 'link.txt')

        self.register()

        self.assertEqual(list(self.rows().values_list('fname', flat=True)),
                         [f'{self.output}/a.txt'])
        self.assertEqual(self.status(), ('finishedSuccessfully', 5, ''))

    def test_a_link_file_another_run_inserted_meanwhile_is_taken(self):
        linked = 'home/foo/uploads'
        ChrisFolder.objects.get_or_create(path=linked, defaults={'owner': self.user})
        folder = self.inst.output_folder
        name = linked.replace('/', '_')
        job = self.job()
        mine = job._link_file(linked, self.user, folder, name)  # none yet: a new one
        other = job._link_file(linked, self.user, folder, name)
        other.save(name=name)                                   # the other run's

        self.assertEqual(job._save_link_file(mine, name).pk, other.pk)
        self.assertEqual(ChrisLinkFile.objects.filter(parent_folder=folder).count(), 1)

        another = ChrisLinkFile(path='home/foo/elsewhere', owner=self.user,
                                parent_folder=folder)
        with self.assertRaisesRegex(ValueError, 'links home/foo/uploads, not'):
            job._save_link_file(another, name)

    def test_a_soft_time_limit_while_a_link_file_is_written_reaches_its_task(self):
        link = ChrisLinkFile(path='home/foo/uploads', owner=self.user,
                             parent_folder=self.inst.output_folder)
        link.save(name='l')
        self.storage.delete_obj(link.fname.name)  # gone, as after pfcon's listing

        with mock.patch('core.models.connect_storage', return_value=self.storage), \
                mock.patch.object(self.storage, 'obj_exists',
                                  side_effect=SoftTimeLimitExceeded()), \
                self.assertRaises(SoftTimeLimitExceeded):
            link.save(name='l')

    def test_a_link_file_another_task_deleted_meanwhile_is_written_again(self):
        link = ChrisLinkFile(path='home/foo/uploads', owner=self.user,
                             parent_folder=self.inst.output_folder)
        link.save(name='l')
        storage, delete = self.storage, self.storage.delete_obj

        def deleted_first_by_another_task(path):
            delete(path)
            delete(path)  # FileNotFoundError: this task's delete comes second

        with mock.patch('core.models.connect_storage', return_value=storage), \
                mock.patch.object(storage, 'delete_obj', deleted_first_by_another_task):
            link.save(name='l')

        self.assertEqual(storage.download_obj(link.fname.name), b'home/foo/uploads')

        with mock.patch('core.models.connect_storage', return_value=storage), \
                mock.patch.object(storage, 'delete_obj',
                                  side_effect=RuntimeError('503')), \
                self.assertRaisesRegex(RuntimeError, '503'):
            link.save(name='l')  # still there: the delete failed

    def test_a_link_file_counts_the_bytes_of_its_object(self):
        ChrisFolder.objects.get_or_create(path='home/foo/uploads',
                                          defaults={'owner': self.user})
        job = self.job()

        job._create_chris_link_file('home/foo/uploads/', self.inst.output_folder)

        link = ChrisLinkFile.objects.get(parent_folder=self.inst.output_folder)
        self.assertEqual(job.c_plugin_inst.size,
                         len(self.storage.download_obj(link.fname.name)))
