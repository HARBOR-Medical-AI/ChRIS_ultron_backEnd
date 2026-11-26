"""
Integration tests for S3Manager against a real S3-compatible service (MinIO).

These tests only run when STORAGE_ENV=s3, i.e. when the S3 storage backend
is active.  They validate all StorageManager interface methods implemented
by S3Manager against the live MinIO instance started by docker-compose_s3.yml.

Run via justfile:
    just set-storage s3
    just test-integration

S3ManagerListingTests checks what ``ls`` and ``walk`` ask a mocked client, and runs in every
mode.
"""

import unittest
from unittest import mock

from botocore.exceptions import ClientError
from django.conf import settings
from django.test import SimpleTestCase, TestCase, tag

from core.storage.s3manager import S3Manager
from core.tests.path_contract import PathContractTests


def _get_manager():
    return S3Manager(settings.S3_BUCKET_NAME, settings.S3_CONNECTION_PARAMS)


class S3ManagerListingTests(SimpleTestCase):
    """
    What ``S3Manager.ls`` and ``walk`` ask their client, a mock: no S3 service needed.
    """

    def setUp(self):
        self.manager = S3Manager('bucket', {})
        self.manager._client = mock.Mock()
        self.manager._client.delete_objects.return_value = {}
        self.list = self.manager._client.list_objects_v2

    @staticmethod
    def page(keys, token=None):
        """
        A listing response holding ``keys``, followed by another one when ``token``.
        """
        resp = {'Contents': [{'Key': key, 'Size': len(key)} for key in keys],
                'IsTruncated': token is not None}
        if token is not None:
            resp['NextContinuationToken'] = token
        return resp

    def test_a_limit_asks_for_that_many_keys_at_most(self):
        self.list.side_effect = [self.page(['a/1', 'a/2']),
                                 self.page(['a/1'] * 1000, 't'), self.page(['a/2'] * 500),
                                 self.page(['a/1'])]

        self.assertEqual(self.manager.ls('a/', limit=2), ['a/1', 'a/2'])
        self.assertEqual(len(self.manager.ls('a/', limit=1500)), 1500)
        self.manager.ls('a/')

        self.assertEqual(self.list.call_args_list, [
            mock.call(Bucket='bucket', Prefix='a/', MaxKeys=2),
            mock.call(Bucket='bucket', Prefix='a/', MaxKeys=1000),
            mock.call(Bucket='bucket', Prefix='a/', MaxKeys=500, ContinuationToken='t'),
            mock.call(Bucket='bucket', Prefix='a/', MaxKeys=1000)])

    def test_a_request_that_fails_is_made_again_on_its_own(self):
        error = ClientError({'Error': {'Code': 'SlowDown'}}, 'ListObjectsV2')
        self.list.side_effect = [self.page(['a/1'], 't'), error, self.page(['a/2'])]

        with mock.patch('core.storage.s3manager.time.sleep'), \
                self.assertLogs('core.storage.s3manager', 'ERROR'):
            self.assertEqual(self.manager.ls('a/'), ['a/1', 'a/2'])

        tokens = [c.kwargs.get('ContinuationToken') for c in self.list.call_args_list]
        self.assertEqual(tokens, [None, 't', 't'])

    def test_a_walk_lists_the_folder_with_sizes_a_page_at_a_time(self):
        self.list.side_effect = [self.page(['a/data/x', 'a/data/sub/y'], 't'),
                                 self.page(['a/data/z'])]

        objects = list(self.manager.walk('a/data'))

        self.assertEqual(objects, [('a/data/x', 8), ('a/data/sub/y', 12), ('a/data/z', 8)])
        self.assertEqual({c.kwargs['Prefix'] for c in self.list.call_args_list},
                         {'a/data/'})  # not a/data_2's objects

    def test_a_walk_leaves_the_markers_of_folders_out(self):
        self.list.side_effect = [self.page(['a/data/sub/', 'a/data/sub/x'])]

        self.assertEqual(list(self.manager.walk('a/data')), [('a/data/sub/x', 12)])

    def test_a_listing_cut_short_without_a_token_says_so(self):
        cut = self.page(['a/1'])
        cut['IsTruncated'] = True
        self.list.side_effect = [cut]

        with self.assertRaisesRegex(ValueError, 'without a continuation token'):
            self.manager.ls('a/')

    def test_is_missing_says_s3_has_no_such_key(self):
        for code, missing in (('NoSuchKey', True), ('404', True), ('SlowDown', False)):
            with self.subTest(code=code):
                error = ClientError({'Error': {'Code': code}}, 'CopyObject')
                self.assertEqual(self.manager.is_missing(error), missing)

    def deleted(self):
        return [[o['Key'] for o in c.kwargs['Delete']['Objects']]
                for c in self.manager._client.delete_objects.call_args_list]

    def asked(self):
        return [(c.kwargs['Prefix'], c.kwargs['MaxKeys'])
                for c in self.list.call_args_list]

    def test_a_path_is_deleted_a_batch_of_keys_per_request_as_the_listing_goes(self):
        self.manager.DELETE_BATCH = 2
        self.list.side_effect = [self.page(['a']),
                                 self.page(['a/1'], 't'), self.page(['a/2', 'a/3'])]

        self.manager.delete_path('a')

        self.assertEqual(self.deleted(), [['a', 'a/1'], ['a/2', 'a/3']])
        requests = [c[0] for c in self.manager._client.mock_calls
                    if c[0] in ('list_objects_v2', 'delete_objects')]
        self.assertEqual(requests, ['list_objects_v2', 'list_objects_v2', 'delete_objects',
                                    'list_objects_v2', 'delete_objects'])  # as it goes

    def test_keys_s3_did_not_delete_raise_once_the_others_are_deleted(self):
        self.manager.DELETE_BATCH = 1
        self.list.side_effect = [self.page(['a/1', 'a/2', 'a/3'])]
        self.manager._client.delete_objects.side_effect = [
            {'Errors': [{'Key': 'a/1', 'Code': 'AccessDenied'}]},
            {'Errors': [{'Key': 'a/2', 'Code': 'NoSuchKey'}]},  # gone: deleted
            {}]

        failure = r'did not delete 1 objects .*a/1 \(AccessDenied\)'
        with self.assertRaisesRegex(OSError, failure):
            self.manager.delete_path('a/')

        self.assertEqual(self.deleted(), [['a/1'], ['a/2'], ['a/3']])

    def test_a_deleted_path_keeps_to_the_object_and_the_folder_it_names(self):
        self.list.side_effect = [self.page(['a']), self.page(['a/1'])]

        self.manager.delete_path('a')

        self.assertEqual(self.deleted(), [['a', 'a/1']])
        self.assertEqual(self.asked(), [('a', 1), ('a/', 1000)])

    def test_a_path_exists_when_its_object_or_its_folder_does(self):
        self.list.side_effect = [self.page(['a']),
                                 self.page(['a-b']), self.page(['a/1']),
                                 self.page(['a-b']), self.page([])]

        self.assertTrue(self.manager.path_exists('a'))   # the object at a, listed first
        self.assertTrue(self.manager.path_exists('a'))   # its folder
        self.assertFalse(self.manager.path_exists('a'))  # a sibling, not a

        self.assertEqual(self.asked(), [('a', 1), ('a', 1), ('a/', 1), ('a', 1), ('a/', 1)])
        self.assertFalse(self.manager.path_exists(''))

    def test_deleting_a_key_s3_does_not_have_is_no_error(self):
        delete = self.manager._client.delete_object
        delete.side_effect = ClientError({'Error': {'Code': 'NoSuchKey'}}, 'DeleteObject')

        with self.assertNoLogs('core.storage.s3manager', 'ERROR'):
            self.manager.delete_obj('a/1')

        delete.assert_called_once_with(Bucket='bucket', Key='a/1')  # not made again

    def test_copying_a_key_s3_does_not_have_raises_at_once(self):
        copy = self.manager._client.copy_object
        copy.side_effect = ClientError({'Error': {'Code': 'NoSuchKey'}}, 'CopyObject')

        with self.assertNoLogs('core.storage.s3manager', 'ERROR'), \
                self.assertRaises(ClientError) as raised:
            self.manager.copy_obj('a/1', 'b/1')

        self.assertTrue(self.manager.is_missing(raised.exception))
        copy.assert_called_once_with(Bucket='bucket', Key='b/1',  # not made again
                                     CopySource={'Bucket': 'bucket', 'Key': 'a/1'})


