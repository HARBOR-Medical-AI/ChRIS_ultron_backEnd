
import io
import logging
import os
from contextlib import contextmanager
from unittest import mock

from django.test import TestCase, tag
from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from psycopg.errors import UniqueViolation

from core.models import (ChrisFolder, ChrisFile, ChrisLinkFile, PathAccessError,
                         validate_path_access, user_can_access_obj)
from core.serializers import public_link_follows
from core.storage import connect_storage
from userfiles.models import UserFile


class ModelTests(TestCase):

    def setUp(self):
        # avoid cluttered console output (for instance logging all the http requests)
        logging.disable(logging.WARNING)

        # superuser chris (owner of root folders)
        self.chris_username = 'chris'

        # create normal user
        self.username = 'foo'
        self.password = 'bar'
        User.objects.create_user(username=self.username, password=self.password)

    def tearDown(self):
        # re-enable logging
        logging.disable(logging.NOTSET)


class ChrisFolderModelTests(ModelTests):

    def setUp(self):
        super(ChrisFolderModelTests, self).setUp()

    def test_get_first_existing_folder_ancestor(self):
        """
        Test whether custom get_first_existing_folder_ancestor method returns the
        closest ancestor folder (by path prefix including the passed path itself) that
        exists in the DB.
        """
        folder = ChrisFolder.get_first_existing_folder_ancestor('')
        self.assertEqual(folder.path, '')
        folder = ChrisFolder.get_first_existing_folder_ancestor('home')
        self.assertEqual(folder.path, 'home')
        folder = ChrisFolder.get_first_existing_folder_ancestor('home/12345678/999999')
        self.assertEqual(folder.path, 'home')
        folder = ChrisFolder.get_first_existing_folder_ancestor('home/12345678/file.txt')
        self.assertEqual(folder.path, 'home')


class ChrisFileModelTests(ModelTests):

    def setUp(self):
        super(ChrisFileModelTests, self).setUp()

        # create a ChrisFile instance
        owner = User.objects.get(username=self.username)
        self.upload_path = f'home/{self.username}/uploads/file1.txt'
        folder_path = os.path.dirname(self.upload_path)
        (parent_folder, _) = ChrisFolder.objects.get_or_create(path=folder_path,
                                                               owner=owner)
        self.file = ChrisFile(parent_folder=parent_folder, owner=owner)
        self.file.fname.name = self.upload_path
        self.file.save()

    def test_move(self):
        """
        Test whether custom 'move' method successfully updates a ChrisFile with
        the correct path and parent folder.
        """
        new_path =  f'home/{self.username}/tests/file1.txt'

        storage_manager_mock = mock.Mock()

        with mock.patch('core.models.connect_storage') as connect_storage_mock:
            connect_storage_mock.return_value=storage_manager_mock
            self.file.move(new_path)
            self.assertEqual(self.file.fname.name, new_path)
            self.assertEqual(self.file.parent_folder.path, f'home/{self.username}/tests')
            self.assertEqual(storage_manager_mock.method_calls, [
                mock.call.delete_obj(new_path),  # an object no row names
                mock.call.copy_obj(self.upload_path, new_path),
                mock.call.delete_obj(self.upload_path)])


