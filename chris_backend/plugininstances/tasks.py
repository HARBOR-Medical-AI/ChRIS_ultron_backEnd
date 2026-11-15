
import logging
from functools import wraps
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone
from django.conf import settings

from celery import shared_task
from celery.signals import task_failure

from .models import PluginInstance
from .services.pluginjobs import PluginInstanceAppJob
from .services.copyjobs import PluginInstanceCopyJob
from .services.uploadjobs import PluginInstanceUploadJob
from .services.deletejobs import PluginInstanceDeleteJob


logger = logging.getLogger(__name__)


JOB_CLASSES = {
    'PluginInstanceAppJob': PluginInstanceAppJob,
    'PluginInstanceCopyJob': PluginInstanceCopyJob,
    'PluginInstanceUploadJob': PluginInstanceUploadJob,
    'PluginInstanceDeleteJob': PluginInstanceDeleteJob,
}


def skip_if_running(f):
    """
    This decorator is supposed to ensure that a task is only running once across all
    workers.
    https://stackoverflow.com/questions/20894771/celery-beat-limit-to-single-task-instance-at-a-time
    """
    task_name = f'{f.__module__}.{f.__name__}'
    @wraps(f)
    def wrapped(self, *args, **kwargs):
        workers = self.app.control.inspect().active()
        if workers is None:
            logger.info('could not find worker for task %s (%s, %s), skipping',
                        task_name, args, kwargs)
            return None
        for worker, tasks in workers.items():
            for task in tasks:
                if (task_name == task['name'] and tuple(args) == tuple(task['args']) and
                        kwargs == task['kwargs'] and self.request.id != task['id']):
                    logger.info('task %s (%s, %s) is running on %s, skipping',
                                task_name, args, kwargs, worker)
                    return None
        return f(self, *args, **kwargs)
    return wrapped


@shared_task(bind=True, autoretry_for=(Exception,), retry_kwargs={"max_retries": 3})
def delete_plugin_instance(self, plugin_inst_id):
    try:
        plugin_inst = PluginInstance.objects.get(id=plugin_inst_id)

        if not plugin_inst.is_pending_deletion():
            return # idempotent safety

        plugin_inst.delete()
    except PluginInstance.DoesNotExist:
        pass
    except Exception as e:
        PluginInstance.objects.filter(id=plugin_inst_id).update(  # atomic update
            deletion_status=PluginInstance.DeletionStatus.FAILED,
            deletion_error=str(e)
        )
        raise

@shared_task
def run_plugin_instance_job(plg_inst_id, job_class_name):
    """
    Run a job for this plugin instance.
    """
    try:
        plugin_inst = PluginInstance.objects.get(pk=plg_inst_id)
    except PluginInstance.DoesNotExist:
        logger.error(f"Plugin instance with id {plg_inst_id} not found when running "
                     f"run_plugin_instance_job task.")
    else:
        job_class = JOB_CLASSES[job_class_name]
        plg_inst_job = job_class(plugin_inst)
        plg_inst_job.run()


@shared_task
def check_plugin_instance_job_exec_status(plg_inst_id, job_class_name):
    """
    Check the execution status of a job for this plugin instance.
    """
    try:
        plugin_inst = PluginInstance.objects.get(pk=plg_inst_id)
    except PluginInstance.DoesNotExist:
        logger.error(f"Plugin instance with id {plg_inst_id} not found when running "
                     f"check_plugin_instance_job_exec_status task.")
    else:
        job_class = JOB_CLASSES[job_class_name]
        plg_inst_job = job_class(plugin_inst)
        plg_inst_job.check_exec_status()


#: how many times a cancel that failed (the database or the storage gone, say) tries again,
#: one, two, then four seconds later or so
CANCEL_RETRIES = 3


@shared_task(autoretry_for=(Exception,), max_retries=CANCEL_RETRIES, retry_backoff=True)
def cancel_plugin_instance_job(plg_inst_id, job_class_name=None, error_code=''):
    """
    Cancel the execution of the job corresponding to this plugin instance, with
    ``error_code`` when given, and make sure its remote cleanup is scheduled.
    If job_class_name is not provided it is auto-detected from the instance status.
    A failure is tried again a few times, apart, and then left: the periodic tasks cancel
    an instance still stuck later.
    """
    try:
        plugin_inst = PluginInstance.objects.get(pk=plg_inst_id)
    except PluginInstance.DoesNotExist:
        logger.error(f"Plugin instance with id {plg_inst_id} not found when running "
                     f"cancel_plugin_instance task.")
    else:
        if job_class_name is None:
            job_class_name = _detect_job_class_name(plugin_inst)
        job_class = JOB_CLASSES[job_class_name]
        plg_inst_job = job_class(plugin_inst)
        plg_inst_job.cancel_exec(error_code)


