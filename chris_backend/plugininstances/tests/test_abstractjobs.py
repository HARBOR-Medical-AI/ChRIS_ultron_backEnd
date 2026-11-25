"""
Tests for what every job of a plugin instance shares (plugininstances.services.abstractjobs):
the check that the previous instance's output, the input of a 'ds' plugin's job, is all in
storage before a job sends it to the remote.
"""

from unittest import mock

from django.conf import settings
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from core.models import ChrisFolder, ChrisLinkFile
from core.storage import StorageObject
from core.storage.helpers import mock_storage
from plugininstances.services import abstractjobs
from plugininstances.services.copyjobs import PluginInstanceCopyJob
from plugininstances.services.pluginjobs import PluginInstanceAppJob
from plugininstances.tests.test_transitions import PluginInstanceFixture
from userfiles.models import UserFile


class Clock:
    """
    A monotonic clock that only a sleep moves on, and the sleeps it saw.
    """

    def __init__(self):
        self.now, self.sleeps = 1000.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class PreviousOutputPathTests(PluginInstanceFixture, TestCase):

    def setUp(self):
        super().setUp()
        self.parent = self.instance('finishedSuccessfully')
        self.child = self.instance('copying', previous=self.parent)
        self.output = self.parent.get_output_path()
        self.listed = []  # what each listing walked

    def register(self, files=(), links=()):
        """
        Rows for ``files`` and link files named ``links`` in the parent's output.
        """
        folder = self.parent.output_folder
        for name in files:
            UserFile.objects.create(fname=f'{self.output}/{name}', owner=self.user,
                                    parent_folder=folder)
        ChrisLinkFile.objects.bulk_create(  # rows only: their save writes storage too
            ChrisLinkFile(fname=f'{self.output}/{name}.chrislink', path='home/foo',
                          owner=self.user, parent_folder=folder) for name in links)

    def job(self, storage_env, *listings, job_class=PluginInstanceCopyJob):
        """
        A job of the child on ``storage_env`` whose listings of the parent's output find
        ``listings[i]`` objects (the last one from then on), or raise it.
        """
        job = job_class(self.child)
        job.storage_env = storage_env
        job.storage_manager = mock.Mock()
        left = list(listings)

        def walk(folder):
            self.assertEqual(folder, self.output)
            found = left.pop(0) if len(left) > 1 else left[0]
            if isinstance(found, Exception):
                raise found
            self.listed.append(0)
            for i in range(found):
                self.listed[-1] += 1
                yield StorageObject(f'{folder}/{i}', 1)

        job.storage_manager.walk.side_effect = walk
        return job

    def test_a_filesystem_is_not_checked(self):
        self.register(files=['a'])

        for storage_env in ('fslink', 'filesystem'):
            job = self.job(storage_env, 0)
            with self.subTest(storage_env=storage_env), \
                    CaptureQueriesContext(connection) as queries:
                self.assertEqual(job.previous_output_path(), self.output)

            job.storage_manager.walk.assert_not_called()
            self.assertFalse([q for q in queries if '"core_chrisfile"' in q['sql']
                              or '"core_chrislinkfile"' in q['sql']])

    def test_object_storage_lists_until_it_finds_the_files_and_link_files(self):
        self.register(files=['a', 'sub/b'], links=['l'])
        # a sibling's file, whose path starts with the output's, is not counted
        sibling, _ = ChrisFolder.objects.get_or_create(path=self.output + '_2',
                                                       defaults={'owner': self.user})
        UserFile.objects.create(fname=f'{self.output}_2/c', owner=self.user,
                                parent_folder=sibling)
        job = self.job('swift', 2, 2, 5)
        clock = Clock()

        with mock.patch.object(abstractjobs, 'time', clock), \
                CaptureQueriesContext(connection) as queries:
            self.assertEqual(job.previous_output_path(), self.output)

        self.assertEqual(clock.sleeps, [1, 2])
        self.assertEqual(self.listed, [2, 2, 3])  # the last one stops at the count
        counts = [q['sql'] for q in queries if 'COUNT(*)' in q['sql']]
        self.assertEqual(len(counts), 2)
        self.assertFalse([sql for sql in counts if 'ORDER BY' in sql])

    def test_a_listing_that_never_finds_them_all_gives_up_with_code11(self):
        self.register(files=['a', 'b'])
        job = self.job('s3', 1)
        clock = Clock()

        with mock.patch.object(abstractjobs, 'time', clock), \
                self.assertLogs('plugininstances.services', 'ERROR') as logs, \
                self.assertRaises(NameError):
            job.previous_output_path()

        self.assertEqual(clock.sleeps, [1, 2, 4, 8, 8, 8, 8, 8, 8])  # 55 s of 60
        self.assertIn('[CODE11,', logs.output[-1])
        self.assertEqual(job.c_plugin_inst.error_code, 'CODE11')

    def test_a_listing_that_fails_is_made_again(self):
        self.register(files=['a'])
        job = self.job('swift', RuntimeError('503'), 1)
        clock = Clock()

        with mock.patch.object(abstractjobs, 'time', clock), \
                self.assertLogs('plugininstances.services', 'ERROR') as logs:
            self.assertEqual(job.previous_output_path(), self.output)

        self.assertIn('[CODE06,', logs.output[0])
        self.assertEqual((clock.sleeps, self.listed), ([1], [1]))
        self.assertEqual(job.c_plugin_inst.error_code, '')

    def test_a_slow_first_listing_still_has_its_tries(self):
        self.register(files=['a', 'b'])
        job = self.job('swift', 1, 1, 2)
        clock = Clock()
        walk = job.storage_manager.walk.side_effect

        def slow_at_first(folder):
            if not self.listed:
                clock.now += 70  # the first listing takes longer than the deadline
            return walk(folder)

        job.storage_manager.walk.side_effect = slow_at_first

        with mock.patch.object(abstractjobs, 'time', clock):
            self.assertEqual(job.previous_output_path(), self.output)

        self.assertEqual((clock.sleeps, self.listed), ([1, 2], [1, 1, 2]))

    def test_names_with_commas_are_not_counted(self):
        self.register(files=['a,b', 'c'])  # a listing may still show a name renamed
        job = self.job('swift', 0)
        job.storage_manager.walk.side_effect = lambda folder: iter([
            StorageObject(f'{folder}/a,b', 1), StorageObject(f'{folder}/c', 1),
            StorageObject(f'{folder}/ab', 1)])

        self.assertTrue(job._lists_at_least(self.output, 2))
        job.storage_manager.walk.side_effect = lambda folder: iter([
            StorageObject(f'{folder}/a,b', 1), StorageObject(f'{folder}/c', 1)])
        self.assertFalse(job._lists_at_least(self.output, 2))

    def test_listings_are_tried_for_a_minute_on_any_storage(self):
        # a network filesystem's caches can lag too
        for storage_env in ('fslink', 'filesystem', 'swift', 's3'):
            job = self.job(storage_env, 0)
            clock = Clock()
            with self.subTest(storage_env=storage_env), \
                    mock.patch.object(abstractjobs, 'time', clock):
                self.assertEqual(len(list(job.listing_tries())), 10)
                self.assertEqual(sum(clock.sleeps), 55)

    def test_an_output_without_files_needs_no_listing(self):
        job = self.job('swift', 0)

        self.assertEqual(job.previous_output_path(), self.output)
        job.storage_manager.walk.assert_not_called()


