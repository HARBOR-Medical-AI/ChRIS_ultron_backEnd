"""
Upload job manager module that provides the interface for submitting and
checking the execution status of upload jobs running in a remote compute
environment (ChRIS / pfcon interface) as well as deleting them when finished.
"""

import logging

from pfconclient.client import JobType
from pfconclient.exceptions import PfconRequestException

from django.utils import timezone

from core.utils import json_zip2str
from plugininstances.models import PluginInstance
from .abstractjobs import PluginInstanceJob


logger = logging.getLogger(__name__)


class PluginInstanceUploadJob(PluginInstanceJob):
    """
    ``PluginInstanceUploadJob`` provides a concrete implementation for managing a remote
    upload job related to a plugin instance.
    """

    status = 'uploading'

    def run(self):
        """
        Run the plugin instance upload job via a call to a remote pfcon service.
        """
        if not self.is_runnable():
            return

        job_id = self.str_job_id

        # create job description dictionary
        job_descriptors = {
            'cpu_limit': self.c_plugin_inst.cpu_limit,
            'memory_limit': self.c_plugin_inst.memory_limit,
            'job_output_path': self.c_plugin_inst.get_output_path()
        }

        pfcon_url = self.pfcon_client.url

        logger.info(f'Submitting upload job {job_id} to pfcon url -->{pfcon_url}<--')
        try:
            d_resp = self._submit(JobType.UPLOAD, job_id, job_descriptors)
        except PfconRequestException as e:
            d_resp = self.submitted_elsewhere(JobType.UPLOAD, 'upload', e)

            if d_resp is None:
                retries = self.c_plugin_inst.increment('upload_retry_count')

                if retries > PluginInstance.MAX_UPLOAD_RETRIES:
                    self.end('cancelled', 'uploading', 'CODE01')  # giving up
                else:
                    self.run()  # retry
                return
        else:
            logger.info(f'Successfully submitted upload job {job_id} to pfcon url '
                        f'-->{pfcon_url}<--')

        # update the job status and summary
        self.submitted(summary=self.get_job_status_summary(d_resp),
                       raw=json_zip2str(d_resp))

    def check_exec_status(self):
        """
        Check a plugin instance upload job's execution status. If the associated job's
        execution time exceeds the maximum set for the remote compute environment then
        the job is cancelled. Otherwise the job's execution status is fetched from the
        remote.
        """
        if self.c_plugin_inst.status == 'uploading':
            job_id = self.str_job_id

            if self._job_has_timeout():
                logger.error(f'[CODE13,{job_id}]: Error, upload job exceeded maximum '
                             f'execution time')
                return self.cancel_exec('CODE13', 'uploading').status

            pfcon_url = self.pfcon_client.url
            logger.info(f'Sending job status request to pfcon url -->{pfcon_url}<-- for '
                        f'upload job {job_id}')
            try:
                d_resp = self._get_status(JobType.UPLOAD, job_id)
            except PfconRequestException as e:
                logger.error(f'[CODE02,{job_id}]: Error getting upload job status at pfcon '
                             f'url -->{pfcon_url}<--, detail: {str(e)}')
                return self.c_plugin_inst.status  # return, periodic task will retry later

            status = d_resp['compute']['status']
            logger.info('Current upload job %s remote status = %s %s', job_id, status,
                        d_resp['compute'].get('message') or '')
            logger.info(f'Current upload job {job_id} plugin instance DB status = '
                        f'{self.c_plugin_inst.status}')

            # only while uploading: another task may have moved it on meanwhile
            written = self.c_plugin_inst.update_if_status(
                'uploading', summary=self.get_job_status_summary(d_resp),
                raw=json_zip2str(d_resp))
            if not written:
                return written.status

            if status == 'finishedSuccessfully':
                self.handle_finished_successfully_status()
            elif status == 'finishedWithError':
                self.handle_finished_with_error_status()
            elif status == 'undefined':
                self.handle_undefined_status()
        return self.c_plugin_inst.status

    def delete(self):
        """
        Delete a plugin instance upload job from the remote compute. It connects to the
        remote service to delete the job.
        """
        pfcon_url = self.pfcon_client.url
        job_id = self.str_job_id
        logger.info(f'Deleting upload job {job_id} from pfcon at url '
                    f'-->{pfcon_url}<--')
        try:
            self._delete(JobType.UPLOAD, job_id)
        except PfconRequestException as e:
            logger.error(f'[CODE12,{job_id}]: Error deleting upload job from '
                             f'pfcon at url -->{pfcon_url}<--, detail: {str(e)}')
            self.c_plugin_inst.error_code = 'CODE12'
        else:
            logger.info(f'Successfully deleted upload job {job_id} from pfcon at '
                        f'url -->{pfcon_url}<--')
            if self.c_plugin_inst.error_code == 'CODE12':
                self.c_plugin_inst.error_code = ''

    def handle_finished_successfully_status(self):
        """
        Handle the 'finishedSuccessfully' status returned by the remote compute: the task
        that moves the instance on, to 'registeringFiles', has a task of its own register
        the files the upload job fetched (the registration asks the remote how the plugin
        job ended).
        """
        # the sweep of stuck registrations counts from end_date, so it starts here
        if self.c_plugin_inst.transition('registeringFiles', 'uploading',
                                         end_date=timezone.now()):
            logger.info(f'Successfully finished plugin instance upload job '
                        f'{self.str_job_id}')
            self.enqueue_registration()

    def handle_finished_with_error_status(self):
        """
        Handle the 'finishedWithError' status returned by the remote compute.
        """
        job_id = self.str_job_id

        logger.error(f'[CODE18,{job_id}]: Error while running upload job, remote compute '
                     f'returned finishedWithError status for job {job_id}')

        self.end('cancelled', 'uploading', 'CODE18')

    def handle_undefined_status(self):
        """
        Handle the 'undefined' status returned by the remote compute.
        """
        retries = self.c_plugin_inst.increment('upload_retry_count')

        if retries > PluginInstance.MAX_UPLOAD_RETRIES:
            job_id = self.str_job_id

            logger.error(f'[CODE18,{job_id}]: Error while running upload job, remote '
                        f'compute returned undefined status for job {job_id} and '
                        f'exceeded max upload retries')

            self.end('cancelled', 'uploading', 'CODE18')
        else:
            self.enqueue_run('PluginInstanceUploadJob')  # resubmit
