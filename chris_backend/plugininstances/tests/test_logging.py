"""
Tests for what the job classes log (HARBOR-planning #152): one line per registration
rather than one per file, the line that says how each instance ended, pfcon's bodies and
the job descriptions at DEBUG without the owner's token, and no log file in the benchmark
settings.
"""

import json
import logging
import os
import subprocess  # nosec B404 - runs this interpreter on a literal program
import sys
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from pfconclient.client import JobType
from rest_framework.authtoken.models import Token

from core.storage import StorageObject
from plugininstances import tasks
from plugininstances.models import PluginInstance
from plugininstances.services import pluginjobs
from plugininstances.services.abstractjobs import LogJson
from plugininstances.services.deletejobs import PluginInstanceDeleteJob
from plugininstances.tests.test_transitions import PluginInstanceFixture


class LogJsonTests(SimpleTestCase):

    def test_a_job_description_shows_no_token(self):
        description = {'image': 'pl-app', 'env': ['CHRIS_JID=chris-jid-1',
                                                  'CHRIS_USER_TOKEN=abc123',
                                                  'ANY_TOKEN_HERE=x', 'TOKENLESS=1']}

        shown = json.loads(str(LogJson(description)))

        self.assertEqual(shown['env'], ['CHRIS_JID=chris-jid-1', 'CHRIS_USER_TOKEN=********',
                                        'ANY_TOKEN_HERE=********', 'TOKENLESS=1'])
        self.assertEqual(description['env'][1], 'CHRIS_USER_TOKEN=abc123')  # untouched

    def test_a_response_shows_no_token_its_job_printed(self):
        response = {'compute': {'status': 'started', 'logs': (
            'starting\nCHRIS_USER_TOKEN=abc123\nexport GH_TOKEN=x y\nMY_TOKENS=1\ndone')}}

        shown = json.loads(str(LogJson(response)))

        self.assertEqual(shown['compute']['logs'],
                         'starting\nCHRIS_USER_TOKEN=********\nexport GH_TOKEN=******** y\n'
                         'MY_TOKENS=1\ndone')
        self.assertIn('abc123', response['compute']['logs'])  # untouched

    def test_only_a_variable_with_the_word_token_in_its_name_is_masked(self):
        logs = ('TOKEN=a\nexport A__TOKEN=b\nXTOKENX=c\nMY_TOKENS=d\ntoken=e\n'
                'URL=x?TOKEN=f\nA_TOKEN_B=g')

        shown = json.loads(str(LogJson({'logs': logs})))['logs']

        self.assertEqual(shown, 'TOKEN=********\nexport A__TOKEN=********\nXTOKENX=c\n'
                                'MY_TOKENS=d\ntoken=e\nURL=x?TOKEN=********\n'
                                'A_TOKEN_B=********')

    def test_it_is_formatted_only_when_the_line_is_emitted(self):
        data = mock.MagicMock()
        logger = logging.getLogger('plugininstances.tests.test_logging')
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.setLevel, logging.NOTSET)

        with mock.patch.object(json, 'dumps') as dumps:
            logger.debug('response: %s', LogJson(data))

        dumps.assert_not_called()
        self.assertEqual(str(LogJson({'compute': {'status': 'started'}})),
                         '{"compute": {"status": "started"}}')


