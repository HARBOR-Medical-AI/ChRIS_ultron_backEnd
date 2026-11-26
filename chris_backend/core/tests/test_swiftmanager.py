"""
Integration tests for SwiftManager against a real Swift service.

These tests only run when STORAGE_ENV=swift, i.e. when the Swift storage
backend is active.  They validate all StorageManager interface methods
implemented by SwiftManager against the live Swift service started by
docker-compose_swift.yml.

Run via justfile:
    just set-storage swift
    just test-integration

SwiftManagerListingTests checks what ``ls`` and ``walk`` ask a mocked connection, and
runs in every mode.
"""

import unittest
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, TestCase, tag

from swiftclient.exceptions import ClientException

from core.storage.swiftmanager import SwiftManager
from core.tests.path_contract import PathContractTests


def _get_manager():
    return SwiftManager(settings.SWIFT_CONTAINER_NAME, settings.SWIFT_CONNECTION_PARAMS)


class SwiftManagerListingTests(SimpleTestCase):
    """
    What ``SwiftManager.ls`` and ``walk`` ask their connection, a mock: no Swift service
    needed.
    """

    def setUp(self):
        self.manager = SwiftManager('container', {})
        self.manager._conn = mock.Mock()
        self.get_container = self.manager._conn.get_container

    def pages(self, *pages):
        """
        Have the connection answer with ``pages`` of names, then with an empty one.
        """
        self.get_container.side_effect = [
            ({}, [{'name': name, 'bytes': len(name)} for name in page])
            for page in (*pages, [])]

    def test_a_limit_asks_for_that_many_objects_and_none_for_all_of_them(self):
        self.pages(['a/1', 'a/2'])
        self.assertEqual(self.manager.ls('a/', limit=2), ['a/1', 'a/2'])
        self.pages(['a/1', 'a/2'], ['a/3'])
        self.assertEqual(self.manager.ls('a/'), ['a/1', 'a/2', 'a/3'])

        self.assertEqual(self.get_container.call_args_list, [
            mock.call('container', prefix='a/', marker='', limit=2),
            mock.call('container', prefix='a/', marker='', limit=None),
            mock.call('container', prefix='a/', marker='a/2', limit=None),
            mock.call('container', prefix='a/', marker='a/3', limit=None)])

    def test_a_limit_past_one_page_asks_for_the_rest_after_it(self):
        self.manager.PAGE_SIZE = 2
        self.pages(['a/1', 'a/2'], ['a/3'])

        self.assertEqual(self.manager.ls('a/', limit=3), ['a/1', 'a/2', 'a/3'])
        self.assertEqual([c.kwargs['limit'] for c in self.get_container.call_args_list],
                         [2, 1])

    def test_a_walk_lists_the_folder_with_sizes_a_page_at_a_time(self):
        self.pages(['a/data/x', 'a/data/sub/y'], ['a/data/z'])

        objects = list(self.manager.walk('a/data/'))

        self.assertEqual(objects, [('a/data/x', 8), ('a/data/sub/y', 12), ('a/data/z', 8)])
        self.assertEqual({c.kwargs['prefix'] for c in self.get_container.call_args_list},
                         {'a/data/'})  # not a/data_2's objects

    def test_a_request_that_fails_is_made_again_on_its_own(self):
        error = ClientException('busy', http_status=503)
        self.get_container.side_effect = [({}, [{'name': 'a/1', 'bytes': 1}]), error,
                                          ({}, [{'name': 'a/2', 'bytes': 1}]), ({}, [])]

        with mock.patch('core.storage.swiftmanager.time.sleep'), \
                self.assertLogs('core.storage.swiftmanager', 'ERROR'):
            self.assertEqual(self.manager.ls('a/'), ['a/1', 'a/2'])

        markers = [c.kwargs['marker'] for c in self.get_container.call_args_list]
        self.assertEqual(markers, ['', 'a/1', 'a/1', 'a/2'])

    def test_a_limit_past_what_there_is_takes_one_request(self):
        self.pages(['a/1', 'a/2'])

        self.assertEqual(self.manager.ls('a/', limit=101), ['a/1', 'a/2'])
        self.get_container.assert_called_once_with('container', prefix='a/', marker='',
                                                   limit=101)

    def test_a_walk_leaves_the_markers_of_folders_out(self):
        self.pages(['a/data/sub/', 'a/data/sub/x'])

        self.assertEqual(list(self.manager.walk('a/data')), [('a/data/sub/x', 12)])

    def test_is_missing_says_swift_answered_404(self):
        self.assertTrue(self.manager.is_missing(ClientException('x', http_status=404)))
        self.assertFalse(self.manager.is_missing(ClientException('x', http_status=503)))
        self.assertFalse(self.manager.is_missing(FileNotFoundError('x')))

    def answers(self, *pages):
        """
        Have the connection answer each request with the next page of names.
        """
        self.get_container.side_effect = [
            ({}, [{'name': name, 'bytes': len(name)} for name in page]) for page in pages]

    def asked(self):
        return [(c.kwargs['prefix'], c.kwargs['limit'])
                for c in self.get_container.call_args_list]

    def test_a_path_exists_when_its_object_or_its_folder_does(self):
        self.answers(['a'])
        self.assertTrue(self.manager.path_exists('a'))  # the object at a, listed first
        self.answers(['a-b'], ['a/1'])
        self.assertTrue(self.manager.path_exists('a'))  # its folder
        self.answers(['a-b'], [])
        self.assertFalse(self.manager.path_exists('a'))  # a sibling, not a

        self.assertEqual(self.asked(), [('a', 1), ('a', 1), ('a/', 1), ('a', 1), ('a/', 1)])
        self.assertFalse(self.manager.path_exists(''))

    def test_a_listing_keeps_to_the_object_and_the_folder_its_path_names(self):
        self.answers(['a/data'], ['a/data/x'], [])
        self.assertEqual(self.manager.ls('a/data'), ['a/data', 'a/data/x'])
        self.answers(['a/data_2'], ['a/data/x'], [])
        self.assertEqual(self.manager.ls('a/data'), ['a/data/x'])
        self.answers(['a/data'])
        self.assertEqual(self.manager.ls('a/data', limit=1), ['a/data'])

        self.assertEqual(self.asked(), [('a/data', 1), ('a/data/', None), ('a/data/', None),
                                        ('a/data', 1), ('a/data/', None), ('a/data/', None),
                                        ('a/data', 1)])

    def test_deleting_an_object_swift_does_not_have_is_no_error(self):
        delete = self.manager._conn.delete_object
        delete.side_effect = ClientException('not found', http_status=404)

        with self.assertNoLogs('core.storage.swiftmanager', 'ERROR'):
            self.manager.delete_obj('a/1')

        delete.assert_called_once_with('container', 'a/1')  # not made again

    def test_copying_an_object_swift_does_not_have_raises_at_once(self):
        copy = self.manager._conn.copy_object
        copy.side_effect = ClientException('not found', http_status=404)

        with self.assertNoLogs('core.storage.swiftmanager', 'ERROR'), \
                self.assertRaises(ClientException) as raised:
            self.manager.copy_obj('a/1', 'b/1')

        self.assertTrue(self.manager.is_missing(raised.exception))
        copy.assert_called_once_with('container', 'a/1', '/container/b/1')  # not made again


