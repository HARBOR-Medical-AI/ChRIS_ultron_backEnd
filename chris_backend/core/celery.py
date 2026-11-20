
import os
import time
from logging.config import dictConfig
from django.conf import settings

from celery import Celery
from celery.signals import before_task_publish, setup_logging

# set the default Django settings module for the 'celery' program
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.local')

app = Celery('core')

# using a string here means the worker doesn't have to serialize
# the configuration object to child processes
# - namespace='CELERY' means all celery-related configuration keys
#   should have a `CELERY_` prefix
app.config_from_object('django.conf:settings', namespace='CELERY')

# load task modules from all registered Django app configs.
app.autodiscover_tasks()


# Note: django settings cannot be used here for not-understood module dependency resasons.
POLL_INTERVAL = float(os.getenv('CUBE_CELERY_POLL_INTERVAL', '5.0'))
"""
How often to poll for plugin instance status changes.
"""

# One queue per class of task so that a backlog of one class never queues the others'
# tasks behind it; a worker that consumes several queues still shares its slots among
# them. Which worker consumes which queues is the deployment's choice (see
# docker-compose.yml). The default 'celery' queue is exclusively used for the automated
# tests (core/testrunner.py).
SUBMIT = 'submit'        # job submissions to pfcon
STATUS = 'status'        # job status checks and cancels: short, in a queue of their own
REGISTER = 'register'    # output registration: every one starts here, the short end here
REGISTER_LARGE = 'register_large'  # the registrations of more files: minutes
DELETE = 'delete'        # feed, folder, plugin instance and PACS series deletion: minutes to hours
CLEANUP = 'cleanup'      # the remote cleanup of ended plugin instances
PACS = 'pacs'            # PACS series registration, DICOMweb indexing and PACS queries
PERIODIC = 'periodic'    # the beat tasks

QUEUES = (SUBMIT, STATUS, REGISTER, REGISTER_LARGE, DELETE, CLEANUP, PACS, PERIODIC)

#: the queue of each task
TASK_QUEUES = {
    'plugininstances.tasks.run_plugin_instance_job': SUBMIT,
    'plugininstances.tasks.check_plugin_instance_job_exec_status': STATUS,
    'plugininstances.tasks.cancel_plugin_instance_job': STATUS,
    'plugininstances.tasks.register_plugin_instance_output': REGISTER,
    'plugininstances.tasks.register_large_plugin_instance_output': REGISTER_LARGE,
    'plugininstances.tasks.run_plugin_instance_cleanup_job': CLEANUP,
    'plugininstances.tasks.check_plugin_instance_cleanup_job': CLEANUP,
    'plugininstances.tasks.delete_plugin_instance_containers_from_remote': CLEANUP,
    'plugininstances.tasks.delete_plugin_instance': DELETE,
    'feeds.tasks.delete_feed': DELETE,
    'filebrowser.tasks.delete_folder': DELETE,
    'pacsfiles.tasks.delete_pacs_series': DELETE,
    'pacsfiles.tasks.send_pacs_query': PACS,
    'pacsfiles.tasks.register_pacs_series': PACS,  # also OXIDICOM_QUEUE_NAME for oxidicom
    'dicomweb.tasks.index_pacs_instance': PACS,
    'plugininstances.tasks.schedule_waiting_plugin_instances': PERIODIC,
    'plugininstances.tasks.check_running_plugin_instances_exec_status': PERIODIC,
    'plugininstances.tasks.cancel_waiting_plugin_instances': PERIODIC,
    'plugininstances.tasks.cancel_plugin_instances_stuck_in_lock': PERIODIC,
    'plugininstances.tasks.cancel_plugin_instances_stuck_in_scheduled_status': PERIODIC,
    'plugininstances.tasks.handle_remote_cleanup': PERIODIC,
    'plugininstances.tasks.delete_plugin_instances_jobs_from_remote': PERIODIC,
    # Celery's own, which django-celery-beat's scheduler runs daily (result_expires)
    'celery.backend_cleanup': PERIODIC,
    'plugininstances.tasks.sum': SUBMIT,
    'core.celery.debug_task': SUBMIT,
}

