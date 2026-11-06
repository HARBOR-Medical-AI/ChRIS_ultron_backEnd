"""
Copy job manager module that provides the interface for submitting and
checking the execution status of copy jobs running in a remote compute 
environment (ChRIS / pfcon interface) as well as deleting them when finished.
"""

import logging
import os
import time
import json

from pfconclient.client import JobType
from pfconclient.exceptions import PfconRequestException

from django.utils import timezone
from django.conf import settings

from core.utils import json_zip2str
from core.models import  ChrisFile
from plugininstances.models import PluginInstance
from .abstractjobs import PluginInstanceJob
from .pluginjobs import PluginInstanceAppJob


logger = logging.getLogger(__name__)


class PluginInstanceCopyJob(PluginInstanceJob):
    """
    ``PluginInstanceCopyJob`` provides a concrete implementation for managing a remote 
    copy job related to a plugin instance.
    """

    def __init__(self, plugin_instance):
        super().__init__(plugin_instance)
        self.l_plugin_inst_param_instances = self.c_plugin_inst.get_parameter_instances()

    def run(self):
        """
        Run the plugin instance copy job via a call to a remote pfcon service.
        """
        if self.c_plugin_inst.status == 'cancelled':
            return

        job_id = self.str_job_id        
        plugin = self.c_plugin_inst.plugin
        plugin_type = plugin.meta.type
        inputdirs = []

        _, d_path_params = self.get_plugin_instance_path_parameters()
        for path_param_value in [param_value for param_value in d_path_params.values()]:
            # the value of each parameter of type 'path' is a string
            # representing a comma-separated list of paths in obj storage
            inputdirs = inputdirs + path_param_value.split(',')

        try:
            if plugin_type == 'ds':
                inputdirs.append(self.get_previous_output_path())
        except Exception as e:
            logger.error(f'[CODE01,{job_id}]: Error creating copy job, detail: {str(e)}')
            self.c_plugin_inst.status = 'cancelled'  # giving up
            self.c_plugin_inst.error_code = 'CODE01'
            self.c_plugin_inst.save(update_fields=['status', 'error_code'])
            self.schedule_remote_cleanup()
            return

        output_dir = self.c_plugin_inst.get_output_path()

        # create job description dictionary
        job_descriptors = {
            'cpu_limit': self.c_plugin_inst.cpu_limit,
            'memory_limit': self.c_plugin_inst.memory_limit,
            'input_dirs': inputdirs,
            'output_dir': output_dir
        }

        if self.storage_env in ('filesystem', 'fslink'):
            # remote pfcon requires both the input and output dirs to exist
            os.makedirs(os.path.join(settings.MEDIA_ROOT, output_dir), exist_ok=True)

        pfcon_url = self.pfcon_client.url

        logger.info(f'Submitting copy job {job_id} to pfcon url -->{pfcon_url}<--, '
                    f'description: {json.dumps(job_descriptors, indent=4)}')
        try:
            d_resp = self._submit(JobType.COPY, job_id, job_descriptors)
        except PfconRequestException as e:
            logger.error(f'[CODE01,{job_id}]: Error submitting copy job to pfcon url '
                         f'-->{pfcon_url}<--, detail: {str(e)}')
            
            self.c_plugin_inst.copy_retry_count += 1

            if self.c_plugin_inst.copy_retry_count > PluginInstance.MAX_COPY_RETRIES:
                self.c_plugin_inst.error_code = 'CODE01'
                self.c_plugin_inst.status = 'cancelled'  # giving up
                self.c_plugin_inst.save(update_fields=['status', 'error_code'])
                self.schedule_remote_cleanup()
            else:
                self.c_plugin_inst.save(update_fields=['copy_retry_count'])
                self.run()  # retry
        else:
            logger.info(f'Successfully submitted copy job {job_id} to pfcon url '
                        f'-->{pfcon_url}<--, response: {json.dumps(d_resp, indent=4)}')
            
            # update the job status and summary
            self.c_plugin_inst.summary = self.get_job_status_summary(d_resp)
            self.c_plugin_inst.raw = json_zip2str(d_resp)

            # https://github.com/HARBOR-Medical-AI/ChRIS_ultron_backEnd/issues/408
            now = timezone.now()
            self.c_plugin_inst.start_date = now
            self.c_plugin_inst.end_date = now
            self.c_plugin_inst.save()

    def check_exec_status(self):
        """
        Check a plugin instance copy job's execution status. If the associated job's  
        execution time exceeds the maximum set for the remote compute environment then 
        the job is cancelled. Otherwise the job's execution status is fetched from the 
        remote.
        """
        if self.c_plugin_inst.status == 'copying':
            job_id = self.str_job_id

            if self._job_has_timeout():
                logger.error(f'[CODE13,{job_id}]: Error, copy job exceeded maximum '
                             f'execution time')
                self.c_plugin_inst.error_code = 'CODE13'
                self.cancel_exec()
                return self.c_plugin_inst.status

            pfcon_url = self.pfcon_client.url
            logger.info(f'Sending job status request to pfcon url -->{pfcon_url}<-- for '
                        f'copy job {job_id}')
            try:
                d_resp = self._get_status(JobType.COPY, job_id)
            except PfconRequestException as e:
                logger.error(f'[CODE02,{job_id}]: Error getting copy job status at pfcon '
                             f'url -->{pfcon_url}<--, detail: {str(e)}')
                return self.c_plugin_inst.status  # return, periodic task will retry later

            logger.info(f'Successful job status response from pfcon url -->{pfcon_url}<--'
                        f' for copy job {job_id}: {json.dumps(d_resp, indent=4)}')

            status = d_resp['compute']['status']
            logger.info(f'Current copy job {job_id} remote status = {status}')
            logger.info(f'Current copy job {job_id} plugin instance DB status = '
                        f'{self.c_plugin_inst.status}')

            summary = self.get_job_status_summary(d_resp)
            self.c_plugin_inst.summary = summary
            raw = json_zip2str(d_resp)
            self.c_plugin_inst.raw = raw

            # only update (atomically) if still in copy phase to avoid concurrency problems
            PluginInstance.objects.filter(
                id=self.c_plugin_inst.id,
                status=self.c_plugin_inst.status).update(summary=summary, raw=raw)

            if status == 'finishedSuccessfully':
                self.handle_finished_successfully_status()
            elif status == 'finishedWithError':
                self.handle_finished_with_error_status()
            elif status == 'undefined':
                self.handle_undefined_status()
        return self.c_plugin_inst.status

    def cancel_exec(self):
        """
        Cancel a plugin instance copy job execution. It connects to the remote service
        to cancel job and schedules remote cleanup.
        """
        self.c_plugin_inst.status = 'cancelled'
        self.c_plugin_inst.save(update_fields=['status'])
        self.schedule_remote_cleanup()

    def delete(self):
        """
        Delete a plugin instance copy job from the remote compute. It connects to the
        remote service to delete the job.
        """
        pfcon_url = self.pfcon_client.url
        job_id = self.str_job_id
        logger.info(f'Deleting copy job {job_id} from pfcon at url '
                    f'-->{pfcon_url}<--')
        try:
            self._delete(JobType.COPY, job_id)
        except PfconRequestException as e:
            logger.error(f'[CODE12,{job_id}]: Error deleting copy job from '
                             f'pfcon at url -->{pfcon_url}<--, detail: {str(e)}')
            self.c_plugin_inst.error_code = 'CODE12'
        else:
            logger.info(f'Successfully deleted copy job {job_id} from pfcon at '
                        f'url -->{pfcon_url}<--')
            if self.c_plugin_inst.error_code == 'CODE12':
                self.c_plugin_inst.error_code = ''
        
    def get_previous_output_path(self):
        """
        Get the previous plugin instance output directory. Make sure to deal with
        the eventual consistency.
        """
        job_id = self.str_job_id
        output_path = self.c_plugin_inst.previous.get_output_path()
        prefix = output_path + '/'  # avoid sibling folders with paths that start with path

        set_fnames = {f.fname.name for f in ChrisFile.objects.filter(
            fname__startswith=prefix)}

        for i in range(20):  # loop to deal with eventual consistency
            try:
                l_ls = self.storage_manager.ls(output_path)
            except Exception as e:
                logger.error(f'[CODE06,{job_id}]: Error while listing storage files '
                             f'in {output_path}, detail: {str(e)}')
            else:
                if set_fnames.issubset(set(l_ls)):
                    return output_path
            time.sleep(3)

        logger.error(f'[CODE11,{job_id}]: Error while listing storage files in '
                     f'{output_path}, detail: Presumable eventual consistency problem')

        self.c_plugin_inst.error_code = 'CODE11'
        raise NameError('Presumable eventual consistency problem.')

    def get_plugin_instance_path_parameters(self):
        """
        Get the unextpath and path parameters dictionaries in a tuple. The keys and
        values in these dictionaries are parameters' flag and value respectively.
        """
        path_parameters_dict = {}
        unextpath_parameters_dict = {}

        for param_inst in self.l_plugin_inst_param_instances:
            param = param_inst.plugin_param
            value = param_inst.value

            if param.type == 'unextpath':
                unextpath_parameters_dict[param.flag] = value

            if param.type == 'path':
                path_parameters_dict[param.flag] = value
        return unextpath_parameters_dict, path_parameters_dict

    def handle_finished_successfully_status(self):
        """
        Handle the 'finishedSuccessfully' status returned by the remote compute.
        """
        job_id = self.str_job_id
        
        logger.info(f'Successfully finished plugin instance copy job {job_id}')
        
        # data successfully copied so update instance summary
        self.c_plugin_inst.summary['pushPath']['status'] = True
        now = timezone.now()
        self.c_plugin_inst.start_date = now  # save scheduling date
        self.c_plugin_inst.end_date = now
        self.c_plugin_inst.status = 'scheduled'
        self.c_plugin_inst.save()
        plg_inst_app_job = PluginInstanceAppJob(self.c_plugin_inst)
        plg_inst_app_job.run()

    def handle_finished_with_error_status(self):
        """
        Handle the 'finishedWithError' status returned by the remote compute.
        """
        job_id = self.str_job_id

        logger.error(f'[CODE18,{job_id}]: Error while running copy job, remote compute '
                     f'returned finishedWithError status for job {job_id}')
        
        self.c_plugin_inst.status = 'cancelled'
        self.c_plugin_inst.error_code = 'CODE18'
        self.c_plugin_inst.save(update_fields=['status', 'error_code'])
        self.schedule_remote_cleanup()

    def handle_undefined_status(self):
        """
        Handle the 'undefined' status returned by the remote compute.
        """
        self.c_plugin_inst.copy_retry_count += 1

        if self.c_plugin_inst.copy_retry_count > PluginInstance.MAX_COPY_RETRIES:
            job_id = self.str_job_id

            logger.error(f'[CODE18,{job_id}]: Error while running copy job, remote '
                        f'compute returned undefined status for job {job_id} and '
                        f'exceeded max copy retries')
            
            self.c_plugin_inst.status = 'cancelled'
            self.c_plugin_inst.error_code = 'CODE18'
            self.c_plugin_inst.save(update_fields=['status', 'error_code'])
            self.schedule_remote_cleanup()
        else:
            self.c_plugin_inst.save(update_fields=['copy_retry_count'])
            self.run()  # retry
