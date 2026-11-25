"""
Unit tests for FilesystemManager.

These tests always run regardless of STORAGE_ENV since FilesystemManager
operates on a local temporary directory with no external service dependency.

Run via justfile:
    just test-unit
"""

import os
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase

from core.storage.plain_fs import FilesystemManager
from core.tests.path_contract import PathContractTests


class FilesystemManagerConnectionTests(TestCase):

    def test_create_container(self):
        """Container (base directory) creation should succeed and be idempotent."""
        with tempfile.TemporaryDirectory() as tmp:
            base = f'{tmp}/store'
            manager = FilesystemManager(base)
            manager.create_container()
            # call again to verify idempotency
            manager.create_container()


class FilesystemManagerCRUDTests(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._tmp = tempfile.TemporaryDirectory()
        cls.manager = FilesystemManager(cls._tmp.name)
        cls.manager.create_container()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()
        super().tearDownClass()

    def tearDown(self):
        super().tearDown()
        if self.manager.path_exists('test'):
            self.manager.delete_path('test')

    def test_upload_and_download_bytes(self):
        data = b'hello ChRIS from filesystem!'
        self.manager.upload_obj('test/hello.txt', data)
        result = self.manager.download_obj('test/hello.txt')
        self.assertEqual(result, data)

    def test_upload_and_download_str(self):
        data = 'string content gets encoded to utf-8'
        self.manager.upload_obj('test/string.txt', data, content_type='text/plain')
        result = self.manager.download_obj('test/string.txt')
        self.assertEqual(result, data.encode('utf-8'))

    def test_obj_exists(self):
        self.assertFalse(self.manager.obj_exists('test/nonexistent.txt'))
        self.manager.upload_obj('test/exists.txt', b'data')
        self.assertTrue(self.manager.obj_exists('test/exists.txt'))

    def test_path_exists(self):
        self.assertFalse(self.manager.path_exists('test/subdir'))
        self.manager.upload_obj('test/subdir/file.txt', b'data')
        self.assertTrue(self.manager.path_exists('test/subdir'))
        self.assertTrue(self.manager.path_exists('test/subdir/file.txt'))

    def test_ls(self):
        self.manager.upload_obj('test/ls/a.txt', b'a')
        self.manager.upload_obj('test/ls/b.txt', b'b')
        self.manager.upload_obj('test/ls/sub/c.txt', b'c')
        result = self.manager.ls('test/ls')
        self.assertEqual(sorted(result),
                         ['test/ls/a.txt', 'test/ls/b.txt', 'test/ls/sub/c.txt'])

    def test_ls_single_file(self):
        """ls of an exact file path returns a list with just that path."""
        self.manager.upload_obj('test/single.txt', b'data')
        result = self.manager.ls('test/single.txt')
        self.assertEqual(result, ['test/single.txt'])

    def test_delete_obj(self):
        self.manager.upload_obj('test/del.txt', b'delete me')
        self.assertTrue(self.manager.obj_exists('test/del.txt'))
        self.manager.delete_obj('test/del.txt')
        self.assertFalse(self.manager.obj_exists('test/del.txt'))

    def test_copy_obj(self):
        self.manager.upload_obj('test/src.txt', b'copy me')
        self.manager.copy_obj('test/src.txt', 'test/dst.txt')
        self.assertTrue(self.manager.obj_exists('test/dst.txt'))
        self.assertEqual(self.manager.download_obj('test/dst.txt'), b'copy me')
        # source still exists
        self.assertTrue(self.manager.obj_exists('test/src.txt'))


class FilesystemManagerPathOpsTests(PathContractTests, TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._tmp = tempfile.TemporaryDirectory()
        cls.manager = FilesystemManager(cls._tmp.name)
        cls.manager.create_container()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()
        super().tearDownClass()

    def tearDown(self):
        super().tearDown()
        if self.manager.path_exists('test'):
            self.manager.delete_path('test')

    def test_copy_path(self):
        self.manager.upload_obj('test/cp/a.txt', b'a')
        self.manager.upload_obj('test/cp/sub/b.txt', b'b')
        self.manager.copy_path('test/cp', 'test/cp2')
        # originals still exist
        self.assertTrue(self.manager.obj_exists('test/cp/a.txt'))
        # copies exist
        self.assertTrue(self.manager.obj_exists('test/cp2/a.txt'))
        self.assertTrue(self.manager.obj_exists('test/cp2/sub/b.txt'))
        self.assertEqual(self.manager.download_obj('test/cp2/sub/b.txt'), b'b')

    def test_move_path(self):
        self.manager.upload_obj('test/mv/a.txt', b'a')
        self.manager.upload_obj('test/mv/sub/b.txt', b'b')
        self.manager.move_path('test/mv', 'test/mv2')
        # originals gone
        self.assertFalse(self.manager.path_exists('test/mv'))
        # moved objects exist
        self.assertTrue(self.manager.obj_exists('test/mv2/a.txt'))
        self.assertEqual(self.manager.download_obj('test/mv2/sub/b.txt'), b'b')

    def test_delete_path(self):
        self.manager.upload_obj('test/delpath/a.txt', b'a')
        self.manager.upload_obj('test/delpath/b.txt', b'b')
        self.manager.upload_obj('test/delpath/sub/c.txt', b'c')
        self.manager.delete_path('test/delpath')
        self.assertFalse(self.manager.path_exists('test/delpath'))

    def test_sanitize_obj_names(self):
        self.manager.upload_obj('test/san/fi,le.txt', b'data')
        self.manager.upload_obj('test/san/clean.txt', b'ok')
        result = self.manager.sanitize_obj_names('test/san')
        # comma-containing file should be renamed
        self.assertIn('test/san/fi,le.txt', result)
        self.assertEqual(result['test/san/fi,le.txt'], 'test/san/file.txt')
        # clean file should not appear in result
        self.assertNotIn('test/san/clean.txt', result)
        # new file exists, old one gone
        self.assertTrue(self.manager.obj_exists('test/san/file.txt'))
        self.assertFalse(self.manager.obj_exists('test/san/fi,le.txt'))

    def test_sanitize_deletes_comma_only_names(self):
        # file whose ENTIRE name is only commas and whitespace gets deleted
        self.manager.upload_obj('test/san2/,,', b'junk')
        self.manager.upload_obj('test/san2/good.txt', b'keep')
        result = self.manager.sanitize_obj_names('test/san2')
        # pure comma name -> deleted
        self.assertIn('test/san2/,,', result)
        self.assertEqual(result['test/san2/,,'], '')
        self.assertFalse(self.manager.obj_exists('test/san2/,,'))
        # clean file untouched
        self.assertTrue(self.manager.obj_exists('test/san2/good.txt'))

    def test_sanitize_merges_a_folder_of_commas_into_its_parent(self):
        self.manager.upload_obj('test/san3/a,b/, ,/c,d.txt', b'data')

        result = self.manager.sanitize_obj_names('test/san3')

        self.assertEqual(result, {'test/san3/a,b/, ,/c,d.txt': 'test/san3/ab/cd.txt'})
        self.assertEqual(self.manager.ls('test/san3'), ['test/san3/ab/cd.txt'])


class FilesystemManagerLargeOpsTests(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._tmp = tempfile.TemporaryDirectory()
        cls.manager = FilesystemManager(cls._tmp.name)
        cls.manager.create_container()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()
        super().tearDownClass()

    def tearDown(self):
        super().tearDown()
        if self.manager.path_exists('test'):
            self.manager.delete_path('test')

    def test_ls_many_objects(self):
        count = 50
        for i in range(count):
            self.manager.upload_obj(f'test/bulk/{i:04d}.dat', b'x')
        result = self.manager.ls('test/bulk')
        self.assertEqual(len(result), count)

    def test_ls_lists_its_limit_at_most(self):
        names = {f'test/bulk/{i:04d}.dat' for i in range(5)}
        for name in names:
            self.manager.upload_obj(name, b'x')

        some = self.manager.ls('test/bulk/', limit=3)

        self.assertEqual(len(some), 3)
        self.assertLessEqual(set(some), names)
        self.assertEqual(set(self.manager.ls('test/bulk/', limit=10)), names)
        self.assertEqual(self.manager.ls('test/bulk/0000.dat', limit=1),
                         ['test/bulk/0000.dat'])  # a file's own path

    def test_ls_with_a_limit_finds_the_files_it_finds_without(self):
        base = Path(self._tmp.name)
        for name in ('a.txt', 'sub/b.txt', 'sub/deeper/c.txt'):
            self.manager.upload_obj(f'test/bulk/{name}', b'x')
        self.manager.upload_obj('elsewhere/d.txt', b'x')
        self.addCleanup(self.manager.delete_path, 'elsewhere')
        os.symlink(base / 'elsewhere', base / 'test/bulk/to_a_dir')
        os.symlink(base / 'test/bulk/a.txt', base / 'test/bulk/to_a_file')
        os.symlink(base / 'nowhere', base / 'test/bulk/dangling')

        everything = self.manager.ls('test/bulk')

        self.assertEqual(sorted(everything), ['test/bulk/a.txt', 'test/bulk/sub/b.txt',
                                              'test/bulk/sub/deeper/c.txt',
                                              'test/bulk/to_a_file'])
        self.assertEqual(sorted(self.manager.ls('test/bulk', limit=10)),
                         sorted(everything))
        self.assertEqual(self.manager.ls('test/nothing', limit=10), [])

    def test_walk_lists_the_files_under_a_folder_with_their_sizes(self):
        base = Path(self._tmp.name)
        for name, data in (('a.txt', b'1'), ('sub/b.txt', b'22'), ('sub/deeper/c', b'333')):
            self.manager.upload_obj(f'test/bulk/{name}', data)
        self.manager.upload_obj('test/bulk_2/d.txt', b'4444')  # a sibling, not under it
        self.manager.upload_obj('elsewhere/e.txt', b'x')
        self.addCleanup(self.manager.delete_path, 'elsewhere')
        os.symlink(base / 'elsewhere', base / 'test/bulk/to_a_dir')
        os.symlink(base / 'test/bulk/a.txt', base / 'test/bulk/to_a_file')
        os.symlink(base / 'nowhere', base / 'test/bulk/dangling')

        for folder in ('test/bulk', 'test/bulk/'):
            self.assertEqual(sorted(self.manager.walk(folder)), [
                ('test/bulk/a.txt', 1), ('test/bulk/sub/b.txt', 2),
                ('test/bulk/sub/deeper/c', 3)])  # symbolic links left out
        self.assertEqual(list(self.manager.walk('test/bulk/a.txt')), [])
        self.assertEqual(list(self.manager.walk('test/nothing')), [])
        self.assertEqual(sorted(o.path for o in self.manager.walk('test')), [
            'test/bulk/a.txt', 'test/bulk/sub/b.txt', 'test/bulk/sub/deeper/c',
            'test/bulk_2/d.txt'])

    def test_walk_calls_no_stat_but_its_directory_entries(self):
        for i in range(3):
            self.manager.upload_obj(f'test/bulk/sub{i}/{i}.dat', b'x')

        with mock.patch('os.stat', wraps=os.stat) as stat, \
                mock.patch('os.lstat', wraps=os.lstat) as lstat:
            objects = list(self.manager.walk('test/bulk'))

        self.assertEqual(len(objects), 3)
        # the directory entries' own stat (DirEntry.stat), not os.stat or os.lstat
        self.assertEqual((stat.call_count, lstat.call_count), (0, 0))

    def test_walk_skips_a_file_deleted_since_its_folder_was_read(self):
        for name in ('a.txt', 'b.txt'):
            self.manager.upload_obj(f'test/bulk/{name}', b'x')
        objects = self.manager.walk('test/bulk')

        first = next(objects)
        self.manager.delete_obj('test/bulk/b.txt' if first.path.endswith('a.txt')
                                else 'test/bulk/a.txt')

        self.assertEqual(list(objects), [])

    def test_listings_skip_a_folder_they_may_not_read(self):
        for name in ('a.txt', 'locked/b.txt'):
            self.manager.upload_obj(f'test/bulk/{name}', b'x')
        scandir = os.scandir

        def refused(path):
            if path.endswith('locked'):
                raise PermissionError(13, 'Permission denied', path)
            return scandir(path)

        with mock.patch('os.scandir', refused):
            self.assertEqual(self.manager.ls('test/bulk'), ['test/bulk/a.txt'])
            self.assertEqual([o.path for o in self.manager.walk('test/bulk')],
                             ['test/bulk/a.txt'])

    def test_is_missing_says_a_file_is_not_there(self):
        with self.assertRaises(FileNotFoundError) as missing:
            self.manager.download_obj('test/none')

        self.assertTrue(self.manager.is_missing(missing.exception))
        self.assertFalse(self.manager.is_missing(PermissionError('x')))

    def test_copy_obj_takes_a_link_to_the_same_file_as_the_copy(self):
        self.manager.upload_obj('test/bulk/src.txt', b'data')
        self.manager.upload_obj('test/bulk/other.txt', b'other')

        self.manager.copy_obj('test/bulk/src.txt', 'test/out/dst.txt')
        self.manager.copy_obj('test/bulk/src.txt', 'test/out/dst.txt')  # made again

        self.assertEqual(self.manager.download_obj('test/out/dst.txt'), b'data')
        with self.assertRaises(FileExistsError):
            self.manager.copy_obj('test/bulk/other.txt', 'test/out/dst.txt')

    def test_bulk_delete(self):
        count = 25
        for i in range(count):
            self.manager.upload_obj(f'test/bulk/{i:04d}.dat', b'x')
        self.manager.delete_path('test/bulk')
        self.assertFalse(self.manager.path_exists('test/bulk'))
