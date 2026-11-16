
import logging
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone
from django.conf import settings

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from celery.signals import task_failure

from core.celery import CLEANUP, REGISTER_FAST_MAX_FILES, REGISTER_LARGE, time_limits
from core.tasks import DedupedTask, single_instance

from .models import PluginInstance
from .services.pluginjobs import PluginInstanceAppJob
from .services.copyjobs import PluginInstanceCopyJob
from .services.uploadjobs import PluginInstanceUploadJob
from .services.deletejobs import PluginInstanceDeleteJob


logger = logging.getLogger(__name__)


# the remote cleanup's delete job has tasks of its own (run_plugin_instance_cleanup_job)
JOB_CLASSES = {
    'PluginInstanceAppJob': PluginInstanceAppJob,
    'PluginInstanceCopyJob': PluginInstanceCopyJob,
    'PluginInstanceUploadJob': PluginInstanceUploadJob,
}


# the job class whose status check a plugin instance's status calls for
CHECKED_JOB_CLASSES = {
    'copying': 'PluginInstanceCopyJob',
    'started': 'PluginInstanceAppJob',
    'uploading': 'PluginInstanceUploadJob',
}

# how many times a registration that reached its soft time limit runs again
REGISTRATION_RETRIES = 2

#: how long a deletion of an instance's remote containers holds the instance's lock at
#: most: the hard time limit of the cleanup's tasks, which ends a run sooner
CONTAINER_DELETION_LOCK_TTL = time_limits(CLEANUP)[1]


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


@shared_task(base=DedupedTask, dedupe_args=1)
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
def register_plugin_instance_output(plg_inst_id):
    """
    Register the output files of a plugin instance in 'registeringFiles', the status a
    status check moved it to when its job ended, and end it (see
    PluginInstanceAppJob.register_output), when they are REGISTER_FAST_MAX_FILES at most.
    Any other registration goes on to register_large_plugin_instance_output, on a queue of
    its own, so that registrations of many files never hold every slot of the worker of
    this one's queue; so does one that reaches this queue's short soft time limit, as its
    next attempt.

    The messages of both are acknowledged once the task ends and run again when its worker
    dies, so a delivery that a newer one overtook writes nothing. On the soft time limit of
    register_large_plugin_instance_output a registration leaves the instance in
    'registeringFiles' and runs again, until it has had REGISTRATION_RETRIES + 1 attempts
    in all, then cancels it with CODE13.
    """
    job = _registration_job(plg_inst_id)
    if job is None:
        return

    reason = _reason_to_send_on(job)
    if reason:
        register_large_plugin_instance_output.delay(plg_inst_id)
        logger.info(f'Sent the registration of the output files of job {job.str_job_id} '
                    f'on to the {REGISTER_LARGE} queue: {reason}')
        return
    _register_output(register_plugin_instance_output, job,
                     again=register_large_plugin_instance_output)


@shared_task
def register_large_plugin_instance_output(plg_inst_id):
    """
    Register the output files of a plugin instance as register_plugin_instance_output
    does, for the registrations it sends on.
    """
    job = _registration_job(plg_inst_id)
    if job is not None:
        _register_output(register_large_plugin_instance_output, job)


def _registration_job(plg_inst_id):
    """
    The app job of the plugin instance with id ``plg_inst_id``, or None when the instance
    is gone or no longer registering files.
    """
    try:
        plugin_inst = PluginInstance.objects.get(pk=plg_inst_id)
    except PluginInstance.DoesNotExist:
        logger.error(f'Plugin instance with id {plg_inst_id} not found when registering '
                     f'its output files')
        return None

    if plugin_inst.status != 'registeringFiles':
        return None  # registered and ended already, or cancelled
    return PluginInstanceAppJob(plugin_inst)