def _detect_job_class_name(plugin_inst):
    """
    Auto-detect the appropriate job class name based on the plugin instance's current
    status and compute resource configuration.
    """
    cr = plugin_inst.compute_resource
    status = plugin_inst.status

    if status == 'copying' and cr.compute_requires_copy_job:
        return 'PluginInstanceCopyJob'

    if status == 'uploading' and cr.compute_requires_upload_job:
        return 'PluginInstanceUploadJob'
    return 'PluginInstanceAppJob'


@shared_task
def delete_plugin_instance_containers_from_remote(plg_inst_id):
    """
    Delete all remote containers for a plugin instance's job from the remote compute
    environment. Updates cleanup status accordingly.
    """
    try:
        plugin_inst = PluginInstance.objects.get(pk=plg_inst_id)
    except PluginInstance.DoesNotExist:
        logger.error(f"Plugin instance with id {plg_inst_id} not found when running "
                     f"delete_plugin_instance_containers_from_remote task.")
        return

    if plugin_inst.remote_cleanup_status != 'deletingContainers':
        return  # another task has moved it on

    job = PluginInstanceDeleteJob(plugin_inst)
    if job.delete_all_remote_containers():
        job.complete('complete')
    elif (plugin_inst.increment('remote_cleanup_retry_count') >
          PluginInstance.MAX_REMOTE_CLEANUP_RETRIES):
        job.complete('failed')


@shared_task(bind=True)
@skip_if_running
def schedule_waiting_plugin_instances(self):  # task is passed info about itself
    """
    Schedule the jobs corresponding to all plugin instances in 'waiting' DB status
    and whose previous plugin instance is in 'finishedSuccessfully' DB status.
    However, if the plugin instance is of type 'ts' all the ancestor plugin instances
    with id in the list given by the plugininstances parameter must also be in
    'finishedSuccessfully' DB status.
    """
    all_instances = PluginInstance.objects.filter(status='waiting',
                                                  previous__status='finishedSuccessfully')
    ts_instances = all_instances.filter(plugin__meta__type='ts')

    for plg_inst in ts_instances:
        param = plg_inst.string_param.filter(plugin_param__name='plugininstances').first()

        if param and param.value:
            parent_ids = [int(parent_id) for parent_id in param.value.split(',')]
            parents = PluginInstance.objects.filter(pk__in=parent_ids)
            finished = [parent.status == 'finishedSuccessfully' for parent in parents]

            if all(finished):
                schedule_plugin_instance(plg_inst, 'waiting')
        else:
            schedule_plugin_instance(plg_inst, 'waiting')

    for plg_inst in all_instances.difference(ts_instances):
        schedule_plugin_instance(plg_inst, 'waiting')


def schedule_plugin_instance(plg_inst, source):
    """
    Move a plugin instance from the status ``source`` to the first status of its run on
    its compute resource ('copying' when the resource needs a copy job, 'scheduled'
    otherwise) and, when this call moved it, have a task submit that job. Returns the
    ``StatusWrite``.
    """
    if plg_inst.compute_resource.compute_requires_copy_job:
        to, job_class_name, fields = 'copying', 'PluginInstanceCopyJob', {}
    else:
        now = timezone.now()  # the scheduling date
        to, job_class_name = 'scheduled', 'PluginInstanceAppJob'
        fields = {'start_date': now, 'end_date': now}

    moved = plg_inst.transition(to, source, **fields)

    if moved:
        run_plugin_instance_job.delay(plg_inst.id, job_class_name)
    return moved


@shared_task
def check_running_plugin_instances_exec_status():
    """
    Check the execution status of all the running jobs.
    """
    lookup = Q(status='started')

    if settings.STORAGE_ENV not in ('filesystem', 'zipfile'):
        lookup = Q(status='copying') | lookup

    if settings.STORAGE_ENV not in ('filesystem', 'zipfile', 'fslink'):
        lookup = lookup | Q(status='uploading')
    
    instances = PluginInstance.objects.filter(lookup)

    for plg_inst in instances:
        if plg_inst.status == 'copying':
            check_plugin_instance_job_exec_status.delay(plg_inst.id, 
                                                        'PluginInstanceCopyJob')  # call async task
        elif plg_inst.status == 'started':
            check_plugin_instance_job_exec_status.delay(plg_inst.id, 
                                                        'PluginInstanceAppJob')
        elif plg_inst.status == 'uploading':
            check_plugin_instance_job_exec_status.delay(plg_inst.id, 
                                                        'PluginInstanceUploadJob')


@shared_task
def handle_remote_cleanup():
    """
    Handle remote cleanup for plugin instances that need data deletion or container
    deletion from the remote compute environment.
    """
    # instances needing data deletion status check
    deleting_data = PluginInstance.objects.filter(remote_cleanup_status='deletingData')
    for plg_inst in deleting_data:
        check_plugin_instance_job_exec_status.delay(plg_inst.id,
                                                     'PluginInstanceDeleteJob')

    # instances needing container deletion
    deleting_containers = PluginInstance.objects.filter(
        remote_cleanup_status='deletingContainers')
    for plg_inst in deleting_containers:
        delete_plugin_instance_containers_from_remote.delay(plg_inst.id)