#: the soft and hard time limits of each queue's tasks, in seconds, sized for in-network
#: compute resources (a status call retried around a token refresh takes up to 230 s, four
#: copy submissions 29 min, the cleanup's four container deletions 36 min, the longest
#: registration measured 22 min). A registration of few files on 'register' takes a
#: second: one that reaches its soft limit there goes on to 'register_large', so that a
#: slow remote or storage never holds a slot of the short ones for long.
#: CUBE_CELERY_TIME_LIMITS_<QUEUE>="<soft>,<hard>" replaces one; set it alike on every
#: CUBE process: a task's limits travel in its message from the process that publishes it,
#: and a worker applies its own to a message that has none (oxidicom's). The deletions get
#: none until they can resume (HARBOR-planning #157).
TIME_LIMITS = {
    SUBMIT: (1800, 2100),
    STATUS: (300, 360),
    REGISTER: (300, 360),
    REGISTER_LARGE: (3000, 3300),
    DELETE: None,
    CLEANUP: (2700, 3000),
    PACS: (1800, 2100),
    PERIODIC: (300, 360),
}

#: the queues whose tasks are acknowledged only once they end, and are run again when
#: their worker dies: their tasks must be safe to run again
LATE_ACK_QUEUES = (REGISTER, REGISTER_LARGE)

#: seconds after which the broker gives an unacknowledged message to another worker,
#: whether or not the one that has it is alive: above the hard time limit of every
#: late-acknowledged task, and the same for every process
VISIBILITY_TIMEOUT = float(os.getenv('CUBE_CELERY_VISIBILITY_TIMEOUT', '3600'))


def time_limits(queue, env=None):
    """
    The (soft, hard) time limits of ``queue``'s tasks, or None for none, from the
    environment ``env`` (os.environ when None) or else the table.
    """
    env = os.environ if env is None else env
    value = env.get(f'CUBE_CELERY_TIME_LIMITS_{queue.upper()}')

    if not value:
        return TIME_LIMITS[queue]

    try:
        soft, hard = (float(limit) for limit in value.split(','))
    except ValueError:  # not two numbers
        soft = hard = 0

    if not 0 < soft < hard:
        raise ValueError(f'CUBE_CELERY_TIME_LIMITS_{queue.upper()}={value!r}: a soft '
                         f'limit under the hard one, both above 0, as "<soft>,<hard>"')
    return soft, hard


def register_fast_max_files(env=None):
    """
    The most files a registration on the register queue registers itself, from
    CUBE_CELERY_REGISTER_FAST_MAX_FILES in the environment ``env`` (os.environ when None)
    or else 100: one of more files goes on to the register_large queue, so that
    registrations of many files never hold every slot of the worker of those of few. It
    counts with listings of one object more at most, and Swift refuses a listing limit
    above its own (10,000 by default), so the value is 9,999 at most; 0 sends on every
    registration with files.
    """
    env = os.environ if env is None else env
    value = env.get('CUBE_CELERY_REGISTER_FAST_MAX_FILES') or '100'
    try:
        files = int(value)
    except ValueError:
        files = -1

    if not 0 <= files <= 9999:
        raise ValueError(f'CUBE_CELERY_REGISTER_FAST_MAX_FILES={value!r}: a whole number '
                         f'of files from 0 to 9999')
    return files


def task_options(queue, env=None):
    """
    The task options of ``queue``'s tasks: their time limits and acknowledgement.
    """
    options = {}
    limits = time_limits(queue, env)

    if limits:
        options['soft_time_limit'], options['time_limit'] = limits

    if queue in LATE_ACK_QUEUES:
        if not limits or limits[1] >= VISIBILITY_TIMEOUT:
            raise ValueError(f'the {queue} queue acknowledges its tasks late, so their '
                             f'hard time limit must be under the visibility timeout '
                             f'({VISIBILITY_TIMEOUT:.0f} s, '
                             f'CUBE_CELERY_VISIBILITY_TIMEOUT)')
        options.update(acks_late=True, reject_on_worker_lost=True)
    return options