def _reason_to_send_on(job):
    """
    Why ``job``'s registration is not one for the register queue, or None when it is: it
    registers REGISTER_FAST_MAX_FILES files at most
    (PluginInstanceAppJob.registers_at_most).
    A count that fails, even by the soft time limit, sends it on: the registration that
    the other queue runs then reports what fails.
    """
    try:
        if job.registers_at_most(REGISTER_FAST_MAX_FILES):
            return None
    except Exception:  # SoftTimeLimitExceeded included
        logger.warning(f'Could not count the output files of job {job.str_job_id}',
                       exc_info=True)
        return 'its files could not be counted'

    if not job.pfcon_client.pfcon_innetwork:
        return 'the remote sends its output as a zip'
    return (f'more than {REGISTER_FAST_MAX_FILES} files, or a link file among the objects '
            f'of an input of a ts plugin')


def _register_output(task, job, again=None):
    """
    Run ``job``'s registration in ``task``, and when it ends the instance
    'finishedSuccessfully', schedule the ready waiting instances of its feed
    (_schedule_followers). On its soft time limit run it again at once: in ``again`` when
    given, another task, which takes it as its next attempt, else in ``task`` itself; once
    it has had REGISTRATION_RETRIES + 1 attempts, cancel the instance with CODE13 instead.
    """
    try:
        job.register_output()
    except SoftTimeLimitExceeded:
        attempt = task.request.retries
        logger.error(f'Registration of the output files of plugin instance '
                     f'{job.c_plugin_inst.id} reached its time limit '
                     f'(attempt {attempt + 1})')

        if attempt >= REGISTRATION_RETRIES:
            job.give_up_registration('CODE13')
        elif again is not None:
            again.apply_async((job.c_plugin_inst.id,), retries=attempt + 1)
        else:
            # at once: a message with a countdown waits in its worker's memory outside
            # the reserved tasks that --disable-prefetch counts, so that worker would
            # take another task meanwhile and the retry would wait behind it
            raise task.retry(countdown=0, max_retries=REGISTRATION_RETRIES)
    else:
        # the object's status is the final one only when this run's final write won
        if job.c_plugin_inst.status == 'finishedSuccessfully':
            _schedule_followers(job.c_plugin_inst)


def _schedule_followers(plugin_inst):
    """
    Schedule at once the waiting instances of the feed of ``plugin_inst``, which just
    finished successfully, that are ready now, rather than at the next pass of the beat's
    schedule_waiting_plugin_instances, which a failure here leaves those still waiting to.
    """
    try:
        schedule_ready_plugin_instances(feed_id=plugin_inst.feed_id)
    except Exception:  # SoftTimeLimitExceeded included
        logger.exception(f'Could not schedule the ready plugin instances of the feed of '
                         f'plugin instance {plugin_inst.id}: the beat schedules those '
                         f'still waiting')


@shared_task
def run_plugin_instance_cleanup_job(plg_inst_id):
    """
    Submit the data delete job of a plugin instance's remote cleanup.
    """
    plugin_inst = _cleanup_instance(plg_inst_id)
    if plugin_inst is not None:
        PluginInstanceDeleteJob(plugin_inst).run()


@shared_task(base=DedupedTask)
def check_plugin_instance_cleanup_job(plg_inst_id):
    """
    Check the data delete job of a plugin instance's remote cleanup, and submit it again
    when it is not there.
    """
    plugin_inst = _cleanup_instance(plg_inst_id)
    if plugin_inst is not None:
        PluginInstanceDeleteJob(plugin_inst).check_exec_status()


def _cleanup_instance(plg_inst_id):
    try:
        return PluginInstance.objects.get(pk=plg_inst_id)
    except PluginInstance.DoesNotExist:
        logger.error(f"Plugin instance with id {plg_inst_id} not found when running its "
                     f"remote cleanup.")
        return None


