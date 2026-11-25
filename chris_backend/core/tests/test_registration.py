"""
Tests for core.registration: the batch sizes, the folders created level by level, and
the files inserted a batch at a time, stopped and resumed.
"""

import math

from django.contrib.auth.models import User
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from core.models import ChrisFile, ChrisFolder
from core.registration import (BatchSizer, FileRegistrar, FolderIndex, Stopped,
                               parent_path)
from userfiles.models import UserFile


def fixed(size):
    """
    A sizer that keeps every batch at ``size`` rows.
    """
    return BatchSizer(size, least=size, most=size)


def statements(queries, start):
    return [q['sql'] for q in queries if q['sql'].startswith(start)]


class BatchSizerTests(SimpleTestCase):

    def test_a_batch_takes_what_the_last_rate_inserts_in_the_target_time(self):
        sizer = BatchSizer(1000, target=0.25, least=250, most=5000)

        sizer.took(1000, 0.1)          # 10,000 rows a second: 2,500, but twice 1,000 at most
        self.assertEqual(sizer.size, 2000)
        sizer.took(2000, 0.1)          # 20,000 a second: 5,000, twice 2,000 at most
        self.assertEqual(sizer.size, 4000)
        sizer.took(4000, 0.1)
        self.assertEqual(sizer.size, 5000)   # the most
        sizer.took(5000, 2.5)          # 2,000 a second
        self.assertEqual(sizer.size, 500)
        sizer.took(500, 10.0)
        self.assertEqual(sizer.size, 250)    # the least
        sizer.took(250, 0.0)
        self.assertEqual(sizer.size, 500)

    def test_a_statement_of_fewer_rows_than_the_least_says_nothing_of_the_rate(self):
        sizer = BatchSizer(1000, target=0.25, least=250, most=5000)

        for rows in (0, 1, 3, 249):    # a batch's few folders, mostly round trips
            sizer.took(rows, 0.002)
        self.assertEqual(sizer.size, 1000)


class RegistrationFixture(TestCase):

    root = 'home/foo/feeds/feed_1/pl_1/data'

    def setUp(self):
        self.user = User.objects.create_user(username='foo')
        ChrisFolder.objects.get_or_create(path=self.root, defaults={'owner': self.user})

    def paths(self, folders, files):
        """
        ``files`` file paths in each of ``folders`` folders two levels under the root.
        """
        return [f'{self.root}/d{i // 10}/e{i}/f{j}.txt' for i in range(folders)
                for j in range(files)]

    def parents(self, paths):
        return dict(ChrisFile.objects.filter(fname__in=paths).values_list(
            'fname', 'parent_folder__path'))


