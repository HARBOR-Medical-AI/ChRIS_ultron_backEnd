from unittest import mock

from django.test import SimpleTestCase

from core.testrunner import TerminateConnectionsTestRunner


@mock.patch('django.test.runner.DiscoverRunner.teardown_databases')
@mock.patch('core.testrunner.connection')
class TerminateConnectionsTestRunnerTests(SimpleTestCase):
    """
    Test which sessions the test runner terminates before dropping the test database.
    """

    def test_terminates_nothing_when_no_test_database_was_created(self, connection,
                                                                 teardown):
        TerminateConnectionsTestRunner().teardown_databases([])

        connection.cursor.assert_not_called()
        teardown.assert_called_once_with([])

    def test_terminates_the_other_sessions_on_the_test_database_before_dropping_it(
            self, connection, teardown):
        connection.settings_dict = {'NAME': 'test_chris_dev'}
        old_config = [(mock.Mock(), 'chris_dev', True)]
        cursor = connection.cursor.return_value.__enter__.return_value
        order = []
        cursor.execute.side_effect = lambda *args: order.append('terminate')
        teardown.side_effect = lambda *args, **kwargs: order.append('drop')

        TerminateConnectionsTestRunner().teardown_databases(old_config)

        sql, params = cursor.execute.call_args.args
        self.assertRegex(' '.join(sql.split()),
                         r'pg_terminate_backend\(pid\) FROM pg_stat_activity '
                         r'WHERE datname = %s AND pid <> pg_backend_pid\(\)$')
        self.assertEqual(params, ['test_chris_dev'])
        self.assertEqual(order, ['terminate', 'drop'])
        teardown.assert_called_once_with(old_config)
