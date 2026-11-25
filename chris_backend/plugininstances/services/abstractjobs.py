"""
Abstract job manager module that provides the base interface for submitting
and checking the execution status of jobs running in a remote compute
environment (ChRIS / pfcon interface) as well as deleting them when finished.

Several tasks can act on one plugin instance at once (overlapping status checks, a cancel
from the API, a redelivered task). Thus the job classes never save an instance whole:
every write of its status is a transition (``PluginInstance.transition``) from the
statuses the writer expects, its other fields are written while it is in the status the
writer expects (``update_if_status``) but for the retry counters, and the remote cleanup
moves on only from the step it expects (``move_remote_cleanup``). Only the task that won
a transition does what follows it (submit the next job, the remote cleanup's first one);
a cancel that lost to another task's end makes sure the cleanup is scheduled. A task that
alone moves the instance on from the status a transition wrote goes out through
``publish_or_cancel``, which cancels the instance with CODE01 when it cannot be published.
"""

import logging
import io
import abc
import itertools
import json
import re
import time
from typing import Iterator

from celery.exceptions import SoftTimeLimitExceeded
from pfconclient import client as pfcon
from pfconclient.client import JobType
from pfconclient.exceptions import (PfconRequestException,
                                    PfconRequestInvalidTokenException)

from django.conf import settings
from django.utils import timezone

from core.storage import connect_storage, sanitized_path
from core.models import ChrisFile, ChrisInstance, ChrisLinkFile
from plugininstances.enums import ARM_REMOTE_CLEANUP, INACTIVE_STATUSES


logger = logging.getLogger(__name__)

# the name of an environment variable set in a job description or printed in a job's logs
_VARIABLE = re.compile(r'(?<![A-Za-z0-9_])([A-Z0-9_]+)=')
_VALUE = re.compile(r'\S*')


def mask_tokens(text):
    """
    ``text`` without the value of any environment variable whose name has the word TOKEN
    (``CHRIS_USER_TOKEN=...``), also one inside another's value, in one pass.
    """
    out, end = [], 0

    for match in _VARIABLE.finditer(text):
        if match.start() >= end and 'TOKEN' in match[1].split('_'):
            out += [text[end:match.end()], '********']
            end = _VALUE.match(text, match.end()).end()
    return ''.join(out) + text[end:]


class LogJson:
    """
    A job description or a pfcon response for a DEBUG log line: formatted as JSON only
    when the line is emitted, without the value of an environment variable that holds a
    token (CHRIS_USER_TOKEN gives the job its owner's API access) in any of its strings:
    the description's environment, or the job's logs in a response.
    """

    def __init__(self, data):
        self.data = data

    def __str__(self):
        return json.dumps(self._masked(self.data), default=str)

    @classmethod
    def _masked(cls, data):
        if isinstance(data, str):
            return mask_tokens(data)
        if isinstance(data, dict):
            return {key: cls._masked(value) for key, value in data.items()}
        if isinstance(data, (list, tuple)):
            return [cls._masked(value) for value in data]
        return data