class FolderIndexTests(RegistrationFixture):

    def test_missing_folders_are_created_a_level_at_a_time_under_their_parents(self):
        index = FolderIndex(self.root, self.user, fixed(100))

        with CaptureQueriesContext(connection) as queries:
            index.ensure([f'{self.root}/a/b/c', f'{self.root}/a/x', f'{self.root}/a/b/d'])

        inserts = statements(queries, 'INSERT INTO "core_chrisfolder"')
        self.assertEqual(len(inserts), 3)  # a; a/b and a/x; a/b/c and a/b/d
        for path in ('a', 'a/b', 'a/x', 'a/b/c', 'a/b/d'):
            folder = ChrisFolder.objects.get(path=f'{self.root}/{path}')
            self.assertEqual(folder.parent.path, parent_path(folder.path))
            self.assertEqual(folder.owner, self.user)
            self.assertEqual(index.id(folder.path), folder.id)

        with CaptureQueriesContext(connection) as queries:
            index.ensure([f'{self.root}/a/b/c', self.root])  # all there: no query
        self.assertEqual(len(queries), 0)

    def test_the_index_reads_the_tree_in_one_query(self):
        FolderIndex(self.root, self.user).ensure([f'{self.root}/a/b', f'{self.root}/c'])

        with CaptureQueriesContext(connection) as queries:
            index = FolderIndex(self.root, self.user)

        self.assertEqual(len(queries), 1)
        self.assertNotIn('ORDER BY', queries[0]['sql'])
        self.assertEqual(len(index), 4)

    def test_a_level_of_more_folders_than_a_batch_takes_several_statements(self):
        index = FolderIndex(self.root, self.user, fixed(4))

        with CaptureQueriesContext(connection) as queries:
            index.ensure(f'{self.root}/{i}' for i in range(10))

        self.assertEqual(len(statements(queries, 'INSERT INTO "core_chrisfolder"')), 3)
        self.assertEqual(ChrisFolder.objects.filter(
            path__startswith=self.root + '/').count(), 10)

    def test_folders_another_run_created_meanwhile_are_taken_as_they_are(self):
        index = FolderIndex(self.root, self.user)
        other, _ = ChrisFolder.objects.get_or_create(path=f'{self.root}/a',
                                                     defaults={'owner': self.user})

        index.ensure([f'{self.root}/a/b', f'{self.root}/c'])

        self.assertEqual(index.id(f'{self.root}/a'), other.id)
        self.assertEqual(ChrisFolder.objects.get(path=f'{self.root}/a/b').parent_id,
                         other.id)
        self.assertEqual(ChrisFolder.objects.filter(path=f'{self.root}/a').count(), 1)

    def test_a_folder_outside_the_tree_is_refused(self):
        index = FolderIndex(self.root, self.user)

        for path in ('home/foo/uploads', self.root + '_2/a'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                index.ensure([path])

    def test_a_missing_root_is_created_with_its_ancestors(self):
        root = 'home/foo/feeds/feed_2/pl_2/data'

        index = FolderIndex(root, self.user)

        self.assertEqual(index.id(root), ChrisFolder.objects.get(path=root).id)
        self.assertTrue(ChrisFolder.objects.filter(path='home/foo/feeds/feed_2').exists())


class FileRegistrarTests(RegistrationFixture):

    def registrar(self, size, **kwargs):
        return FileRegistrar(UserFile, self.user,
                             FolderIndex(self.root, self.user, fixed(size)), **kwargs)

    def test_files_are_inserted_a_sorted_batch_at_a_time_in_their_folders(self):
        paths = self.paths(folders=40, files=50)  # 2,000 files, 40 + 4 folders
        registrar = self.registrar(300)

        with CaptureQueriesContext(connection) as queries:
            for path in reversed(paths):
                registrar.add(path, 3)
            registrar.close()

        inserts = statements(queries, 'INSERT INTO "core_chrisfile"')
        self.assertEqual(len(inserts), math.ceil(len(paths) / 300))
        # a batch's folders, two levels, a statement each at most
        folder_inserts = statements(queries, 'INSERT INTO "core_chrisfolder"')
        self.assertLessEqual(len(folder_inserts), 2 * len(inserts))
        self.assertEqual(statements(queries, 'SELECT'), [])  # nothing else asked

        parents = self.parents(paths)
        self.assertEqual(len(parents), len(paths))
        self.assertTrue(all(parent == parent_path(path) for path, parent in parents.items()))
        self.assertEqual((registrar.files, registrar.bytes), (2000, 6000))

    def test_a_batch_is_sorted_by_path(self):
        registrar = self.registrar(10)

        with CaptureQueriesContext(connection) as queries:
            for name in ('c', 'a', 'b'):
                registrar.add(f'{self.root}/{name}', 1)
            registrar.close()

        sql, = statements(queries, 'INSERT INTO "core_chrisfile"')
        self.assertLess(sql.index(f'{self.root}/a'), sql.index(f'{self.root}/b'))
        self.assertLess(sql.index(f'{self.root}/b'), sql.index(f'{self.root}/c'))

    def test_a_registration_asked_to_stop_writes_nothing_more(self):
        paths = self.paths(folders=5, files=10)
        answers = iter([False, False, True])
        registrar = self.registrar(20, should_stop=lambda: next(answers))

        with self.assertRaises(Stopped):
            for path in paths:
                registrar.add(path, 1)
            registrar.close()

        self.assertEqual(ChrisFile.objects.filter(fname__in=paths).count(), 40)
        self.assertEqual(registrar.files, 40)
        # the third batch's folder is not created either
        self.assertFalse(ChrisFolder.objects.filter(path=parent_path(paths[-1])).exists())

    def test_a_registration_resumed_registers_each_file_once(self):
        paths = self.paths(folders=5, files=10)
        first = self.registrar(20, should_stop=iter([False, False, True]).__next__)

        with self.assertRaises(Stopped):
            for path in paths:
                first.add(path, 1)
            first.close()

        again = self.registrar(20, resume=True)
        with CaptureQueriesContext(connection) as queries:
            for path in paths:
                again.add(path, 2)
            again.close()

        self.assertEqual(ChrisFile.objects.filter(fname__in=paths).count(), 50)
        self.assertEqual((again.files, again.bytes), (50, 100))
        inserts = statements(queries, 'INSERT INTO "core_chrisfile"')
        # the first two batches had rows already: no insert for them, no conflict
        self.assertEqual(len(inserts), 1)
        self.assertFalse([sql for sql in inserts if 'ON CONFLICT' in sql])

    def test_rows_another_run_inserted_are_skipped_without_resume(self):
        paths = self.paths(folders=1, files=10)
        registrar = self.registrar(100)
        registrar.folders.ensure([parent_path(paths[3])])
        UserFile.objects.create(fname=paths[3], owner=self.user,
                                parent_folder_id=registrar.folders.id(parent_path(paths[3])))

        with CaptureQueriesContext(connection) as queries:
            for path in paths:
                registrar.add(path, 1)
            registrar.close()

        self.assertEqual(ChrisFile.objects.filter(fname__in=paths).count(), 10)
        inserts = statements(queries, 'INSERT INTO "core_chrisfile"')
        self.assertEqual(['ON CONFLICT' in sql for sql in inserts], [False, True])
