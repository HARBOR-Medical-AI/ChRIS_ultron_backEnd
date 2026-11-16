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

from django.test import TestCase

from core.storage.plain_fs import FilesystemManager


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


class FilesystemManagerPathOpsTests(TestCase):

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

    def test_delete_path_nonexistent_raises(self):
        """delete_path raises FileNotFoundError for non-existent paths."""
        with self.assertRaises(FileNotFoundError):
            self.manager.delete_path('test/no_such_path')

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

    def test_bulk_delete(self):
        count = 25
        for i in range(count):
            self.manager.upload_obj(f'test/bulk/{i:04d}.dat', b'x')
        self.manager.delete_path('test/bulk')
        self.assertFalse(self.manager.path_exists('test/bulk'))