class PluginInstanceJob(abc.ABC):
    """
    ``PluginInstanceJob`` provides an interface for managing remote jobs related to a
    plugin instance.

    ``PluginInstanceJob`` methods implement helper functions for submitting and checking
    the status of jobs required during the full flow of running a plugin instance's app
    on a remote compute environment.
    """

    #: the status a plugin instance is in while this job runs; None for a job that runs
    #: after the instance's terminal status
    status = None

    #: how long a job lists a folder in storage again for objects it expects there that
    #: a listing did not show (listing_tries): seconds after the end of the first listing
    LISTING_DEADLINE = 60
    #: the seconds between two of those listings, the last again from then on
    LISTING_WAITS = (1, 2, 4, 8)

    # not annotated as a PluginInstance yet: mypy would then flag each use of a relation
    # of it that may be null
    def __init__(self, plugin_instance):
        """
        A job of ``plugin_instance``, a ``PluginInstance``.
        """
        self.c_plugin_inst = plugin_instance

        self.str_job_id_prefix = ChrisInstance.load().job_id_prefix
        self.str_job_id = self.str_job_id_prefix + str(plugin_instance.id)

        cr = self.c_plugin_inst.compute_resource
        self.pfcon_client = pfcon.Client(cr.compute_url, cr.compute_auth_token)
        self.pfcon_client.pfcon_innetwork = cr.compute_innetwork
        self.pfcon_client.requires_copy_job = cr.compute_requires_copy_job
        self.pfcon_client.requires_upload_job = cr.compute_requires_upload_job

        self.plugin_inst_output_files = set()  # set of obj names in object storage

        self.storage_manager = connect_storage(settings)
        self.storage_env = settings.STORAGE_ENV

    @abc.abstractmethod
    def run(self):
        """
        Submit the job to the remote compute service for execution.
        """
        ...

    @abc.abstractmethod
    def check_exec_status(self):
        """
        Check the job's execution status in the remote compute service.
        """
        ...
 
    def cancel_exec(self, error_code='', source=None):
        """
        Cancel the plugin instance from the statuses ``source`` (any active one by
        default; a status check gives the one it checked), with ``error_code`` when given,
        and make sure its remote cleanup is scheduled: ``PluginInstance.cancel`` arms it
        in the same UPDATE when the instance may have a job in the remote compute, and
        logs the final status, and this then submits the cleanup's first job. An instance
        that another writer has already ended only gets its cleanup scheduled, if nothing
        did. Returns the ``StatusWrite``.
        """
        fields = {'error_code': error_code} if error_code else {}
        ended = self.c_plugin_inst.cancel(source, job_id=self.str_job_id, **fields)

        if ended:
            if self.c_plugin_inst.remote_cleanup_status == 'deletingData':
                self._submit_remote_cleanup()
        elif ended.status in INACTIVE_STATUSES:
            self.schedule_remote_cleanup()
        return ended

    def end(self, to, source, error_code='', **fields):
        """
        Move the plugin instance from the statuses ``source``, in which it has or may
        have a job in the remote compute, to the terminal status ``to``, with
        ``error_code`` when given and ``fields``, and its remote cleanup armed in the same
        UPDATE; the winner then logs the final status and submits the cleanup's first
        job. Returns the ``StatusWrite``.
        """
        if error_code:
            fields['error_code'] = error_code

        ended = self.c_plugin_inst.transition(to, source, **fields, **ARM_REMOTE_CLEANUP)

        if ended:
            self.log_final_status()
            self._submit_remote_cleanup()
        return ended

    def log_final_status(self):
        """
        Log the terminal status, and the error code if any, that this job's write has
        just given the plugin instance: the line that tells how the instance ended.
        """
        self.c_plugin_inst.log_final_status(self.str_job_id)

    def is_runnable(self):
        """
        Whether the plugin instance is in the status this job runs in: a task for one that
        another task has moved on, or cancelled, submits nothing.
        """
        if self.c_plugin_inst.status == self.status:
            return True

        logger.info(f'Not running {type(self).__name__} {self.str_job_id}: the plugin '
                    f'instance is {self.c_plugin_inst.status}, not {self.status}')
        return False

    def previous_output_path(self, check: bool = True) -> str:
        """
        The output folder of the previous plugin instance, the input of a 'ds' plugin's
        job. With ``check``, on object storage, it first waits until a listing of the
        folder finds as many objects as the previous instance has files and link files
        registered there: a listing may miss objects written a moment before (eventual
        consistency). Each listing stops once it has counted that many; listings are
        LISTING_WAITS apart, and after LISTING_DEADLINE seconds without one that found
        them all it raises, with CODE11. On a filesystem a listing finds what is there,
        so nothing is checked.

        The first job that sends the folder to the remote checks it: the copy job when
        the remote needs one, and the plugin job after it does not check it again.
        """
        output_path = self.c_plugin_inst.previous.get_output_path()

        if check and self.storage_env not in ('fslink', 'filesystem'):
            self._wait_until_listed(output_path)
        return output_path

    def _wait_until_listed(self, folder: str) -> None:
        job_id = self.str_job_id
        prefix = folder + '/'
        count = (ChrisFile.objects.filter(fname__startswith=prefix).count()
                 + ChrisLinkFile.objects.filter(fname__startswith=prefix).count())
        if not count:
            return

        for _ in self.listing_tries():
            try:
                if self._lists_at_least(folder, count):
                    return
            except SoftTimeLimitExceeded:
                raise
            except Exception as e:
                logger.error(f'[CODE06,{job_id}]: Error while listing storage files '
                             f'in {folder}, detail: {str(e)}')

        logger.error(f'[CODE11,{job_id}]: Error while listing storage files in '
                     f'{folder}, detail: Presumable eventual consistency problem')
        self.c_plugin_inst.error_code = 'CODE11'
        raise NameError('Presumable eventual consistency problem.')

    def _lists_at_least(self, folder: str, count: int) -> bool:
        """
        Whether a listing of ``folder`` finds ``count`` objects, listing no further. A
        name with commas is not counted: a listing may still show one that the folder's
        registration renamed, in place of a new name it does not show yet.
        """
        start, listed = len(folder) + 1, 0

        for obj in self.storage_manager.walk(folder):
            if sanitized_path(obj.path[start:]) == obj.path[start:]:
                listed += 1
                if listed == count:
                    return True
        return False

    def listing_tries(self) -> Iterator[None]:
        """
        When to list a folder in storage that may not show yet all the objects a job
        expects there: an object store's listings are eventually consistent, and a
        network filesystem's caches can lag the writes of another node. It yields before
        each listing, the first at once, the next ones after each of LISTING_WAITS (the
        last again from then on), as long as a listing can start within LISTING_DEADLINE
        seconds of the end of the first one. The caller stops once a listing has found
        them all.
        """
        yield
        deadline = time.monotonic() + self.LISTING_DEADLINE

        for wait in itertools.chain(self.LISTING_WAITS,
                                    itertools.repeat(self.LISTING_WAITS[-1])):
            if time.monotonic() + wait > deadline:
                return
            time.sleep(wait)
            yield

    def submitted(self, to=None, **fields):
        """
        Record what a successful submission returned (``fields``), moving the plugin
        instance to the status ``to`` when given, as long as it is still in this job's
        status. When it is not and it was cancelled meanwhile, the job just submitted may
        be the only one left in the remote, so its cleanup is scheduled again, also after it
        completed; another status means another task got there first, and nothing is left
        to do. Returns the ``StatusWrite``.
        """
        if to is None:
            written = self.c_plugin_inst.update_if_status(self.status, **fields)
        else:
            written = self.c_plugin_inst.transition(to, self.status, **fields)

        if not written and written.status == 'cancelled':
            self.schedule_remote_cleanup(rearm=True)
        return written

    def enqueue_run(self, job_class_name, moved_to=None):
        """
        Have a task submit the job of the class named ``job_class_name`` for the plugin
        instance: the status checks of the copy, plugin and upload jobs never submit one
        themselves. ``moved_to`` is the status the caller has just moved the instance to,
        where only that task moves it on: a task that cannot be published then cancels the
        instance (publish_or_cancel). Without it, a publish that fails only raises: the
        next status check submits a job again, as the periodic task does the remote
        cleanup's. The delete job of the remote cleanup has a task of its own, on the
        cleanup's queue, and its status check, on that queue too, submits it again itself
        when the remote does not have it.
        """
        from plugininstances.tasks import (publish_or_cancel,
                                           run_plugin_instance_cleanup_job,
                                           run_plugin_instance_job)

        if job_class_name == 'PluginInstanceDeleteJob':
            run_plugin_instance_cleanup_job.delay(self.c_plugin_inst.id)
        elif moved_to is None:
            run_plugin_instance_job.delay(self.c_plugin_inst.id, job_class_name)
        else:
            publish_or_cancel(self.c_plugin_inst, moved_to, run_plugin_instance_job.delay,
                              self.c_plugin_inst.id, job_class_name,
                              job_id=self.str_job_id)

    def enqueue_registration(self):
        """
        Have a task register the plugin instance's output files, on the queue of
        registrations: a status check never registers them itself. The caller has just moved
        the instance to 'registeringFiles', where only that task moves it on, so a task that
        cannot be published cancels the instance (publish_or_cancel).
        """
        from plugininstances.tasks import publish_or_cancel, register_plugin_instance_output

        publish_or_cancel(self.c_plugin_inst, 'registeringFiles',
                          register_plugin_instance_output.delay, self.c_plugin_inst.id,
                          job_id=self.str_job_id)
        logger.info(f'Queued the registration of the output files of job {self.str_job_id}: '
                    f'its task, on the register queue or the one it sends it on to, saves '
                    f'the final status')

    def submitted_elsewhere(self, job_type: JobType, kind: str,
                            error: PfconRequestException) -> dict | None:
        """
        After a submission of the ``kind`` job (copy, plugin, upload, data delete) that
        raised ``error``: the remote's status response for the job when another task (or
        an earlier try of this one that timed out) submitted it, which is no failure (a
        warning), or None, with the CODE01 error, when the remote has no such job.
        """
        job_id, pfcon_url = self.str_job_id, self.pfcon_client.url
        d_resp = self.job_exists(job_type)

        if d_resp is None:
            logger.error(f'[CODE01,{job_id}]: Error submitting {kind} job to pfcon url '
                         f'-->{pfcon_url}<--, detail: {str(error)}')
        else:
            logger.warning(f'Submitting {kind} job {job_id} to pfcon url -->{pfcon_url}<-- '
                           f'failed ({str(error)}), but pfcon has the job: another task, '
                           f'or an earlier try of this one, submitted it')
        return d_resp

    def job_exists(self, job_type: JobType) -> dict | None:
        """
        The remote's status response for this job, or None when it has none (or does not
        answer): a submission that failed may still have created the job, as when two
        tasks submitted it at once and the remote refused only the second container.
        """
        try:
            return self._get_status(job_type, self.str_job_id)
        except PfconRequestException:
            return None

    @abc.abstractmethod
    def delete(self):
        """
        Delete the job from the remote compute. It connects to the
        remote compute service to delete the job.
        """
        ...

    @abc.abstractmethod
    def handle_finished_successfully_status(self):
        """
        Handle the 'finishedSuccessfully' status returned by the remote compute.
        """
        ...

    @abc.abstractmethod
    def handle_finished_with_error_status(self):
        """
        Handle the 'finishedWithError' status returned by the remote compute.
        """
        ...

    @abc.abstractmethod
    def handle_undefined_status(self):
        """
        Handle the 'undefined' status returned by the remote compute.
        """
        ...

    def get_job_status_summary(self, d_resp: dict | None = None, 
                               push_path_status: bool | None = None, 
                               pull_path_status: bool | None = None) -> dict:
        """
        Get a job status JSON summary from pfcon response.
        """
        d_jobStatusSummary = self.c_plugin_inst.summary

        if push_path_status is not None:
            d_jobStatusSummary['pushPath']['status'] = push_path_status

        if pull_path_status is not None:
            d_jobStatusSummary['pullPath']['status'] = pull_path_status

        if d_resp is not None:
            d_c = d_resp['compute']

            if d_c['status'] in ('undefined', 'finishedSuccessfully',
                                'finishedWithError'):
                d_jobStatusSummary['compute']['return']['status'] = True

            d_jobStatusSummary['compute']['return']['job_status'] = d_c['status']
            logs = d_jobStatusSummary['compute']['return']['job_logs'] = d_c['logs']

            # truncate logs, assuming worst case where every character needs
            # to be escaped
            if len(logs) > 1800:
                d_jobStatusSummary['compute']['return']['job_logs'] = logs[-1800:]
        return d_jobStatusSummary

    def schedule_remote_cleanup(self, rearm=False):
        """
        Schedule a remote cleanup operation to delete storeBase data and all containers
        from the remote compute environment, for a plugin instance in a terminal status
        whose cleanup has not started (or, with ``rearm``, has ended: a job submitted after
        it). Sets the remote_cleanup_status to 'deletingData' and submits the first
        PluginInstanceDeleteJob. Returns whether this call scheduled it.
        """
        if not self.c_plugin_inst.arm_remote_cleanup(rearm=rearm):
            return False

        self._submit_remote_cleanup()
        return True

    def _submit_remote_cleanup(self):
        logger.info(f'Scheduling remote cleanup for job {self.str_job_id}')
        self.enqueue_run('PluginInstanceDeleteJob')

    def delete_all_remote_containers(self) -> bool:
        """
        Delete all remote containers (copy, plugin, upload, delete) for this plugin
        instance's job from the remote compute environment. Returns True if all
        deletions succeeded, False if any failed.
        """
        job_id = self.str_job_id
        cr = self.c_plugin_inst.compute_resource
        all_succeeded = True
        job_types = []

        if cr.compute_requires_copy_job:
            job_types.append(JobType.COPY)

        job_types.append(JobType.PLUGIN)

        if cr.compute_requires_upload_job:
            job_types.append(JobType.UPLOAD)
            
        job_types.append(JobType.DELETE)

        for job_type in job_types:
            try:
                self._delete(job_type, job_id)
            except PfconRequestException:
                logger.error(f'[CODE12,{job_id}]: Error deleting {job_type} container '
                             f'from pfcon at url -->{self.pfcon_client.url}<--')
                all_succeeded = False
        return all_succeeded
    
    #: what pfcon answers only once it has accepted a request's token (it checks the
    #: token before any job endpoint and refuses a bad one with 401,
    #: ``PfconRequestInvalidTokenException``): no such job, or one that must wait for
    #: another of the instance. Refreshing the token cannot change those answers.
    ANSWERS_AFTER_AUTH = (404, 409)

    def _refresh_compute_resource_auth_token(self):
        """
        Get a new auth token from a remote pfcon service and update the DB.
        """
        cr = self.c_plugin_inst.compute_resource
        token = pfcon.Client.get_auth_token(cr.compute_auth_url, cr.compute_user,
                                            cr.compute_password)
        self.pfcon_client.set_auth_token(token)
        cr.compute_auth_token = token
        cr.save(update_fields=['compute_auth_token'])

    def _submit(self, job_type: JobType, job_id: str, job_descriptors: dict, 
                dfile: io.BytesIO | None = None, timeout: int = 200) -> dict:
        """
        Submit job to a remote pfcon service. The job's description and pfcon's response
        are logged at DEBUG.
        """
        logger.debug('Description of %s job %s: %s', job_type.value, job_id,
                     LogJson(job_descriptors))
        try:
            d_resp = self.pfcon_client.submit_job(job_type, job_id, job_descriptors, 
                                                  dfile, timeout)
        except PfconRequestInvalidTokenException:
            logger.info(f'Auth token has expired while submitting {job_type} job '
                        f'{job_id} to pfcon url -->{self.pfcon_client.url}<--')
            self._refresh_compute_resource_auth_token()
            d_resp = self.pfcon_client.submit_job(job_type, job_id, job_descriptors, 
                                                  dfile, timeout)
        except PfconRequestException as e:
            if self.pfcon_client.requires_copy_job or e.code in self.ANSWERS_AFTER_AUTH:
                raise
            else:  # legacy behavior
                # FIXME HACK
                # Under some conditions, the requests library will produce a
                # "Connection Aborted" error instead of a 401 response. This happens when
                # pfcon responds eagerly to an invalid token and closes the connection.
                # The temporary workaround is to catch a wider range of Exceptions here.
                # Ideally we only want to try again in the event that we know the token is
                # invalid, PfconRequestInvalidTokenException, however this exception
                # is not correctly raised in all the situations where it should be.
                #
                logger.exception(f'Error while submitting {job_type} job {job_id} to pfcon '
                                f'url -->{self.pfcon_client.url}<--, auth token might have '
                                f'expired, will try refreshing token and resubmitting job')
                self._refresh_compute_resource_auth_token()
                d_resp = self.pfcon_client.submit_job(job_type, job_id, job_descriptors, 
                                                    dfile, timeout)
        logger.debug('pfcon response to %s job %s: %s', job_type.value, job_id,
                     LogJson(d_resp))
        return d_resp

    def _get_status(self, job_type: JobType, job_id: str, timeout: int = 100) -> dict:
        """
        Get job status from a remote pfcon service. pfcon's response is logged at DEBUG.
        """
        try:
            d_resp = self.pfcon_client.get_job_status(job_type, job_id, timeout)
        except PfconRequestInvalidTokenException:
            logger.info(f'Auth token has expired while getting status for {job_type} job '
                        f'{job_id} from pfcon url -->{self.pfcon_client.url}<--')
            self._refresh_compute_resource_auth_token()
            d_resp = self.pfcon_client.get_job_status(job_type, job_id, timeout)
        except PfconRequestException as e:
            if self.pfcon_client.requires_copy_job or e.code in self.ANSWERS_AFTER_AUTH:
                raise
            else:  # legacy behavior
                # FIXME HACK
                # Same as in above method.
                #
                logger.exception(f'Error while getting status for {job_type} job {job_id} '
                                f'from pfcon url -->{self.pfcon_client.url}<--, auth token '
                                f'might have expired, will try refreshing token and '
                                f'resubmitting job status request')
                self._refresh_compute_resource_auth_token()
                d_resp = self.pfcon_client.get_job_status(job_type, job_id, timeout)
        logger.debug('pfcon status response for %s job %s: %s', job_type.value, job_id,
                     LogJson(d_resp))
        return d_resp

    def _delete(self, job_type: JobType, job_id: str, timeout: int = 200):
        """
        Delete a job from a remote pfcon service.
        """
        try:
            self.pfcon_client.delete_job(job_type, job_id, timeout)
        except PfconRequestInvalidTokenException:
            logger.info(f'Auth token has expired while requesting to delete {job_type} '
                        f'job {job_id} from pfcon url -->{self.pfcon_client.url}<--')
            self._refresh_compute_resource_auth_token()
            self.pfcon_client.delete_job(job_type, job_id, timeout)
        except PfconRequestException as e:
            if self.pfcon_client.requires_copy_job or e.code in self.ANSWERS_AFTER_AUTH:
                raise
            else:  # legacy behavior
                # FIXME HACK (for legacy storage)
                # Same as in above method.
                #
                logger.exception(f'Error while requesting to delete {job_type} job {job_id} '
                                f'from pfcon url -->{self.pfcon_client.url}<--, auth token '
                                f'might have expired, will try refreshing token and '
                                f'resubmitting job delete request')
                self._refresh_compute_resource_auth_token()
                self.pfcon_client.delete_job(job_type, job_id, timeout)

    def _job_has_timeout(self) -> bool:
        """
        Check if a job has timed out. If the associated job's execution time exceeds
        the maximum set for the remote compute environment then the job has timed out.
        """
        max_job_exec_sec = self.c_plugin_inst.compute_resource.max_job_exec_seconds

        if max_job_exec_sec >= 0:
            delta_exec_time = timezone.now() - self.c_plugin_inst.start_date
            delta_seconds = delta_exec_time.total_seconds()

            if delta_seconds > max_job_exec_sec:
                return True
        return False