class PreviousOutputCheckedOnceTests(PluginInstanceFixture, TestCase):
    """
    The jobs that send the previous instance's output to the remote check it once: the
    copy job when the remote needs one, else the plugin job.
    """

    def submit(self, job_class, status, requires_copy_job):
        parent = self.instance('finishedSuccessfully')
        child = self.instance(status, previous=parent)
        job = job_class(child)
        job.pfcon_client.requires_copy_job = requires_copy_job
        job.pfcon_client.pfcon_innetwork = True
        job._submit = mock.Mock(return_value={'compute': {'status': 'notStarted',
                                                          'logs': ''}})

        with mock_storage(settings), \
                mock.patch.object(job_class, 'previous_output_path', autospec=True,
                                  return_value=parent.get_output_path()) as previous:
            job.run()

        job._submit.assert_called_once()
        return previous.call_args_list

    def test_the_copy_job_checks_and_the_plugin_job_after_it_does_not(self):
        self.assertEqual(self.submit(PluginInstanceCopyJob, 'copying', True),
                         [mock.call(mock.ANY)])
        self.assertEqual(self.submit(PluginInstanceAppJob, 'scheduled', True),
                         [mock.call(mock.ANY, check=False)])

    def test_the_plugin_job_checks_when_no_copy_job_ran(self):
        self.assertEqual(self.submit(PluginInstanceAppJob, 'scheduled', False),
                         [mock.call(mock.ANY, check=True)])
