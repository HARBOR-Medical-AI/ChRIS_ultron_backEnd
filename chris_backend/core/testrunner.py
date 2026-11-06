"""
Custom Django test runner that terminates open database connections before
dropping the test database. This prevents the "database is being accessed by
other users" error that occurs when Celery workers (started inside TransactionTestCase
setUpClass) still hold connections when the test suite tries to tear down.
"""

from django.db import connection
from django.test.runner import DiscoverRunner


class TerminateConnectionsTestRunner(DiscoverRunner):

    def teardown_databases(self, old_config, **kwargs):
        # Terminate all other connections to the test database before dropping it
        # so that Celery worker connections don't block the DROP DATABASE command.
        # old_config is empty when no selected test needed a database: no test database
        # was created but the connection still names the development database whose
        # sessions belong to the running stack.
        if old_config:
            test_db_name = connection.settings_dict['NAME']
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    [test_db_name],
                )
        super().teardown_databases(old_config, **kwargs)