class FileSavesAndMovesTests(TestCase):
    """
    What a failed save of a file deletes from storage, and how files and link files move:
    neither loses its object, nor another's.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='moves')
        self.storage = connect_storage(settings)
        self.addCleanup(self.storage.delete_path, 'home/moves/uploads/t')
        self.file = self.chris_file('home/moves/uploads/t/a.txt', b'a')
        self.other = self.chris_file('home/moves/uploads/t/b.txt', b'b')
        self.folder = self.file.parent_folder

    def chris_file(self, path, contents):
        self.storage.upload_obj(path, contents)
        (folder, _) = ChrisFolder.objects.get_or_create(path=os.path.dirname(path),
                                                        owner=self.user)
        f = ChrisFile(owner=self.user, parent_folder=folder)
        f.fname.name = path
        f.save()
        return f

    def test_an_upload_whose_row_fails_is_deleted_from_storage(self):
        new = ChrisFile(owner=self.user, parent_folder=self.folder,
                        fname=ContentFile(b'c', name='c.txt'))
        new.fname.name = 'home/moves/uploads/t/c.txt'

        with mock.patch.object(ChrisFile, '_do_insert', side_effect=DatabaseError('row')), \
                self.assertRaisesRegex(DatabaseError, 'row'), transaction.atomic():
            new.save()

        self.assertFalse(self.storage.obj_exists(new.fname.name))  # the name it got

    def test_a_failed_save_of_a_row_leaves_storage_alone(self):
        with mock.patch.object(ChrisFile, '_do_update', side_effect=DatabaseError('row')), \
                self.assertRaises(DatabaseError), transaction.atomic():
            self.file.save()  # a row saved again

        twin = ChrisFile(owner=self.user, parent_folder=self.folder)
        twin.fname.name = self.other.fname.name
        with self.assertRaises(IntegrityError), transaction.atomic():
            twin.save()  # a row for an object in storage: another row has it

        self.assertEqual(self.storage.download_obj(self.file.fname.name), b'a')
        self.assertEqual(self.storage.download_obj(self.other.fname.name), b'b')

    def upload_refused(self, error):
        """
        An upload whose row the database refuses with ``error``, the IntegrityError
        raised, and the name its object was stored under.
        """
        new = ChrisFile(owner=self.user, parent_folder=self.folder,
                        fname=ContentFile(b'c', name='c.txt'))
        new.fname.name = 'home/moves/uploads/t/c.txt'

        with mock.patch.object(ChrisFile, '_do_insert', side_effect=error), \
                self.assertRaises(IntegrityError), transaction.atomic():
            new.save()
        return new.fname.name

    def test_an_upload_refused_for_its_name_leaves_storage_alone(self):
        taken = IntegrityError('duplicate key value violates unique constraint')
        taken.__cause__ = UniqueViolation()  # another row took the name meanwhile

        self.assertTrue(self.storage.obj_exists(self.upload_refused(taken)))

    def test_an_upload_refused_by_another_constraint_is_deleted_from_storage(self):
        refused = IntegrityError('violates foreign key constraint')  # its folder deleted

        self.assertFalse(self.storage.obj_exists(self.upload_refused(refused)))

    def test_a_storage_error_after_a_failed_upload_is_logged_and_the_row_s_raised(self):
        new = ChrisFile(owner=self.user, parent_folder=self.folder,
                        fname=ContentFile(b'c', name='c.txt'))
        new.fname.name = 'home/moves/uploads/t/c.txt'
        broken = mock.Mock()
        broken.delete_obj.side_effect = RuntimeError('503')

        with mock.patch.object(ChrisFile, '_do_insert', side_effect=DatabaseError('row')), \
                mock.patch('core.models.connect_storage', return_value=broken), \
                self.assertLogs('core.models', 'ERROR'), \
                self.assertRaisesRegex(DatabaseError, 'row'), transaction.atomic():
            new.save()

    def test_a_file_moves_with_its_object(self):
        new_path = 'home/moves/uploads/t/sub/a.txt'

        self.file.move(new_path)

        self.file.refresh_from_db()
        self.assertEqual(self.file.fname.name, new_path)
        self.assertEqual(self.file.parent_folder.path, 'home/moves/uploads/t/sub')
        self.assertEqual(self.storage.download_obj(new_path), b'a')
        self.assertFalse(self.storage.obj_exists('home/moves/uploads/t/a.txt'))

    def test_a_move_to_its_own_path_is_nothing_to_do(self):
        self.file.move('/' + self.file.fname.name)

        self.assertEqual(self.storage.download_obj(self.file.fname.name), b'a')

    def test_a_move_to_a_path_in_use_is_refused(self):
        link, other_link = (ChrisLinkFile(path='home/moves/uploads', owner=self.user,
                                          parent_folder=self.folder) for _ in range(2))
        link.save(name='l')
        other_link.save(name='l2')

        for taken in (self.other.fname.name, link.fname.name, self.folder.path,
                      f'{self.other.fname.name}/x.txt'):  # a folder where a file is
            with self.subTest(taken=taken), self.assertRaisesRegex(ValueError, 'in use'):
                self.file.move(taken)
        with self.assertRaisesRegex(ValueError, 'in use'):
            link.move(other_link.fname.name)

        self.file.refresh_from_db()
        self.assertEqual(self.file.fname.name, 'home/moves/uploads/t/a.txt')
        self.assertEqual(self.storage.download_obj(self.file.fname.name), b'a')
        self.assertEqual(self.storage.download_obj(self.other.fname.name), b'b')

    def test_a_move_whose_row_fails_leaves_the_file_whole(self):
        new_path = 'home/moves/uploads/t/sub/a.txt'

        with mock.patch.object(ChrisFile, '_do_update', side_effect=DatabaseError('row')), \
                self.assertRaises(DatabaseError):
            self.file.move(new_path)

        self.assertEqual(self.file.fname.name, 'home/moves/uploads/t/a.txt')
        self.assertEqual(self.file.parent_folder, self.folder)
        self.assertEqual(self.storage.download_obj(self.file.fname.name), b'a')
        self.assertFalse(self.storage.obj_exists(new_path))

    @staticmethod
    def commit_failing():
        """
        Have the transactions of core.models fail where their commit would, once their
        block ran.
        """
        atomic = transaction.atomic

        @contextmanager
        def failing(*args, **kwargs):
            with atomic(*args, **kwargs):
                yield
                raise DatabaseError('commit')
        return mock.patch('core.models.transaction.atomic', failing)

    def test_a_move_whose_commit_fails_leaves_the_file_whole(self):
        new_path = 'home/moves/uploads/t/sub/a.txt'

        with self.commit_failing(), self.assertRaisesRegex(DatabaseError, 'commit'):
            self.file.move(new_path)

        self.assertEqual((self.file.fname.name, self.file.parent_folder),
                         ('home/moves/uploads/t/a.txt', self.folder))
        self.assertEqual(self.storage.download_obj(self.file.fname.name), b'a')
        self.assertFalse(self.storage.obj_exists(new_path))  # the copy, discarded
        self.file.refresh_from_db()
        self.assertEqual(self.file.fname.name, 'home/moves/uploads/t/a.txt')

    def test_a_folder_moves_with_its_tree_and_takes_its_new_parent(self):
        folder = ChrisFolder.objects.create(path='home/moves/uploads/t/d', owner=self.user)
        inner = self.chris_file('home/moves/uploads/t/d/x.txt', b'x')
        sibling = self.chris_file('home/moves/uploads/t/d0/y.txt', b'y')

        folder.move('home/moves/uploads/t/e/d2')

        folder.refresh_from_db()
        inner.refresh_from_db()
        self.assertEqual((folder.path, folder.parent.path),
                         ('home/moves/uploads/t/e/d2', 'home/moves/uploads/t/e'))
        self.assertEqual(inner.fname.name, 'home/moves/uploads/t/e/d2/x.txt')
        self.assertEqual(self.storage.download_obj(inner.fname.name), b'x')
        self.assertEqual(self.storage.download_obj(sibling.fname.name), b'y')
        self.assertFalse(self.storage.path_exists('home/moves/uploads/t/d'))

    def test_a_folder_without_objects_moves(self):
        folder = ChrisFolder.objects.create(path='home/moves/uploads/t/empty',
                                            owner=self.user)

        folder.move('home/moves/uploads/t/empty2')

        folder.refresh_from_db()
        self.assertEqual(folder.path, 'home/moves/uploads/t/empty2')

    def test_a_folder_move_whose_rows_fail_moves_its_objects_back(self):
        folder = ChrisFolder.objects.create(path='home/moves/uploads/t/d', owner=self.user)
        self.chris_file('home/moves/uploads/t/d/x.txt', b'x')

        with mock.patch.object(ChrisFile.objects, 'bulk_update',
                               side_effect=DatabaseError('rows')), \
                self.assertRaisesRegex(DatabaseError, 'rows'):
            folder.move('home/moves/uploads/t/d2')

        self.assertEqual(folder.path, 'home/moves/uploads/t/d')
        self.assertEqual(self.storage.download_obj('home/moves/uploads/t/d/x.txt'), b'x')
        self.assertFalse(self.storage.path_exists('home/moves/uploads/t/d2'))
        folder.refresh_from_db()
        self.assertEqual(folder.path, 'home/moves/uploads/t/d')

    def test_a_folder_move_to_its_own_path_or_one_in_use_does_nothing_or_is_refused(self):
        folder = ChrisFolder.objects.create(path='home/moves/uploads/t/d', owner=self.user)

        folder.move('home/moves/uploads/t/d/')
        with self.assertRaisesRegex(ValueError, 'in use'):
            folder.move(self.file.fname.name)

        folder.refresh_from_db()
        self.assertEqual(folder.path, 'home/moves/uploads/t/d')

    def test_a_move_that_loses_a_race_for_its_path_changes_nothing(self):
        taken = self.other.fname.name

        with mock.patch('core.models.path_in_use', return_value=False), \
                self.assertRaises(IntegrityError):
            self.file.move(taken)  # the other row took the path after the check

        self.assertEqual(self.storage.download_obj(taken), b'b')
        self.assertEqual(self.storage.download_obj(self.file.fname.name), b'a')
        self.file.refresh_from_db()
        self.assertEqual(self.file.fname.name, 'home/moves/uploads/t/a.txt')

    def test_a_move_whose_old_object_stays_is_done_and_logged(self):
        old_path, new_path = self.file.fname.name, 'home/moves/uploads/t/sub/a.txt'
        storage = mock.Mock(wraps=self.storage)

        def delete_but_the_old_object(path):
            if path == old_path:
                raise RuntimeError('503')
            self.storage.delete_obj(path)
        storage.delete_obj.side_effect = delete_but_the_old_object

        with mock.patch('core.models.connect_storage', return_value=storage), \
                self.assertLogs('core.models', 'ERROR'):
            self.file.move(new_path)

        self.file.refresh_from_db()
        self.assertEqual(self.file.fname.name, new_path)
        self.assertEqual(self.storage.download_obj(new_path), b'a')

    def test_a_public_file_keeps_its_public_link_whether_its_move_fails_or_not(self):
        self.file.grant_public_access()
        self.file.create_public_link()
        old_path, new_path = self.file.fname.name, 'home/moves/uploads/t/sub/a.txt'
        self.addCleanup(lambda: ChrisLinkFile.objects.filter(
            fname__startswith='PUBLIC/home_moves').delete())

        def link_of(path):
            return f"PUBLIC/{path.replace('/', '_')}.chrislink"

        with mock.patch.object(self.storage.__class__, 'copy_obj',
                               side_effect=RuntimeError('503')), \
                self.assertRaisesRegex(RuntimeError, '503'), \
                public_link_follows(self.file, True):
            self.file.move(new_path)
        self.assertTrue(ChrisLinkFile.objects.filter(fname=link_of(old_path)).exists())

        with public_link_follows(self.file, True):
            self.file.move(new_path)
        self.assertFalse(ChrisLinkFile.objects.filter(fname=link_of(old_path)).exists())
        self.assertTrue(ChrisLinkFile.objects.filter(fname=link_of(new_path)).exists())

    def test_a_link_file_moves_writing_its_object_once(self):
        link = ChrisLinkFile(path='home/moves/uploads', owner=self.user,
                             parent_folder=self.folder)
        link.save(name='l')
        old_path, new_path = link.fname.name, 'home/moves/uploads/t/sub/m.chrislink'
        spy = mock.Mock(wraps=self.storage)

        with mock.patch('core.models.connect_storage', return_value=spy):
            link.move(new_path)

        spy.copy_obj.assert_not_called()
        spy.upload_obj.assert_called_once()
        link.refresh_from_db()
        self.assertEqual((link.fname.name, link.parent_folder.path),
                         (new_path, 'home/moves/uploads/t/sub'))
        self.assertEqual(self.storage.download_obj(new_path), b'home/moves/uploads')
        self.assertFalse(self.storage.obj_exists(old_path))

    def test_a_link_file_move_whose_row_fails_leaves_it_whole(self):
        link = ChrisLinkFile(path='home/moves/uploads', owner=self.user,
                             parent_folder=self.folder)
        link.save(name='l')
        old_path, new_path = link.fname.name, 'home/moves/uploads/t/m.chrislink'

        with mock.patch.object(ChrisLinkFile, '_do_update',
                               side_effect=DatabaseError('row')), \
                self.assertRaises(DatabaseError):
            link.move(new_path)

        self.assertEqual((link.fname.name, link.parent_folder), (old_path, self.folder))
        self.assertEqual(self.storage.download_obj(old_path), b'home/moves/uploads')
        self.assertFalse(self.storage.obj_exists(new_path))

    def test_a_link_file_move_whose_commit_fails_leaves_it_whole(self):
        link = ChrisLinkFile(path='home/moves/uploads', owner=self.user,
                             parent_folder=self.folder)
        link.save(name='l')
        old_path, new_path = link.fname.name, 'home/moves/uploads/t/m.chrislink'

        with self.commit_failing(), self.assertRaisesRegex(DatabaseError, 'commit'):
            link.move(new_path)

        self.assertEqual((link.fname.name, link.parent_folder), (old_path, self.folder))
        self.assertEqual(self.storage.download_obj(old_path), b'home/moves/uploads')
        self.assertFalse(self.storage.obj_exists(new_path))  # written, then discarded


class UserCanAccessObjTests(ModelTests):
    """
    Tests for the canonical ``user_can_access_obj`` read-access rule.
    """

    def test_owner_chris_public_and_permission_rule(self):
        """
        Test the owner/superuser/public/permission read-access rule.
        """
        user = User.objects.get(username=self.username)
        chris = User.objects.get(username='chris')
        other = User.objects.create_user(username='other3', password='o-pass')

        folder, _ = ChrisFolder.objects.get_or_create(
            path='home/other3/uploads', owner=other)

        # owner -> allowed
        self.assertTrue(user_can_access_obj(folder, other))
        # superuser 'chris' -> allowed
        self.assertTrue(user_can_access_obj(folder, chris))
        # unrelated user, not public, no permission -> denied
        self.assertFalse(user_can_access_obj(folder, user))

        # public -> allowed
        folder.public = True
        folder.save()
        self.assertTrue(user_can_access_obj(folder, user))

        # explicitly shared with the user -> allowed
        folder.public = False
        folder.save()
        folder.grant_user_permission(user, 'r')
        self.assertTrue(user_can_access_obj(folder, user))


class ValidatePathAccessTests(ModelTests):
    """
    Tests for the centralized ``validate_path_access`` path-access
    authorization function.
    """

    def test_rejects_structural_paths_with_exact_messages(self):
        """
        Test whether validate_path_access rejects structurally invalid paths and
        that the exact, distinct error messages are preserved.
        """
        user = User.objects.get(username=self.username)

        cases = [
            ('home',
             "This field may not reference a top-level folder path 'home'."),
            ('NOTAROOT/x',
             "This field may not reference an invalid path 'NOTAROOT/x'."),
            (f'home/{self.username}',
             f"This field may not reference a home folder path "
             f"'home/{self.username}'."),
            (f'home/{self.username}/feeds',
             f"This field may not reference a home's feeds folder path "
             f"'home/{self.username}/feeds'."),
        ]
        for path, expected_msg in cases:
            with self.assertRaises(PathAccessError) as cm:
                validate_path_access(user, path)
            self.assertEqual(str(cm.exception), expected_msg)

    def test_rejects_nonexistent_path_with_exact_message(self):
        """
        Test whether validate_path_access rejects a structurally valid path that
        does not resolve to any folder/file/link file.
        """
        user = User.objects.get(username=self.username)
        with self.assertRaises(PathAccessError) as cm:
            validate_path_access(user, 'home/someone/uploads')
        self.assertEqual(
            str(cm.exception),
            "This field may not reference an invalid path 'home/someone/uploads'.")

    def test_folder_owner_public_and_permission_rule(self):
        """
        Test the owner/public/permission access rule and that a normalized path
        is returned on success.
        """
        user = User.objects.get(username=self.username)
        own_folder, _ = ChrisFolder.objects.get_or_create(
            path=f'home/{self.username}/uploads', owner=user)

        # owner -> allowed; trailing slash is normalized away in the return value
        self.assertEqual(validate_path_access(user, own_folder.path + '/'),
                         own_folder.path)

        other = User.objects.create_user(username='other', password='other-pass')
        other_folder, _ = ChrisFolder.objects.get_or_create(
            path='home/other/uploads', owner=other)

        # not owner / not public / no permission -> denied (permission message)
        with self.assertRaises(PathAccessError) as cm:
            validate_path_access(user, other_folder.path)
        self.assertEqual(
            str(cm.exception),
            "User does not have permission to access path 'home/other/uploads'.")

        # public -> allowed
        other_folder.public = True
        other_folder.save()
        self.assertEqual(validate_path_access(user, other_folder.path),
                         other_folder.path)

        # explicitly shared with the user -> allowed
        other_folder.public = False
        other_folder.save()
        other_folder.grant_user_permission(user, 'r')
        self.assertEqual(validate_path_access(user, other_folder.path),
                         other_folder.path)

    def test_chris_superuser_bypass(self):
        """
        Test whether the 'chris' superuser is allowed to access any path.
        """
        chris = User.objects.get(username='chris')
        other = User.objects.create_user(username='other2', password='o-pass')
        other_folder, _ = ChrisFolder.objects.get_or_create(
            path='home/other2/uploads', owner=other)
        self.assertEqual(validate_path_access(chris, other_folder.path),
                         other_folder.path)


@tag('integration')
class ValidatePathAccessStorageTests(ModelTests):
    """
    Storage-backed tests for ``validate_path_access`` covering the ChrisFile and
    ChrisLinkFile resolution branches and the PUBLIC/SHARED link-target rule.
    """

    def setUp(self):
        super(ValidatePathAccessStorageTests, self).setUp()
        self.storage_manager = connect_storage(settings)

    def test_resolves_file_and_link_targets(self):
        user = User.objects.get(username=self.username)
        folder, _ = ChrisFolder.objects.get_or_create(
            path=f'home/{self.username}/uploads', owner=user)

        # ChrisFile branch -> owned by user -> allowed
        fpath = f'home/{self.username}/uploads/f.txt'
        with io.StringIO('x') as f:
            self.storage_manager.upload_obj(fpath, f.read(),
                                            content_type='text/plain')
        uf = UserFile(owner=user, parent_folder=folder)
        uf.fname.name = fpath
        uf.save()
        self.assertEqual(validate_path_access(user, fpath), fpath)

        # ChrisLinkFile branch, target not PUBLIC/SHARED -> allowed
        lf = ChrisLinkFile(path=f'home/{self.username}/uploads', owner=user,
                           parent_folder=folder)
        lf.save(name='mylink')
        self.assertEqual(validate_path_access(user, lf.fname.name),
                         lf.fname.name)

        # ChrisLinkFile whose target is PUBLIC -> rejected even though owned
        lf2 = ChrisLinkFile(path='PUBLIC', owner=user, parent_folder=folder)
        lf2.save(name='publink')
        with self.assertRaises(PathAccessError) as cm:
            validate_path_access(user, lf2.fname.name)
        self.assertEqual(
            str(cm.exception),
            f"This field may not reference an invalid path '{lf2.fname.name}'.")

        # delete files from storage
        self.storage_manager.delete_path(f'home/{self.username}/uploads')


class DatabaseSettingsTests(TestCase):
    """
    The table storage parameters migration core.0004 sets, and (tagged 'compose') how the
    compose stacks run the database server: exclude those with --exclude-tag compose when
    testing against any other PostgreSQL.
    """

    TABLES = (ChrisFile._meta.db_table, ChrisFolder._meta.db_table)

    AUTOVACUUM_OPTIONS = {'autovacuum_vacuum_scale_factor=0.02',
                          'autovacuum_analyze_scale_factor=0.01'}

    def _autovacuum_options(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT relname, reloptions FROM pg_class "
                           "WHERE relname = ANY(%s) AND relkind = 'r'", [list(self.TABLES)])

            return {table: {option for option in (options or [])
                            if option.startswith('autovacuum_')}
                    for table, options in cursor.fetchall()}

    def _migrate_core(self, name):
        MigrationExecutor(connection).migrate([('core', name)])

    def test_file_and_folder_tables_vacuum_and_analyze_sooner(self):
        """
        Test whether the file and folder tables carry the autovacuum storage parameters
        that migration core.0004 sets.
        """
        self.assertEqual(self._autovacuum_options(),
                         {table: self.AUTOVACUUM_OPTIONS for table in self.TABLES})

    def test_autovacuum_migration_reverses_and_reapplies(self):
        """
        Test whether migration core.0004 can be reversed, which resets the storage
        parameters, and applied again. PostgreSQL's DDL is transactional, so the test's
        transaction rolls both back.
        """
        try:
            self._migrate_core('0003_chrisfolder_deletion_error_and_more')
            self.assertEqual(self._autovacuum_options(),
                             {table: set() for table in self.TABLES})
        finally:
            self._migrate_core('0004_chrisfile_chrisfolder_autovacuum')

        self.assertEqual(self._autovacuum_options(),
                         {table: self.AUTOVACUUM_OPTIONS for table in self.TABLES})

    @tag('compose')
    def test_server_time_zone_is_the_one_django_connects_with(self):
        """
        Test whether the database server's default time zone (postgres/cube.conf) is the
        one Django's connections use, so Django need not set it on every new connection.
        """
        with connection.cursor() as cursor:
            cursor.execute("SELECT reset_val FROM pg_settings WHERE name = 'TimeZone'")
            server_default = cursor.fetchone()[0]

        self.assertEqual(server_default, connection.timezone_name)

    @tag('compose')
    def test_server_reads_postgres_cube_conf(self):
        """
        Test whether the database server runs on postgres/cube.conf, as the compose
        stacks start it.
        """
        with connection.cursor() as cursor:
            cursor.execute('SHOW config_file')
            self.assertEqual(cursor.fetchone()[0], '/etc/postgresql/cube.conf')
