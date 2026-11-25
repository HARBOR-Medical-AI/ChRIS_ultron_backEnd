"""
Unit tests for what every storage manager shares: the paths ChRIS gives objects whose
names have commas (``sanitized_path``), and the renames of ``sanitize_obj_names`` for a
storage without folders (Swift, S3), on a storage kept in memory.
"""

from typing import Iterator

from django.test import SimpleTestCase

from core.storage import StorageManager, StorageObject, sanitized_path
from core.storage.storagemanager import sanitized_name


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


class MemoryStorage(StorageManager):
    """
    Objects in a dict, enough of a StorageManager for the default sanitize_obj_names.
    """

    def __init__(self, *paths):
        self.objects = {path: path.encode() for path in paths}

    def create_container(self) -> None:
        pass

    def walk(self, folder: str) -> Iterator[StorageObject]:
        prefix = folder.rstrip('/') + '/'
        for path in sorted(self.objects):
            if path.startswith(prefix):
                yield StorageObject(path, len(self.objects[path]))

    def copy_obj(self, src: str, dst: str) -> None:
        if src not in self.objects:
            raise FileNotFoundError(src)
        self.objects[dst] = self.objects[src]

    def delete_obj(self, file_path: str) -> None:
        del self.objects[file_path]


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