@shared_task(base=DedupedTask)
@single_instance(CONTAINER_DELETION_LOCK_TTL, key_args=1)
def delete_plugin_instance_containers_from_remote(plg_inst_id):
    """
    Delete all remote containers for a plugin instance's job from the remote compute
    environment. Updates cleanup status accordingly. One run per instance at a time: a
    run can outlast the pending marker that keeps the periodic task from publishing
    another one, and two would count each failure twice.
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


@shared_task
@single_instance(60)
def schedule_waiting_plugin_instances():
    """
    Schedule the jobs of every plugin instance in 'waiting' whose parents all finished
    successfully (schedule_ready_plugin_instances): those a registration that ended did
    not schedule already.
    """
    schedule_ready_plugin_instances()


def schedule_ready_plugin_instances(feed_id=None):
    """
    Schedule the jobs corresponding to the plugin instances in 'waiting' DB status, those
    of the feed with id ``feed_id`` when given, whose previous plugin instance is in
    'finishedSuccessfully' DB status. However, if the plugin instance is of type 'ts' all
    the ancestor plugin instances with id in the list given by the plugininstances
    parameter must also be in 'finishedSuccessfully' DB status. Each moves from 'waiting'
    once, whoever else schedules it meanwhile (schedule_plugin_instance), and one that
    cannot be scheduled (its compute resource deleted, say) is logged and stops no other.
    """
    instances = PluginInstance.objects.filter(status='waiting',
                                              previous__status='finishedSuccessfully')
    if feed_id is not None:
        instances = instances.filter(feed_id=feed_id)

    for plg_inst in instances.select_related('plugin__meta', 'compute_resource'):
        try:
            if _ts_parents_finished(plg_inst):
                schedule_plugin_instance(plg_inst, 'waiting')
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            logger.exception(f'Could not schedule plugin instance {plg_inst.id}')


def _ts_parents_finished(plg_inst):
    """
    Whether every instance of the 'plugininstances' parameter of ``plg_inst`` finished
    successfully, when it is a 'ts' instance with that parameter (always true otherwise).
    """
    if plg_inst.plugin.meta.type != 'ts':
        return True

    param = plg_inst.string_param.filter(plugin_param__name='plugininstances').first()
    if not (param and param.value):
        return True

    parent_ids = [int(parent_id) for parent_id in param.value.split(',')]
    parents = PluginInstance.objects.filter(pk__in=parent_ids)
    return all(parent.status == 'finishedSuccessfully' for parent in parents)


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
    
    # one pending check per instance at most; in random order, so that when the workers
    # fall behind the instances they get to are not always the same
    instances = PluginInstance.objects.filter(lookup).order_by('?').values_list('id',
                                                                               'status')
    for plg_inst_id, status in instances:
        check_plugin_instance_job_exec_status.enqueue_once(plg_inst_id,
                                                           CHECKED_JOB_CLASSES[status])


@shared_task
def handle_remote_cleanup():
    """
    Handle remote cleanup for plugin instances that need data deletion or container
    deletion from the remote compute environment.
    """
    # instances needing data deletion status check
    deleting_data = PluginInstance.objects.filter(
        remote_cleanup_status='deletingData').values_list('id', flat=True)
    for plg_inst_id in deleting_data:
        check_plugin_instance_cleanup_job.enqueue_once(plg_inst_id)

    # instances needing container deletion
    deleting_containers = PluginInstance.objects.filter(
        remote_cleanup_status='deletingContainers').values_list('id', flat=True)
    for plg_inst_id in deleting_containers:
        delete_plugin_instance_containers_from_remote.enqueue_once(plg_inst_id)


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

    # the transition to 'registeringFiles' stamps end_date, whether or not a task has
    # started the registration since
    instances = PluginInstance.objects.filter(status='registeringFiles',
                                              end_date__lt=cutoff)
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
        'plugininstances.tasks.register_plugin_instance_output',
        'plugininstances.tasks.register_large_plugin_instance_output',
    }
    if sender.name not in handled_tasks:
        return  # ignore other tasks

    if not args:
        logger.warning(f"No args found for failed task {sender.name}")
        return

    plg_inst_id = args[0]
    logger.info(f"Sending cancelling task for plugin instance with id {plg_inst_id} "
                f"after crash in task {sender.name}")
    cancel_plugin_instance_job.delay(plg_inst_id, error_code='CODE19')


@shared_task  # toy task for testing celery stuff
def sum(x, y):
    return x + y