@shared_task
def cancel_waiting_plugin_instances():
    """
    Cancel all plugin instances in 'waiting' DB status when their previous plugin 
    instance is in either 'finishedWithError' or 'cancelled' DB status. Plugin 
    instances of type 'ts' are cancelled if at least one of their ancestors is in 
    any of those DB statuses.
    """
    lookup = Q(previous__status='finishedWithError') | Q(previous__status='cancelled')
    PluginInstance.objects.filter(status='waiting').filter(lookup).transition(
        'cancelled', 'waiting')

    ts_instances = PluginInstance.objects.filter(
        status='waiting'
    ).filter(plugin__meta__type='ts')

    plg_inst_ids = []

    for plg_inst in ts_instances:
        param = plg_inst.string_param.filter(plugin_param__name='plugininstances').first()

        if param and param.value:
            parent_ids = [int(parent_id) for parent_id in param.value.split(',')]
            parents = PluginInstance.objects.filter(pk__in=parent_ids)

            for parent in parents:
                if parent.status in ('finishedWithError', 'cancelled'):
                    plg_inst_ids.append(plg_inst.id)
                    break

    PluginInstance.objects.filter(pk__in=plg_inst_ids).transition('cancelled', 'waiting')


@shared_task
def delete_plugin_instances_jobs_from_remote():
    """
    Collect all plugin instances whose remote app job finished after two days ago
    but failed to be deleted from the remote compute environment. Then schedule a new
    delete request for all of them.
    """
    cutoff = timezone.now() - timedelta(days=2)  # hardcoded cutoff delta

    instances = PluginInstance.objects.filter(error_code='CODE12', end_date__gt=cutoff)
    for plg_inst in instances:
        delete_plugin_instance_containers_from_remote.delay(plg_inst.id)


@shared_task
def cancel_plugin_instances_stuck_in_lock():
    """
    Collect all plugin instances for which the check_plugin_instance_job_exec_status 
    async task is stuck or failed in the lock section of the code (e.g. because of a 
    worker crash). Then schedule a new cancel request for all of them.
    """
    cutoff = timezone.now() - timedelta(minutes=240)  # hardcoded cutoff delta

    instances = PluginInstance.objects.filter(status='registeringFiles', 
                                              lock__start_date__lt=cutoff)
    for plg_inst in instances:
        logger.error(f"Plugin instance with id {plg_inst.id} stuck in lock. Sending "
                     f"cancelling task for it. ")
        cancel_plugin_instance_job.delay(plg_inst.id, error_code='CODE18')


@shared_task
def cancel_plugin_instances_stuck_in_scheduled_status():
    """
    Cancel all plugin instances stuck in 'scheduled' status (e.g. because of a periodic
    or normal worker crash). A job may have been submitted to pfcon before the crash,
    so a proper cancel task is scheduled to handle remote cleanup.
    """
    cutoff = timezone.now() - timedelta(minutes=240)  # hardcoded cutoff delta

    instances = PluginInstance.objects.filter(status='scheduled', start_date__lt=cutoff)

    for plg_inst in instances:
        logger.error(f"Plugin instance with id {plg_inst.id} stuck in scheduled status. "
                     f"Sending cancelling task for it. ")
        cancel_plugin_instance_job.delay(plg_inst.id, error_code='CODE18')


@task_failure.connect
def cancel_plugin_inst_on_task_failure(sender=None, task_id=None, exception=None,
                                       args=None, kwargs=None, traceback=None, einfo=None,
                                       **other_kwargs):
    """
    Handler to cancel a plugin instance when related async tasks crash (e.g. because
    of an uncaught exception, or a time limit). It runs in the worker's main process for a
    task its pool lost, so it only sends a task: the cancel task cancels the instance
    with CODE19 if it is still active and schedules its remote cleanup if nothing did.
    A crashed remote cleanup is left to the periodic task that drives cleanups, and a
    crashed cancel to its own retries: sending another at once would loop as fast as the
    workers take it.
    """
    # list of task names we want to handle
    handled_tasks = {
        'plugininstances.tasks.run_plugin_instance_job',
    }
    if sender.name not in handled_tasks:
        return  # ignore other tasks

    if not args:
        logger.warning(f"No args found for failed task {sender.name}")
        return

    if len(args) > 1 and args[1] == 'PluginInstanceDeleteJob':
        return

    plg_inst_id = args[0]
    logger.info(f"Sending cancelling task for plugin instance with id {plg_inst_id} "
                f"after crash in task {sender.name}")
    cancel_plugin_instance_job.delay(plg_inst_id, error_code='CODE19')


@shared_task  # toy task for testing celery stuff
def sum(x, y):
    return x + y