class RegistrationLogTests(PluginInstanceFixture, TestCase):

    def setUp(self):
        super().setUp()
        logging.disable(logging.NOTSET)  # the fixture disables WARNING and below

    def test_a_registration_logs_one_line_whatever_its_files(self):
        inst = self.instance('registeringFiles')
        job = pluginjobs.PluginInstanceAppJob(inst)
        output = inst.get_output_path()
        names = [f'f{n}.txt' for n in range(5)]
        job._get_status = mock.Mock(return_value={'compute': {'status': 'finishedSuccessfully'}})
        job._get_job_json_data = mock.Mock(
            return_value={'job_output_path': output, 'rel_file_paths': names})
        job.storage_manager = mock.Mock()  # objects of 1 byte each
        job.storage_manager.walk.return_value = iter(
            [StorageObject(f'{output}/{name}', 1) for name in names])
        job.enqueue_run = mock.Mock()

        with self.assertLogs('plugininstances', 'INFO') as logs:
            job.register_output()

        registered = [line for line in logs.output if 'Registered' in line]
        self.assertEqual(len(registered), 1)
        self.assertIn('Registered 5 output files and 0 link files (5 bytes)', registered[0])
        self.assertIn('it ends finishedSuccessfully', registered[0])
        self.assertFalse([line for line in logs.output if 'Registering file' in line])
        self.assertIn(f"Saved plugin instance final status for job {job.str_job_id} as "
                      f"'finishedSuccessfully'", '\n'.join(logs.output))

    def test_a_registration_that_failed_counts_no_file_it_did_not_register(self):
        inst = self.instance('registeringFiles')
        job = pluginjobs.PluginInstanceAppJob(inst)
        job._get_status = mock.Mock(return_value={'compute': {'status': 'finishedSuccessfully'}})
        job._get_job_json_data = mock.Mock(return_value={})
        job.enqueue_run = mock.Mock()

        def listed_then_failed(content):
            for n in range(5):
                job._expect(f'f{n}')
            job.c_plugin_inst.error_code = 'CODE16'  # the step logs its own error
            raise ValueError('Received another output path')

        job.expect_json_files = mock.Mock(side_effect=listed_then_failed)

        with self.assertLogs('plugininstances', 'INFO') as logs:
            job.register_output()

        registered = [line for line in logs.output if 'Registered' in line]
        self.assertIn('Registered 0 output files and 0 link files', registered[0])
        self.assertIn('it ends cancelled', registered[0])
        self.assertFalse([line for line in logs.output if 'Error while registering' in line])

    def test_a_failure_no_step_reported_is_logged_with_its_traceback(self):
        for remote_status, final in (('finishedSuccessfully', 'cancelled'),
                                     ('finishedWithError', 'finishedWithError')):
            with self.subTest(remote_status=remote_status):
                inst = self.instance('registeringFiles')
                job = pluginjobs.PluginInstanceAppJob(inst)
                job._get_status = mock.Mock(return_value={
                    'compute': {'status': remote_status}})
                job._get_job_json_data = mock.Mock(return_value={})
                job.enqueue_run = mock.Mock()
                job.expect_json_files = mock.Mock(side_effect=RuntimeError('database gone'))

                with self.assertLogs('plugininstances', 'INFO') as logs:
                    job.register_output()

                errors = [line for line in logs.output if line.startswith('ERROR')]
                self.assertEqual(len(errors), 1)
                self.assertIn(f'Error while registering the output files of job '
                              f'{job.str_job_id}', errors[0])
                self.assertIn('RuntimeError: database gone', errors[0])
                self.assertEqual(PluginInstance.objects.get(pk=inst.pk).status, final)

    def test_the_line_of_a_registration_a_cancel_overtook_says_so(self):
        inst = self.instance('registeringFiles')
        job = pluginjobs.PluginInstanceAppJob(inst)
        job._get_status = mock.Mock(return_value={'compute': {'status': 'finishedSuccessfully'}})
        job._get_job_json_data = mock.Mock(return_value={})
        job.enqueue_run = mock.Mock()

        def cancelled_meanwhile(content):
            PluginInstance.objects.get(pk=inst.pk).transition('cancelled',
                                                              'registeringFiles')

        job.expect_json_files = mock.Mock(side_effect=cancelled_meanwhile)

        with self.assertLogs('plugininstances.services', 'INFO') as logs:
            job.register_output()

        registered = [line for line in logs.output if 'Registered' in line]
        self.assertIn('it is cancelled already, which stays', registered[0])
        self.assertFalse([line for line in logs.output if 'final status' in line])

    def test_a_job_that_ends_its_instance_logs_the_status_it_saved(self):
        inst = self.instance('started')
        job = pluginjobs.PluginInstanceAppJob(inst)
        job.enqueue_run = mock.Mock()

        with self.assertLogs('plugininstances', 'INFO') as logs:
            job.cancel_exec('CODE13', 'started')
            job.cancel_exec('CODE13', 'started')  # ended already: saves nothing

        final = [line for line in logs.output if 'final status' in line]
        self.assertEqual(len(final), 1)
        self.assertIn(f"job {job.str_job_id} as 'cancelled' with error code CODE13",
                      final[0])

    def test_the_status_check_of_an_ended_job_says_which_task_saves_the_status(self):
        inst = self.instance('started')
        job = pluginjobs.PluginInstanceAppJob(inst)
        job._get_status = mock.Mock(return_value={
            'compute': {'status': 'finishedSuccessfully', 'logs': ''}})

        with mock.patch.object(tasks.register_plugin_instance_output, 'delay') as delay, \
                self.assertLogs('plugininstances.services', 'INFO') as logs:
            job.check_exec_status()

        delay.assert_called_once_with(inst.pk)
        self.assertIn(f'Queued the registration of the output files of job '
                      f'{job.str_job_id}', logs.output[-1])

    def test_the_end_of_the_remote_cleanup_says_the_status_the_database_has(self):
        inst = self.instance('finishedSuccessfully')
        PluginInstance.objects.filter(pk=inst.pk).update(
            remote_cleanup_status='deletingContainers')
        job = PluginInstanceDeleteJob(PluginInstance.objects.get(pk=inst.pk))
        # the tree it is in is cancelled after the cleanup's task read it
        PluginInstance.objects.get(pk=inst.pk).transition('cancelled',
                                                          'finishedSuccessfully')

        with mock.patch.object(PluginInstanceDeleteJob, '_plugin_job_active',
                               return_value=True), \
                self.assertLogs('plugininstances.services', 'INFO') as logs:
            self.assertTrue(job.complete('complete'))

        self.assertIn(f"Remote cleanup for job {job.str_job_id} ended as complete; the "
                      f"plugin instance's final status is 'cancelled'", logs.output[-1])

    def test_submitting_a_job_never_logs_the_owner_s_token(self):
        inst = self.instance('scheduled')
        # the token the job's environment gives it (CHRIS_USER_TOKEN)
        token = Token.objects.get_or_create(user=self.user)[0].key
        job = pluginjobs.PluginInstanceAppJob(inst)
        job.pfcon_client.submit_job = mock.Mock(return_value={
            'compute': {'status': 'started', 'logs': ''}})
        job.get_cmd_args = mock.Mock(return_value=[])
        job.enqueue_run = mock.Mock()

        with self.assertLogs('plugininstances', 'DEBUG') as logs:
            job.run()

        self.assertIn(f'CHRIS_USER_TOKEN={token}',
                      job.pfcon_client.submit_job.call_args.args[2]['env'])

        description = [r.getMessage() for r in logs.records
                       if r.getMessage().startswith('Description of plugin job')]
        self.assertEqual(len(description), 1)
        self.assertIn('CHRIS_USER_TOKEN=********', description[0])
        self.assertFalse([r for r in logs.records if token in r.getMessage()])

    def test_every_pfcon_response_is_logged_at_debug_by_the_request_that_got_it(self):
        inst = self.instance('started')
        job = pluginjobs.PluginInstanceAppJob(inst)
        response = {'compute': {'status': 'started', 'logs': 'CHRIS_USER_TOKEN=abc123'}}
        job.pfcon_client.get_job_status = mock.Mock(return_value=response)

        with self.assertLogs('plugininstances', 'DEBUG') as logs:
            self.assertEqual(job.job_exists(JobType.PLUGIN), response)

        self.assertEqual(logs.output, [
            f'DEBUG:plugininstances.services.abstractjobs:pfcon status response for '
            f'plugin job {job.str_job_id}: {{"compute": {{"status": "started", "logs": '
            f'"CHRIS_USER_TOKEN=********"}}}}'])


class BenchmarkSettingsLogTests(SimpleTestCase):

    PROGRAM = """
import json
import django

django.setup()

from django.conf import settings

print(json.dumps({'handlers': sorted(settings.LOGGING['handlers']),
                  'levels': sorted({c['level'] for n, c in settings.LOGGING['loggers'].items()
                                    if n})}))
"""

    def test_the_benchmark_logs_the_apps_at_info_and_writes_no_file(self):
        env = {**os.environ, 'DJANGO_SETTINGS_MODULE': 'config.settings.benchmark'}
        process = subprocess.run(  # nosec B603 nosemgrep - a literal program
            [sys.executable, '-c', self.PROGRAM], env=env, text=True, capture_output=True,
            cwd=settings.BASE_DIR, timeout=60, check=False)

        self.assertEqual(process.returncode, 0, process.stderr)
        shown = json.loads(process.stdout.splitlines()[-1])
        self.assertNotIn('file', shown['handlers'])
        self.assertEqual(shown['levels'], ['INFO'])