@tag('integration')
@unittest.skipUnless(getattr(settings, 'STORAGE_ENV', '') == 'swift',
                     'Swift storage not configured')
class SwiftManagerConnectionTests(TestCase):

    def setUp(self):
        self.manager = _get_manager()

    def test_create_container(self):
        """Container creation should succeed (or be idempotent)."""
        self.manager.create_container()
        # call again to verify idempotency
        self.manager.create_container()


@tag('integration')
@unittest.skipUnless(getattr(settings, 'STORAGE_ENV', '') == 'swift',
                     'Swift storage not configured')
class SwiftManagerCRUDTests(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = _get_manager()
        cls.manager.create_container()

    def tearDown(self):
        super().tearDown()
        for key in self.manager.ls('test/'):
            self.manager.delete_obj(key)

    def test_upload_and_download_bytes(self):
        data = b'hello ChRIS from Swift!'
        self.manager.upload_obj('test/hello.txt', data, content_type='text/plain')
        result = self.manager.download_obj('test/hello.txt')
        self.assertEqual(result, data)

    def test_upload_and_download_str(self):
        data = 'string content'
        self.manager.upload_obj('test/string.txt', data, content_type='text/plain')
        result = self.manager.download_obj('test/string.txt')
        self.assertEqual(result, data.encode('utf-8'))

    def test_obj_exists(self):
        self.assertFalse(self.manager.obj_exists('test/nonexistent.txt'))
        self.manager.upload_obj('test/exists.txt', b'data')
        self.assertTrue(self.manager.obj_exists('test/exists.txt'))

    def test_path_exists(self):
        self.assertFalse(self.manager.path_exists('test/subdir/'))
        self.manager.upload_obj('test/subdir/file.txt', b'data')
        self.assertTrue(self.manager.path_exists('test/subdir/'))
        self.assertTrue(self.manager.path_exists('test/subdir/file.txt'))

    def test_ls(self):
        self.manager.upload_obj('test/ls/a.txt', b'a')
        self.manager.upload_obj('test/ls/b.txt', b'b')
        self.manager.upload_obj('test/ls/sub/c.txt', b'c')
        result = self.manager.ls('test/ls/')
        self.assertEqual(sorted(result),
                         ['test/ls/a.txt', 'test/ls/b.txt', 'test/ls/sub/c.txt'])

    def test_ls_empty_prefix(self):
        result = self.manager.ls('')
        self.assertEqual(result, [])

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


@tag('integration')
@unittest.skipUnless(getattr(settings, 'STORAGE_ENV', '') == 'swift',
                     'Swift storage not configured')
class SwiftManagerPathOpsTests(PathContractTests, TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = _get_manager()
        cls.manager.create_container()

    def tearDown(self):
        super().tearDown()
        for key in self.manager.ls('test/'):
            self.manager.delete_obj(key)

    def test_copy_path(self):
        self.manager.upload_obj('test/cp/a.txt', b'a')
        self.manager.upload_obj('test/cp/sub/b.txt', b'b')
        self.manager.copy_path('test/cp/', 'test/cp2/')
        # originals still exist
        self.assertTrue(self.manager.obj_exists('test/cp/a.txt'))
        # copies exist
        self.assertTrue(self.manager.obj_exists('test/cp2/a.txt'))
        self.assertTrue(self.manager.obj_exists('test/cp2/sub/b.txt'))
        self.assertEqual(self.manager.download_obj('test/cp2/sub/b.txt'), b'b')

    def test_move_path(self):
        self.manager.upload_obj('test/mv/a.txt', b'a')
        self.manager.upload_obj('test/mv/sub/b.txt', b'b')
        self.manager.move_path('test/mv/', 'test/mv2/')
        # originals gone
        self.assertFalse(self.manager.obj_exists('test/mv/a.txt'))
        self.assertFalse(self.manager.obj_exists('test/mv/sub/b.txt'))
        # moved objects exist
        self.assertTrue(self.manager.obj_exists('test/mv2/a.txt'))
        self.assertEqual(self.manager.download_obj('test/mv2/sub/b.txt'), b'b')

    def test_delete_path(self):
        self.manager.upload_obj('test/delpath/a.txt', b'a')
        self.manager.upload_obj('test/delpath/b.txt', b'b')
        self.manager.upload_obj('test/delpath/sub/c.txt', b'c')
        self.manager.delete_path('test/delpath/')
        self.assertEqual(self.manager.ls('test/delpath/'), [])

    def test_sanitize_obj_names(self):
        self.manager.upload_obj('test/san/fi,le.txt', b'data')
        self.manager.upload_obj('test/san/clean.txt', b'ok')
        result = self.manager.sanitize_obj_names('test/san/')
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
        # file with commas in stem but non-comma extension gets renamed, not deleted
        self.manager.upload_obj('test/san2/,,.txt', b'renamed')
        self.manager.upload_obj('test/san2/good.txt', b'keep')
        result = self.manager.sanitize_obj_names('test/san2/')
        # pure comma name -> deleted
        self.assertIn('test/san2/,,', result)
        self.assertEqual(result['test/san2/,,'], '')
        self.assertFalse(self.manager.obj_exists('test/san2/,,'))
        # comma stem + extension -> renamed to .txt
        self.assertIn('test/san2/,,.txt', result)
        self.assertEqual(result['test/san2/,,.txt'], 'test/san2/.txt')
        # clean file untouched
        self.assertTrue(self.manager.obj_exists('test/san2/good.txt'))


@tag('integration')
@unittest.skipUnless(getattr(settings, 'STORAGE_ENV', '') == 'swift',
                     'Swift storage not configured')
class SwiftManagerLargeOpsTests(TestCase):
    """Test operations at moderate scale to validate listings and bulk deletes."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = _get_manager()
        cls.manager.create_container()

    def tearDown(self):
        super().tearDown()
        self.manager.delete_path('test/bulk/')

    def test_ls_many_objects(self):
        count = 50
        for i in range(count):
            self.manager.upload_obj(f'test/bulk/{i:04d}.dat', b'x')
        result = self.manager.ls('test/bulk/')
        self.assertEqual(len(result), count)

    def test_ls_lists_its_limit_at_most(self):
        names = {f'test/bulk/{i:04d}.dat' for i in range(5)}
        for name in names:
            self.manager.upload_obj(name, b'x')

        some = self.manager.ls('test/bulk/', limit=3)

        self.assertEqual(len(some), 3)
        self.assertLessEqual(set(some), names)
        self.assertEqual(set(self.manager.ls('test/bulk/', limit=10)), names)

    def test_walk_lists_the_folder_with_sizes(self):
        sizes = {f'test/bulk/{i:04d}.dat': i for i in range(5)}
        for name, size in sizes.items():
            self.manager.upload_obj(name, b'x' * size)
        self.manager.upload_obj('test/bulk_2/sibling.dat', b'x')
        self.addCleanup(self.manager.delete_path, 'test/bulk_2/')

        # a walk asks for pages of Swift's own size: PAGE_SIZE sizes only limited listings
        self.assertEqual(dict(self.manager.walk('test/bulk')), sizes)

    def test_bulk_delete(self):
        count = 25
        for i in range(count):
            self.manager.upload_obj(f'test/bulk/{i:04d}.dat', b'x')
        self.manager.delete_path('test/bulk/')
        self.assertEqual(self.manager.ls('test/bulk/'), [])
