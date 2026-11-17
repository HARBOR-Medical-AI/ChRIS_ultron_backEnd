"""
The Celery queues are named in several places that must agree, which no Django test sees
(the compose files are outside the image): chris_backend/core/celery.py (QUEUES and the
routing table), the worker services of the compose stacks, which must consume every queue
and never the tests' own, oxidicom's queue, and the queues the benchmark harness samples
(benchmarks/broker.py). A queue renamed in one place alone strands its tasks silently.
Run in the dev image by ``just lint-test``.
"""

import ast
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

#: the queue the test runner keeps for the tests (core/testrunner.py)
TEST_QUEUE = 'celery'

#: a Celery worker's queues on its command line
QUEUES_OPTION = re.compile(r'(?:-Q|--queues)[ =]([\w,]+)')


def module_values(path):
    """
    The string constants and the tuples and dicts of them (or of names bound to them)
    that the Python module at ``path`` assigns at its top level, by name.
    """
    values = {}

    def value(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return values.get(node.id)
        if isinstance(node, ast.Tuple):
            return tuple(value(element) for element in node.elts)
        if isinstance(node, ast.Dict):
            return {value(key): value(item) for key, item in zip(node.keys, node.values)}
        return None

    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            values[node.targets[0].id] = value(node.value)
    return values


def worker_queues(services):
    """
    The queues each Celery worker service of ``services`` (a compose file's) consumes,
    by service.
    """
    workers = {}

    for name, service in services.items():
        command = (service or {}).get('command') or ''
        if isinstance(command, list):
            command = ' '.join(command)

        if 'celery' in command and ' worker' in command:
            match = QUEUES_OPTION.search(command)
            workers[name] = set(match.group(1).split(',')) if match else {TEST_QUEUE}
    return workers


def compose_services(*files):
    """
    The services of the compose files ``files`` merged as compose merges them, as far as
    their commands and environments go: a later file's replace an earlier one's.
    """
    services = {}

    for name in files:
        for service, definition in yaml.safe_load((ROOT / name).read_text())[
                'services'].items():
            merged = services.setdefault(service, {})
            merged.update({key: value for key, value in (definition or {}).items()
                           if key in ('command', 'environment')})
    return services


class QueueTests(unittest.TestCase):

    def setUp(self):
        celery = module_values(ROOT / 'chris_backend' / 'core' / 'celery.py')
        self.queues = set(celery['QUEUES'])
        self.task_queues = celery['TASK_QUEUES']

    def test_every_queue_has_a_consumer_and_none_consumes_the_tests_queue(self):
        stacks = {'dev': ('docker-compose.yml',),
                  'benchmark': ('docker-compose.yml', 'docker-compose.benchmark.yml')}

        for stack, files in stacks.items():
            with self.subTest(stack=stack):
                workers = worker_queues(compose_services(*files))
                consumed = set().union(*workers.values())

                self.assertEqual(consumed, self.queues)
                self.assertNotIn(TEST_QUEUE, consumed)

    def test_every_task_is_routed_to_a_queue(self):
        self.assertTrue(self.task_queues)
        self.assertLessEqual(set(self.task_queues.values()), self.queues)

    def test_oxidicom_publishes_to_the_queue_of_the_series_registration(self):
        environment = compose_services('docker-compose.yml')['oxidicom']['environment']

        self.assertEqual(environment['OXIDICOM_QUEUE_NAME'],
                         self.task_queues['pacsfiles.tasks.register_pacs_series'])

    def test_the_benchmark_samples_every_queue(self):
        broker = module_values(ROOT / 'benchmarks' / 'broker.py')

        self.assertEqual(set(broker['CELERY_QUEUES']), self.queues)


if __name__ == '__main__':
    unittest.main()
