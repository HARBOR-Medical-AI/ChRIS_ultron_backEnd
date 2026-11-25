"""
Delete job manager module that provides the interface for submitting and
checking the execution status of data delete jobs running in a remote compute
environment (ChRIS / pfcon interface) as well as deleting them when finished.
"""

import logging

from pfconclient.client import JobType
from pfconclient.exceptions import PfconRequestException

from core.utils import json_zip2str
from plugininstances.models import PluginInstance
from .abstractjobs import PluginInstanceJob


logger = logging.getLogger(__name__)


class PluginInstanceDeleteJob(PluginInstanceJob):
    """
    ``PluginInstanceDeleteJob`` provides a concrete implementation for managing a remote
    data delete job related to a plugin instance. This job runs AFTER the plugin instance
    has reached a terminal status. It must NEVER modify ``self.c_plugin_inst.status``.
    It uses ``remote_cleanup_status`` as its state tracker, which it moves on only from the
    value it expects. A failure is counted, and the periodic task handle_remote_cleanup
    checks it again at its next pass: a check that finds the job not submitted, gone,
    failed or undefined, or whose status request fails, submits it again, once.
    """

    def run(self):
        """
        Run the plugin instance data delete job via a call to a remote pfcon service.
        """
        if self.c_plugin_inst.remote_cleanup_status != 'deletingData':
            return

        job_id = self.str_job_id
        pfcon_url = self.pfcon_client.url

        logger.info(f'Submitting data delete job {job_id} to pfcon url '
                    f'-->{pfcon_url}<--')
        try:
            d_resp = self._submit(JobType.DELETE, job_id, {})
        except PfconRequestException as e:
            if e.code == 409:
                # a job of this instance is still running in the remote (a cancel does not
                # stop it): try again once it has ended, without using up a retry
                logger.info(f'Data delete job {job_id} has to wait for the instance\'s '
                            f'other jobs at pfcon url -->{pfcon_url}<--: {str(e)}')
                return

            # the check's submission and the run task's can race: the remote then refuses
            # the second container, which is no failure
            if self.submitted_elsewhere(JobType.DELETE, 'data delete', e) is None:
                self._count_failure()
            return

        logger.info(f'Successfully submitted data delete job {job_id} to pfcon url '
                    f'-->{pfcon_url}<--')

        # update the job summary
        self.c_plugin_inst.move_remote_cleanup(
            'deletingData', 'deletingData', summary=self.get_job_status_summary(d_resp),
            raw=json_zip2str(d_resp))

        # with no data of the job's to delete the remote ends it at once
        if d_resp['compute']['status'] == 'finishedSuccessfully':
            self.handle_finished_successfully_status()

    def check_exec_status(self):
        """
        Check a plugin instance data delete job's execution status. The job's execution
        status is fetched from the remote. Uses retry count instead of timeout to bound
        retries.
        """
        if self.c_plugin_inst.remote_cleanup_status != 'deletingData':
            return self.c_plugin_inst.remote_cleanup_status

        job_id = self.str_job_id
        pfcon_url = self.pfcon_client.url

        logger.info(f'Sending job status request to pfcon url -->{pfcon_url}<-- for '
                    f'delete job {job_id}')
        try:
            d_resp = self._get_status(JobType.DELETE, job_id)
        except PfconRequestException as e:
            if e.code == 404:
                # every minute while a cancelled instance's job runs on: no error
                logger.info(f'Delete job {job_id} is not at pfcon url -->{pfcon_url}<-- '
                            f'(not submitted yet, or gone): submitting it')
            else:
                logger.error(f'[CODE02,{job_id}]: Error getting delete job status at '
                             f'pfcon url -->{pfcon_url}<--, detail: {str(e)}')
            self.run()
            return self.c_plugin_inst.remote_cleanup_status

        status = d_resp['compute']['status']
        logger.info('Current delete job %s remote status = %s %s', job_id, status,
                    d_resp['compute'].get('message') or '')

        # only while deleting data: another task may have moved it on meanwhile
        if not self.c_plugin_inst.move_remote_cleanup(
                'deletingData', 'deletingData', summary=self.get_job_status_summary(d_resp),
                raw=json_zip2str(d_resp)):
            return self.c_plugin_inst.remote_cleanup_status

        if status == 'finishedSuccessfully':
            self.handle_finished_successfully_status()
        elif status == 'finishedWithError':
            self.handle_finished_with_error_status()
        elif status == 'undefined':
            self.handle_undefined_status()

        return self.c_plugin_inst.remote_cleanup_status

    def cancel_exec(self, error_code='', source=None):
        """
        Cancel a plugin instance data delete job execution: its cleanup fails. Does NOT
        modify plugin instance status.
        """
        self.complete('failed')

    def delete(self):
        """
        Delete a plugin instance data delete job from the remote compute. It connects to
        the remote service to delete the job.
        """
        pfcon_url = self.pfcon_client.url
        job_id = self.str_job_id
        logger.info(f'Deleting data delete job {job_id} from pfcon at url '
                    f'-->{pfcon_url}<--')
        try:
            self._delete(JobType.DELETE, job_id)
        except PfconRequestException as e:
            logger.error(f'[CODE12,{job_id}]: Error deleting data delete job from '
                             f'pfcon at url -->{pfcon_url}<--, detail: {str(e)}')
            self.c_plugin_inst.error_code = 'CODE12'
        else:
            logger.info(f'Successfully deleted data delete job {job_id} from pfcon at '
                        f'url -->{pfcon_url}<--')
            if self.c_plugin_inst.error_code == 'CODE12':
                self.c_plugin_inst.error_code = ''

    def handle_finished_successfully_status(self):
        """
        Handle the 'finishedSuccessfully' status returned by the remote compute.
        Transitions to 'deletingContainers' phase and attempts to delete all remote
        containers.
        """
        job_id = self.str_job_id
        logger.info(f'Delete data job {job_id} finished successfully')

        if not self.c_plugin_inst.move_remote_cleanup('deletingContainers', 'deletingData'):
            return

        if self.delete_all_remote_containers():
            self.complete('complete')
        # else: stays 'deletingContainers' for periodic task retry

    def handle_finished_with_error_status(self):
        """
        Handle the 'finishedWithError' status returned by the remote compute.
        Submits the delete job again if under max retries, otherwise marks as failed.
        """
        job_id = self.str_job_id
        logger.error(f'[CODE18,{job_id}]: Error while running delete data job, remote '
                     f'compute returned finishedWithError status for job {job_id}')

        if not self._count_failure():
            self.run()

    def handle_undefined_status(self):
        """
        Handle the 'undefined' status returned by the remote compute.
        Submits the delete job again if under max retries, otherwise marks as failed.
        """
        job_id = self.str_job_id
        logger.error(f'[CODE18,{job_id}]: Error while running delete data job, remote '
                     f'compute returned undefined status for job {job_id}')

        if not self._count_failure():
            self.run()

    def complete(self, final):
        """
        End the remote cleanup with ``final`` ('complete' or 'failed'), from either of its
        running phases, after what comes with it, which is safe to do twice and so goes
        first (a task killed in the middle leaves the cleanup running, for the periodic task
        to end): delete the output of a cancelled instance whose remote job is no longer
        running, and grant the feed's sharing permissions on the instance's output folder.
        Returns whether this call ended it.
        """
        running = ['deletingData', 'deletingContainers']
        job_id = self.str_job_id
        instance = PluginInstance.objects.filter(pk=self.c_plugin_inst.pk)

        if not instance.filter(remote_cleanup_status__in=running).exists():
            return False

        if instance.filter(status='cancelled').exists() and not self._plugin_job_active():
            self._cleanup_plugin_instance_output_dir()

        logger.info(f"Setting output folder's permissions for job {job_id} ...")
        self._grant_feed_permissions()

        if not self.c_plugin_inst.move_remote_cleanup(final, running):
            return False

        # as the database has it now: a cancel may have come after this task read it
        status = instance.values_list('status', flat=True).first()
        logger.info(f"Remote cleanup for job {job_id} ended as {final}; the plugin "
                    f"instance's final status is '{status}'")
        return True

    def _plugin_job_active(self):
        """
        Whether the instance's plugin job may still be running in the remote: unless the
        remote says it has ended, or does not know it, its output folder stays.
        """
        try:
            d_resp = self._get_status(JobType.PLUGIN, self.str_job_id)
        except PfconRequestException as e:
            return e.code != 404
        return d_resp['compute']['status'] in ('notStarted', 'started')

    def _grant_feed_permissions(self):
        """
        Set the plugin instance's output folder permissions recursively from its feed's
        sharing.
        """
        feed = self.c_plugin_inst.feed
        folder = self.c_plugin_inst.output_folder.parent

        for group in feed.shared_groups.all():
            folder.grant_group_permission(group, 'w')

        for user in feed.shared_users.all():
            folder.grant_user_permission(user, 'w')

        if feed.public:
            folder.grant_public_access()

    def _count_failure(self):
        """
        Count a failure of the remote cleanup and, past the max retries, mark it as failed.
        Returns whether it failed.
        """
        retries = self.c_plugin_inst.increment('remote_cleanup_retry_count')

        if retries <= PluginInstance.MAX_REMOTE_CLEANUP_RETRIES:
            return False

        logger.error(f'Delete job for plugin instance {self.c_plugin_inst.id} '
                     f'exceeded max retries, marking cleanup as failed')
        self.complete('failed')
        return True

    def _cleanup_plugin_instance_output_dir(self):
        """
        Clean up the plugin instance output directory by deleting all files and folders
        under it, their rows in one deletion and their objects at once
        (``ChrisFolder.delete_contents``). This is needed to remove any orphan files that
        might be left in the output dir. A directory that is not in storage is no error:
        one cancelled before its job ran was never created (pfcon then skips the delete
        job and the cleanup comes here at once), and a cleanup run again finds it deleted.
        """
        self.c_plugin_inst.output_folder.delete_contents()
