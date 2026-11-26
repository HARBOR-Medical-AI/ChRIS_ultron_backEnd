"""
Unit tests for what every storage manager shares: the paths ChRIS gives objects whose
names have commas (``sanitized_path``), and, on an object store kept in memory, what
Swift and S3 share: the renames of ``sanitize_obj_names`` and the operations on paths.
"""

from itertools import islice
from typing import AnyStr, Iterator, Optional

from django.test import SimpleTestCase

from core.storage import StorageObject, in_tree, is_plain_path, sanitized_path
from core.storage.storagemanager import ObjectStorageManager, sanitized_name
from core.tests.path_contract import PathContractTests


class SanitizedPathTests(SimpleTestCase):

    def test_a_name_loses_its_commas_and_one_of_white_space_only_is_empty(self):
        for name, expected in (('a,b', 'ab'), (' a ', ' a '), (', ,', ''), ('  ', ''),
                               ('', '')):
            with self.subTest(name=name):
                self.assertEqual(sanitized_name(name), expected)

    def test_names_lose_their_commas_and_blank_folders_go(self):
        for path, expected in (('a.txt', 'a.txt'),
                               ('fi,le.txt', 'file.txt'),
                               ('SAG,T1/te,st.txt', 'SAGT1/test.txt'),
                               (', ,/SAG,T1,MPRAGE/test2.txt', 'SAGT1MPRAGE/test2.txt'),
                               ('a/,,/ ,/b', 'a/b'),
                               ('a//b', 'a/b'),
                               (',,.txt', '.txt'),
                               (' x ,/y', ' x /y')):
            with self.subTest(path=path):
                self.assertEqual(sanitized_path(path), expected)

    def test_an_object_whose_name_is_blank_without_commas_is_deleted(self):
        # a key ending in a slash (the marker of a folder) has an empty name
        for path in (',,', 'a/, ,', 'a,/ ', 'a/'):
            with self.subTest(path=path):
                self.assertEqual(sanitized_path(path), '')

    def test_a_sanitized_path_is_its_own(self):
        for path in ('fi,le.txt', ', ,/SAG,T1/x', ',,.txt'):
            with self.subTest(path=path):
                once = sanitized_path(path)
                self.assertEqual(sanitized_path(once), once)


class MemoryStorage(ObjectStorageManager):
    """
    Objects in a dict: an object store as Swift and S3 are, whose listing of a prefix
    holds the names that start with it, in their order.
    """

    def __init__(self, *paths):
        self.objects = {path: path.encode() for path in paths}

    def create_container(self) -> None:
        pass

    def _objects(self, prefix: str,
                 limit: Optional[int] = None) -> Iterator[StorageObject]:
        names = (name for name in sorted(self.objects) if name.startswith(prefix))
        for name in islice(names, limit):
            yield StorageObject(name, len(self.objects[name]))

    def obj_exists(self, file_path: str) -> bool:
        return file_path in self.objects

    def upload_obj(self, file_path: str, contents: AnyStr,
                   content_type: Optional[str] = None):
        data = contents.encode() if isinstance(contents, str) else contents
        self.objects[file_path] = data

    def download_obj(self, file_path: str) -> bytes:
        if file_path not in self.objects:
            raise FileNotFoundError(file_path)
        return self.objects[file_path]

    def copy_obj(self, src: str, dst: str) -> None:
        if src not in self.objects:
            raise FileNotFoundError(src)
        self.objects[dst] = self.objects[src]

    def delete_obj(self, file_path: str) -> None:
        self.objects.pop(file_path, None)


class InTreeTests(SimpleTestCase):

    def test_a_path_is_in_the_tree_of_its_folders_not_of_a_sibling_that_starts_alike(self):
        for path, folder, expected in (('a/data', 'a/data', True),
                                       ('a/data/', 'a/data', True),
                                       ('a/data/x', 'a/data/', True),
                                       ('a/data_2', 'a/data', False),
                                       ('a/data-x/y', 'a/data', False),
                                       ('a', 'a/data', False)):
            with self.subTest(path=path, folder=folder):
                self.assertEqual(in_tree(path, folder), expected)


class IsPlainPathTests(SimpleTestCase):

    def test_a_path_is_plain_when_no_name_in_it_is_empty_dot_or_dot_dot(self):
        for path in ('home', 'home/u/a.txt', 'home/u/.hidden', 'home/u/a..b'):
            with self.subTest(path=path):
                self.assertTrue(is_plain_path(path))
        for path in ('', '/home/u', 'home/u/', 'home//u', 'home/./u', 'home/u/..', '..'):
            with self.subTest(path=path):
                self.assertFalse(is_plain_path(path))


class ObjectStoragePathTests(PathContractTests, SimpleTestCase):
    """
    The path contract, and the cases only an object store has, on the code that Swift and
    S3 share.
    """

    def setUp(self):
        self.manager = MemoryStorage()

    def test_an_object_and_a_folder_of_one_name_are_one_path_a_slash_the_folder(self):
        self.upload('test/a', 'test/a/x', 'test/a-b')

        self.assertEqual(self.manager.ls('test/a'), ['test/a', 'test/a/x'])
        self.assertEqual(self.manager.ls('test/a', limit=1), ['test/a'])
        self.assertEqual(self.manager.ls('test/a/'), ['test/a/x'])
        self.manager.delete_path('test/a/')
        self.assertEqual(sorted(self.manager.objects), ['test/a', 'test/a-b'])

    def test_a_folder_moves_into_a_folder_of_its_own(self):
        self.upload('test/a/x', 'test/a/sub/y')

        self.manager.move_path('test/a', 'test/a/sub/moved')

        self.assertEqual(sorted(self.manager.objects),
                         ['test/a/sub/moved/sub/y', 'test/a/sub/moved/x'])


class SanitizeObjNamesTests(SimpleTestCase):

    def test_objects_are_renamed_or_deleted_under_the_folder_only(self):
        storage = MemoryStorage('out/fi,le.txt', 'out/, ,/a,b/c.txt', 'out/,,',
                                'out/clean.txt', 'out_2/x,y', 'out')

        for path in ('out', 'out/'):
            with self.subTest(path=path):
                changes = storage.sanitize_obj_names(path)

                self.assertEqual(changes, {} if path == 'out/' else {
                    'out/fi,le.txt': 'out/file.txt', 'out/, ,/a,b/c.txt': 'out/ab/c.txt',
                    'out/,,': ''})
        self.assertEqual(sorted(storage.objects), [
            'out', 'out/ab/c.txt', 'out/clean.txt', 'out/file.txt', 'out_2/x,y'])
        self.assertEqual(storage.objects['out/file.txt'], b'out/fi,le.txt')

    def test_an_object_listed_but_gone_is_taken_as_renamed_already(self):
        storage = MemoryStorage('out/a,b', 'out/c,d')
        walk = storage.walk

        def renamed_meanwhile(folder):  # by another task, once listed
            listed = list(walk(folder))
            storage.objects['out/ab'] = storage.objects.pop('out/a,b')
            return iter(listed)

        storage.walk = renamed_meanwhile
        storage.sanitize_obj_names('out')

        self.assertEqual(sorted(storage.objects), ['out/ab', 'out/cd'])

    def test_any_other_failure_of_a_rename_raises(self):
        storage = MemoryStorage('out/a,b')

        def refused(src, dst):
            raise PermissionError(src)

        storage.copy_obj = refused

        with self.assertRaises(PermissionError):
            storage.sanitize_obj_names('out')