#: see register_fast_max_files
REGISTER_FAST_MAX_FILES = register_fast_max_files()

# what the tests import: the routes the runner switches off and back on
task_routes = {task: {'queue': queue} for task, queue in TASK_QUEUES.items()}

app.conf.update(
    task_routes=task_routes,
    task_annotations={task: task_options(queue) for task, queue in TASK_QUEUES.items()},
    broker_transport_options={'visibility_timeout': VISIBILITY_TIMEOUT},
    # how long a worker that `docker stop` stops (SIGTERM, a cold shutdown with
    # REMAP_SIGTERM=SIGQUIT) lets its running tasks go on first; the cold shutdown then
    # requeues its running late-acknowledged tasks at once
    worker_soft_shutdown_timeout=float(os.getenv('CUBE_CELERY_SOFT_SHUTDOWN_TIMEOUT',
                                                 '0')),
)

#: the header that stamps each message with when it was published (see queuestats)
PUBLISHED_AT_HEADER = 'cube_published_at'


@before_task_publish.connect
def stamp_published_at(headers=None, **kwargs):
    # set anew: a retry's message carries the custom headers of the one it replaces
    if headers is not None:
        headers[PUBLISHED_AT_HEADER] = time.time()


def _period_options(period):
    # django-celery-beat's DatabaseScheduler keeps 'expire_seconds' (a whole number) and
    # drops 'expires': a tick a backlog delays past its period is dropped
    return {'expire_seconds': max(1, int(period))}


# setup periodic tasks
app.conf.beat_schedule = {
    'schedule-waiting-plugin-instances-every-45-seconds': {
        'task': 'plugininstances.tasks.schedule_waiting_plugin_instances',
        'schedule': POLL_INTERVAL,
        'options': _period_options(POLL_INTERVAL),
    },
    'check-running-plugin-instances-exec-status-every-30-seconds': {
        'task': 'plugininstances.tasks.check_running_plugin_instances_exec_status',
        'schedule': POLL_INTERVAL,
        'options': _period_options(POLL_INTERVAL),
    },
    'cancel-waiting-plugin-instances-every-30-seconds': {
        'task': 'plugininstances.tasks.cancel_waiting_plugin_instances',
        'schedule': POLL_INTERVAL,
        'options': _period_options(POLL_INTERVAL),
    },
    'handle-remote-cleanup-every-60-seconds': {
        'task': 'plugininstances.tasks.handle_remote_cleanup',
        'schedule': 60.0,
        'options': _period_options(60.0),
    },
    'cancel-plugin-instances-stuck-in-lock-every-7200-seconds': {
        'task': 'plugininstances.tasks.cancel_plugin_instances_stuck_in_lock',
        'schedule': 7200.0,
        'options': _period_options(7200.0),
    },
    'cancel-plugin-instances-stuck-in-scheduled_status-every-7200-seconds': {
        'task': 'plugininstances.tasks.cancel_plugin_instances_stuck_in_scheduled_status',
        'schedule': 7200.0,
        'options': _period_options(7200.0),
    },
    'delete-plugin-instances-jobs-from-remote-every-7200-seconds': {
        'task': 'plugininstances.tasks.delete_plugin_instances_jobs_from_remote',
        'schedule': 7200.0,
        'options': _period_options(7200.0),
    },
}

# use logging settings in Django settings
@setup_logging.connect
def config_loggers(*args, **kwags):
    dictConfig(settings.LOGGING)

# example task that is passed info about itself
@app.task(bind=True)
def debug_task(self):
    print('Request: {0!r}'.format(self.request))