@tag('integration')
@unittest.skipUnless(getattr(settings, 'STORAGE_ENV', '') == 's3',
                     'S3 storage not configured')
class S3ManagerConnectionTests(TestCase):

    def setUp(self):
        self.manager = _get_manager()

    def test_create_container(self):
        """Bucket creation should succeed (or be idempotent)."""
        self.manager.create_container()
        # call again to verify idempotency
        self.manager.create_container()


@tag('integration')
@unittest.skipUnless(getattr(settings, 'STORAGE_ENV', '') == 's3',
                     'S3 storage not configured')
class S3ManagerCRUDTests(TestCase):

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
        data = b'hello ChRIS from S3!'
        self.manager.upload_obj('test/hello.txt', data, content_type='text/plain')
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
@unittest.skipUnless(getattr(settings, 'STORAGE_ENV', '') == 's3',
                     'S3 storage not configured')
class S3ManagerPathOpsTests(PathContractTests, TestCase):

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
@unittest.skipUnless(getattr(settings, 'STORAGE_ENV', '') == 's3',
                     'S3 storage not configured')
class S3ManagerLargeOpsTests(TestCase):
    """Test operations at moderate scale to validate pagination and batch delete."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = _get_manager()
        cls.manager.create_container()

    def tearDown(self):
        super().tearDown()
        self.manager.delete_path('test/bulk/')

    def test_ls_many_objects(self):
        """Verify listing works beyond a single page (1000 objects)."""
        count = 50  # keep fast for CI; increase manually to 1100+ to test pagination
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

        with mock.patch.object(self.manager, 'PAGE_SIZE', 2):  # a request per 2 keys
            self.assertEqual(dict(self.manager.walk('test/bulk')), sizes)

    def test_batch_delete(self):
        """Verify delete_path uses batch delete efficiently."""
        count = 25
        for i in range(count):
            self.manager.upload_obj(f'test/bulk/{i:04d}.dat', b'x')
        self.manager.delete_path('test/bulk/')
        self.assertEqual(self.manager.ls('test/bulk/'), [])
