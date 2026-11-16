"""
Plugin job manager module that provides the interface for submitting and
checking the execution status of plugin jobs running in a remote compute 
environment (ChRIS / pfcon interface) as well as deleting them when finished.
"""

import logging
import os
import re
import io
import secrets
import time
import zipfile
from typing import List, Optional

from celery.exceptions import SoftTimeLimitExceeded

from pfconclient.client import JobType
from pfconclient.exceptions import (PfconRequestException,
                                    PfconRequestInvalidTokenException)

from django.utils import timezone
from django.conf import settings
from django.db import IntegrityError, transaction
from rest_framework.authtoken.models import Token

from core.utils import json_zip2str
from core.models import (ChrisFolder, ChrisFile, ChrisLinkFile, PathAccessError,
                          validate_path_access)
from plugininstances.models import PluginInstance, PluginInstanceLock
from userfiles.models import UserFile
from .abstractjobs import ARM_REMOTE_CLEANUP, PluginInstanceJob


logger = logging.getLogger(__name__)


class PluginInstanceAppJob(PluginInstanceJob):
    """
    ``PluginInstanceAppJob`` provides a concrete implementation for managing a remote 
    app job related to a plugin instance.
    """

    status = 'scheduled'

    def __init__(self, plugin_instance):
        super().__init__(plugin_instance)
        self.l_plugin_inst_param_instances = self.c_plugin_inst.get_parameter_instances()
        # the token of the registration this object runs, the link files it wrote and how
        # many output files it registered
        self._registration_token = None
        self._links_written = set()
        self._files_registered = 0

    def run(self):
        """
        Run the plugin instance app job via a call to a remote pfcon service.
        """
        if not self.is_runnable():
            return

        job_id = self.str_job_id
        plugin = self.c_plugin_inst.plugin
        plugin_type = plugin.meta.type
        inputdirs = []

        d_unextpath_params, d_path_params = self.get_plugin_instance_path_parameters()
        for path_param_value in [param_value for param_value in d_path_params.values()]:
            # the value of each parameter of type 'path' is a string
            # representing a comma-separated list of paths in obj storage
            inputdirs = inputdirs + path_param_value.split(',')

        try:
            if plugin_type == 'ds':
                inputdirs.append(self.get_previous_output_path())
            elif not inputdirs and not self.pfcon_client.requires_copy_job:
                inputdirs.append(self.manage_empty_inputdir())
        except Exception as e:
            logger.error(f'[CODE01,{job_id}]: Error creating plugin job, detail: {str(e)}')
            self.end('cancelled', 'scheduled', 'CODE01')  # giving up
            return

        # create job description dictionary
        job_descriptors = {
            'entrypoint': self._assemble_exec(plugin.selfpath, plugin.selfexec, plugin.execshell),
            'args': self.get_cmd_args(),
            'args_path_flags': [*d_unextpath_params.keys(), *d_path_params.keys()],
            'auid': self.c_plugin_inst.owner.username,
            'number_of_workers': self.c_plugin_inst.number_of_workers,
            'cpu_limit': self.c_plugin_inst.cpu_limit,
            'memory_limit': self.c_plugin_inst.memory_limit,
            'gpu_limit': self.c_plugin_inst.gpu_limit,
            'image': plugin.dock_image,
            'type': plugin_type,
            'env': self._compute_env_vars()
        }

        job_zip_file_content = None
        job_timeout = 200

        if self.pfcon_client.pfcon_innetwork:
            output_dir = self.c_plugin_inst.get_output_path()

            job_descriptors['input_dirs'] = inputdirs
            job_descriptors['output_dir'] = output_dir

            if self.storage_env in ('filesystem', 'fslink'):
                # remote pfcon requires both the input and output dirs to exist
                os.makedirs(os.path.join(settings.MEDIA_ROOT, output_dir), exist_ok=True)
        else:
            # create zip file to transmit
            try:
                job_zip_file = self.create_zip_file(inputdirs)
            except Exception:
                self.end('cancelled', 'scheduled', 'CODE01')  # giving up
                return
            job_zip_file_content = job_zip_file.getvalue()
            job_timeout = 9000

        pfcon_url = self.pfcon_client.url
        logger.info(f'Submitting plugin job {job_id} to pfcon url -->{pfcon_url}<--')
        try:
            d_resp = self._submit(JobType.PLUGIN, job_id, job_descriptors, 
                                  job_zip_file_content, job_timeout)
        except PfconRequestException as e:
            d_resp = self.submitted_elsewhere(JobType.PLUGIN, 'plugin', e)

            if d_resp is None:
                self.end('cancelled', 'scheduled', 'CODE01')  # giving up
                return
        else:
            logger.info(f'Successfully submitted plugin job {job_id} to pfcon url '
                        f'-->{pfcon_url}<--')

        # update the job status and summary
        # https://github.com/HARBOR-Medical-AI/ChRIS_ultron_backEnd/issues/408
        now = timezone.now()
        fields = {'raw': json_zip2str(d_resp), 'start_date': now, 'end_date': now}

        if not self.pfcon_client.requires_copy_job:
            # initial status
            fields['summary'] = self.get_job_status_summary(d_resp=d_resp,
                                                            push_path_status=True)
        self.submitted('started', **fields)

    @staticmethod
    def _assemble_exec(selfpath: Optional[str], selfexec: str, execshell: Optional[str]) -> List[str]:
        entrypoint_path = selfexec
        if selfpath:
            entrypoint_path = os.path.join(selfpath, selfexec)
        if not execshell:
            return [entrypoint_path]
        return [execshell, entrypoint_path]

    def _compute_env_vars(self):
        """
        Helper method to compute a list of environment variables to be injected into
        the remote plugin instance app's container.
        """
        job_id = self.str_job_id
        plugin_inst = self.c_plugin_inst
        plugin = plugin_inst.plugin
        plugin_type = plugin.meta.type
        username = plugin_inst.owner.username
        email = plugin_inst.owner.email

        token, _ = Token.objects.get_or_create(user=plugin_inst.owner)

        env = [f'CHRIS_JID={job_id}', f'CHRIS_PLG_INST_ID={plugin_inst.id}',
               f'CHRIS_USER_USERNAME={username}', f'CHRIS_USER_EMAIL={email}',
               f'CHRIS_USER_TOKEN={token.key}']

        if plugin_type != 'fs':
            prev_id = plugin_inst.previous.id
            env.append(f'CHRIS_PREV_PLG_INST_ID={prev_id}')
            env.append(f'CHRIS_PREV_JID={self.str_job_id_prefix + str(prev_id)}')

        if plugin_inst.workflow:
            workflow_instances_info = ''
            for inst in plugin_inst.workflow.plugin_instances.all():
                workflow_instances_info += f'{inst.title}:{inst.id},'
            env.append(f'CHRIS_WORKFLOW_ID={plugin_inst.workflow.id}')
            env.append(f'CHRIS_PIPELINE_ID={plugin_inst.workflow.pipeline.id}')
            env.append(f'CHRIS_WORKFLOW_PLG_INSTANCES={workflow_instances_info[:-1]}')
        return env

    def check_exec_status(self):
        """
        Check a plugin instance app job's execution status. If the job has timed out then
        the job is cancelled. Otherwise the job's execution status is fetched from the
        remote and, once the job has ended (with or without errors), the instance moves
        on: an upload job, or a task of its own that registers the output files.
        """
        if self.c_plugin_inst.status == 'started':
            job_id = self.str_job_id

            if self._job_has_timeout():
                logger.error(f'[CODE13,{job_id}]: Error, plugin job exceeded maximum '
                             f'execution time')
                return self.cancel_exec('CODE13', 'started').status

            pfcon_url = self.pfcon_client.url
            logger.info(f'Sending job status request to pfcon url -->{pfcon_url}<-- for '
                        f'plugin job {job_id}')
            try:
                d_resp = self._get_status(JobType.PLUGIN, job_id)
            except PfconRequestException as e:
                logger.error(f'[CODE02,{job_id}]: Error getting plugin job status at '
                             f'pfcon url -->{pfcon_url}<--, detail: {str(e)}')
                return self.c_plugin_inst.status  # return, periodic task will retry later

            status = d_resp['compute']['status']
            logger.info('Current plugin job %s remote status = %s %s', job_id, status,
                        d_resp['compute'].get('message') or '')
            logger.info(f'Current plugin job {job_id} plugin instance DB status = '
                        f'{self.c_plugin_inst.status}')

            # only while started: another task may have moved it on meanwhile
            written = self.c_plugin_inst.update_if_status(
                'started', summary=self.get_job_status_summary(d_resp),
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
        Delete a plugin instance app job from the remote compute. It connects to the
        remote service to delete the job.
        """
        pfcon_url = self.pfcon_client.url
        job_id = self.str_job_id
        logger.info(f'Deleting job {job_id} from pfcon at url '
                    f'-->{pfcon_url}<--')
        try:
            self._delete(JobType.PLUGIN, job_id)
        except PfconRequestException as e:
            logger.error(f'[CODE12,{job_id}]: Error deleting job from '
                             f'pfcon at url -->{pfcon_url}<--, detail: {str(e)}')
            self.c_plugin_inst.error_code = 'CODE12'
        else:
            logger.info(f'Successfully deleted job {job_id} from pfcon at '
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

    def get_cmd_args(self) -> List[str]:
        app_args = []

        # append the parameters to app's argument list
        for param_inst in self.l_plugin_inst_param_instances:
            param = param_inst.plugin_param
            value = param_inst.value

            if param.action == 'store':
                app_args.append(param.flag)
                app_args.append(str(value))  # convert all argument values to string
            elif param.action == 'store_true' and value:
                app_args.append(param.flag)
            elif param.action == 'store_false' and not value:
                app_args.append(param.flag)
        return app_args

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

    def get_ts_plugin_instance_parameters(self):
        """
        Get the 'ts' plugin's special parameters in a tuple: the ids of its input plugin
        instances, the regular expressions that filter their objects and the
        'groupByInstance' flag (no ids, no expressions and False for another plugin).
        """
        plg_inst_ids = regexs = []
        group_by_instance = False

        if self.c_plugin_inst.plugin.meta.type == 'ts':
            for param_inst in self.l_plugin_inst_param_instances:
                if param_inst.plugin_param.name == 'plugininstances':
                    # string param that represents a comma-separated list of ids
                    plg_inst_ids = param_inst.value.split(',') if param_inst.value else []
                elif param_inst.plugin_param.name == 'filter':
                    # string param that represents a comma-separated list of regular expr
                    regexs = param_inst.value.split(',') if param_inst.value else []
                elif param_inst.plugin_param.name == 'groupByInstance':
                    group_by_instance = param_inst.value
        return plg_inst_ids, regexs, group_by_instance

    def get_ts_plugin_instance_input_objs(self):
        """
        Get a tuple whose first element is a dictionary with keys that are the ids of
        each input plugin instance to this 'ts' plugin instance. The values of
        this dictionary are also dictionaries containing the output dir of the plugin
        instances and the list of all the objects under the output dir that match a
        regular expression. The second element of the tuple indicates the value of the
        'groupByInstance' flag for this 'ts' plugin instance.
        """
        job_id = self.str_job_id
        plg_inst_ids, regexs, group_by_instance = self.get_ts_plugin_instance_parameters()

        d_objs = {}
        for i, inst_id in enumerate(plg_inst_ids):
            try:
                plg_inst = PluginInstance.objects.get(pk=int(inst_id))
            except PluginInstance.DoesNotExist:
                logger.error(f"[CODE05,{job_id}]: Couldn't find any plugin instance with "
                             f"id {inst_id} while processing input instances to 'ts' "
                             f"plugin instance with id {self.c_plugin_inst.id}")
                self.c_plugin_inst.error_code = 'CODE05'
                raise

            output_path = plg_inst.get_output_path()
            all_obj_paths = set()
            visited_paths = set()
            self.find_all_storage_object_paths(output_path, all_obj_paths, visited_paths)

            if (i < len(regexs)) and regexs[i]:
                r = re.compile(regexs[i])
                d_objs[plg_inst.id] = {'output_path': output_path,
                                       'objs': [obj for obj in all_obj_paths if
                                                r.search(obj)]}
            else:
                d_objs[plg_inst.id] = {'output_path': output_path,
                                       'objs': list(all_obj_paths)}
        return d_objs, group_by_instance

    def manage_empty_inputdir(self):
        """
        This method is responsible for managing the 'inputdir' in the case of
        FS and TS plugins. FS and TS plugins do not have an inputdir spec, since this
        is only a requirement for DS plugins. Nonetheless, the remote services do
        require some non-zero inputdir spec in order to operate correctly.

        The hack here is to store data somewhere in storage and accessing it as a
        "pseudo" inputdir for FS and TS plugins. We create a "dummy" inputdir with a
        small dummy text file in storage. This is then transmitted as an 'inputdir'
        to the compute environment and can be completely ignored by the plugin.
        """
        job_id = self.str_job_id
        data_dir = os.path.join(os.path.expanduser("~"), 'data')

        str_inputdir = os.path.join(data_dir, 'squashEmptyDir').lstrip('/')
        str_squashFile = os.path.join(str_inputdir, 'squashEmptyDir.txt')
        str_squashMsg = 'Empty input dir.'

        try:
            if not self.storage_manager.obj_exists(str_squashFile):
                with io.StringIO(str_squashMsg) as f:
                    self.storage_manager.upload_obj(str_squashFile, f.read(),
                                                    content_type='text/plain')
        except Exception as e:
            logger.error(f'[CODE07,{job_id}]: Error while uploading file '
                         f'{str_squashFile} to storage, detail: {str(e)}')
            self.c_plugin_inst.error_code = 'CODE07'
            raise
        return str_inputdir

    def _check_path_access(self, linked_path):
        """
        Raise a ``ValueError`` (and set the CODE17 error code) if the plugin
        instance owner is not allowed to access the given path. Authorization is
        delegated to ``core.models.validate_path_access`` so the exact
        same rule enforced at job submission time is re-enforced at runtime when
        ChRIS link files are followed or created (link files can point anywhere,
        so they must be re-checked).
        """
        try:
            validate_path_access(self.c_plugin_inst.owner, linked_path)
        except PathAccessError:
            job_id = self.str_job_id
            logger.error(
                f'[CODE17,{job_id}]: Found unauthorized input path {linked_path} '
                f'that the plugin instance owner is not allowed to access')
            self.c_plugin_inst.error_code = 'CODE17'
            raise ValueError(f'Invalid input path: {linked_path}')

    def find_all_storage_object_paths(self, storage_path, obj_paths, visited_paths):
        """
        Find all object storage paths from the passed storage path (prefix) by
        recursively following ChRIS links. The resulting set of object paths is given
        by the obj_paths set argument.
        """
        if not storage_path.startswith(tuple(visited_paths)):  # avoid infinite loops
            visited_paths.add(storage_path)
            job_id = self.str_job_id
            output_dir = self.c_plugin_inst.get_output_path()

            try:
                l_ls = self.storage_manager.ls(storage_path)
            except Exception as e:
                logger.error(f'[CODE06,{job_id}]: Error while listing storage files '
                             f'in {storage_path}, detail: {str(e)}')
                self.c_plugin_inst.error_code = 'CODE06'
                raise

            for obj_path in l_ls:
                if obj_path.endswith('.chrislink'):
                    try:
                        linked_path = self.storage_manager.download_obj(
                            obj_path).decode().strip()
                    except Exception as e:
                        logger.error(f'[CODE08,{job_id}]: Error while downloading file '
                                     f'{obj_path} from storage, detail: {str(e)}')
                        self.c_plugin_inst.error_code = 'CODE08'
                        raise

                    if f'{output_dir}/'.startswith(linked_path.rstrip('/') + '/'):
                        # link files are not allowed to point to the output dir or any
                        # of its ancestors
                        logger.error(
                            f'[CODE17,{job_id}]: Found invalid input path {linked_path} '
                            f'pointing to an ancestor of the output dir: '
                            f'{output_dir}')
                        self.c_plugin_inst.error_code = 'CODE17'
                        raise ValueError(f'Invalid input path: {linked_path}')

                    # link files can point anywhere, so re-enforce the access rule
                    # to prevent following links into unauthorized data
                    self._check_path_access(linked_path)

                    self.find_all_storage_object_paths(linked_path, obj_paths,
                                                       visited_paths)  # recursive call
                obj_paths.add(obj_path)

    def create_zip_file(self, storage_paths):
        """
        Create job zip file ready for transmission to the remote from a list of storage
        paths (prefixes).
        """
        job_id = self.str_job_id
        memory_zip_file = io.BytesIO()
        all_obj_paths = set()

        with zipfile.ZipFile(memory_zip_file, 'w', zipfile.ZIP_DEFLATED) as job_data_zip:
            for storage_path in storage_paths:
                obj_paths = set()
                visited_paths = set()

                self.find_all_storage_object_paths(storage_path, obj_paths,
                                                   visited_paths)
                for obj_path in obj_paths:
                    if obj_path not in all_obj_paths:  # add a file to the zip only once
                        try:
                            contents = self.storage_manager.download_obj(obj_path)
                        except Exception as e:
                            logger.error(f'[CODE08,{job_id}]: Error while downloading file '
                                         f'{obj_path} from storage, detail: {str(e)}')
                            self.c_plugin_inst.error_code = 'CODE08'
                            raise
                        
                        zip_path = obj_path.replace(storage_path, '', 1).lstrip('/')
                        job_data_zip.writestr(zip_path, contents)
                        all_obj_paths.add(obj_path)

        memory_zip_file.seek(0)
        return memory_zip_file

    def unpack_zip_file(self, zip_file_content):
        """
        Unpack job zip file from the remote into storage.
        """
        job_id = self.str_job_id
        try:
            memory_zip_file = io.BytesIO(zip_file_content)
            with zipfile.ZipFile(memory_zip_file, 'r', zipfile.ZIP_DEFLATED) as job_zip:
                filenames = job_zip.namelist()
                logger.info(f'{len(filenames)} files to decompress for job {job_id}')
                output_path = self.c_plugin_inst.get_output_path() + '/'

                for fname in filenames:
                    content = job_zip.read(fname)
                    storage_fname = output_path + fname.lstrip('/')
                    try:
                        self.storage_manager.upload_obj(storage_fname, content)
                    except SoftTimeLimitExceeded:
                        raise
                    except Exception as e:
                        logger.error(f'[CODE07,{job_id}]: Error while uploading file '
                                     f'{storage_fname} to storage, detail: {str(e)}')
                        self.c_plugin_inst.error_code = 'CODE07'
                        raise ValueError(str(e))
                    self.plugin_inst_output_files.add(storage_fname)
        except (ValueError, SoftTimeLimitExceeded):
            raise
        except Exception as e:
            logger.error(f'[CODE04,{job_id}]: Received bad zip file from remote, '
                         f'detail: {str(e)}')
            self.c_plugin_inst.error_code = 'CODE04'
            raise

    def check_files_from_json_exist(self, json_file_content):
        """
        Check whether all files listed in the job json file from the remote indeed
        exist in storage.
        """
        job_id = self.str_job_id
        plg_inst_output_path = self.c_plugin_inst.get_output_path()
        job_output_path = json_file_content['job_output_path']

        if job_output_path != plg_inst_output_path:
            err_msg = f'Received {job_output_path} != {plg_inst_output_path} output path'
            logger.error(f'[CODE16,{job_id}]: Inconsistency between received '
                         f'JSON file and storage, detail: {err_msg}')
            self.c_plugin_inst.error_code = 'CODE16'
            raise ValueError(err_msg)

        files_from_json = set([os.path.join(job_output_path, p) for p in
                               json_file_content['rel_file_paths']])

        for i in range(60):  # list until every file is there, 60 times at most, 1 s apart
            try:
                files_in_storage = set(self.storage_manager.ls(job_output_path))
            except SoftTimeLimitExceeded:
                raise
            except Exception as e:
                logger.error(f'[CODE15,{job_id}]: Error while listing storage files '
                             f'in {job_output_path}, detail: {str(e)}')
                self.c_plugin_inst.error_code = 'CODE15'
                raise

            if files_from_json.issubset(files_in_storage):
                break
            if i == 59:
                nmissing = len(files_from_json.difference(files_in_storage))
                err_msg = f'Missing {nmissing} files in storage'
                logger.error(f'[CODE14,{job_id}]: Inconsistency between received '
                             f'JSON file and storage, detail: {err_msg}')
                self.c_plugin_inst.error_code = 'CODE14'
                raise ValueError(err_msg)
            time.sleep(1)
        self.plugin_inst_output_files = files_from_json

    def _handle_unextpath_parameters(self, unextpath_parameters_dict):
        """
        Internal method to handle parameters of type 'unextpath' passed to the
        plugin instance app job.
        """
        output_dir: str = self.c_plugin_inst.get_output_path()
        owner = self.c_plugin_inst.owner

        (link_parent_folder, _) = ChrisFolder.objects.get_or_create(
            path=output_dir, owner=owner)

        for param_flag in unextpath_parameters_dict:
            # each parameter value is a string of one or more paths separated by comma
            path_list = unextpath_parameters_dict[param_flag].split(',')

            for path in path_list:
                self._create_chris_link_file(path, link_parent_folder)

    def _create_chris_link_file(self, linked_path, parent_folder):
        """
        Internal method to create output ChRIS link files generated for the plugin
        instance.

        NOTE:
        Full storage path names are given to the link file names, allowing
        for each copy argument to have its own link file in the output directory.

        NB: This preservation could exhaust DB string lengths!
        """
        job_id: str = self.str_job_id
        output_dir: str = self.c_plugin_inst.get_output_path()
        owner = self.c_plugin_inst.owner
        path = linked_path.rstrip('/')

        if f'{output_dir}/'.startswith(path + '/'):
            # paths are not allowed to point to the output dir or any
            # of its ancestors
            logger.error(f'[CODE17,{job_id}]: Found invalid input path {linked_path} '
                         f'pointing to an ancestor of the output dir: '
                         f'{output_dir}')
            self.c_plugin_inst.error_code = 'CODE17'
            raise ValueError(f'Invalid input path: {linked_path}')

        # re-enforce the access rule: only paths the plugin instance owner is
        # authorized to access may be turned into output link files
        self._check_path_access(path)

        # another link to the same path in the same folder (from two parents' outputs) is
        # this one: written and counted once
        if (path, parent_folder.path) in self._links_written:
            return
        self._links_written.add((path, parent_folder.path))

        str_source_trace_dir = path.replace('/', '_')
        try:
            ChrisFolder.objects.get(path=path)
        except ChrisFolder.DoesNotExist:
            try:
                if self.storage_manager.obj_exists(path):  # path is a file
                    link_file = self._link_file(path, owner, parent_folder,
                                                str_source_trace_dir)
                    link_file.save(name=str_source_trace_dir)
                    logger.info(f'Creating link file -->'
                                f'{link_file.fname.name}<-- for job {job_id}')
                    self.c_plugin_inst.size += link_file.fname.size
            except SoftTimeLimitExceeded:
                raise
            except Exception as e:
                logger.error(f'[CODE09,{job_id}]: Error while creating link file '
                             f'to {path} from {parent_folder.path} in storage, '
                             f'detail: {str(e)}')
                self.c_plugin_inst.error_code = 'CODE09'
                raise
        else:  # path is a folder
            try:
                link_file = self._link_file(path, owner, parent_folder,
                                            str_source_trace_dir)
                link_file.save(name=str_source_trace_dir)
                logger.info(f'Creating link file -->'
                            f'{link_file.fname.name}<-- for job {job_id}')
                self.c_plugin_inst.size += link_file.fname.size
            except SoftTimeLimitExceeded:
                raise
            except Exception as e:
                logger.error(f'[CODE09,{job_id}]: Error while creating link file '
                             f'to {path} from {parent_folder.path} in storage, '
                             f'detail: {str(e)}')
                self.c_plugin_inst.error_code = 'CODE09'
                raise

    @staticmethod
    def _link_file(path, owner, parent_folder, name):
        """
        The link file named ``name`` in ``parent_folder`` to ``path``: the one an earlier
        run of the registration created, whose storage object saving it writes again
        (pfcon's fslink storage removes every link file from an output folder it lists),
        or a new one. Raises when that name links another path: two paths whose names
        come out the same.
        """
        fname = os.path.join(parent_folder.path, f'{name}.chrislink')
        link_file = ChrisLinkFile.objects.filter(fname=fname).first()

        if link_file is None:
            return ChrisLinkFile(path=path, owner=owner, parent_folder=parent_folder)

        if link_file.path != path:
            raise ValueError(f'{fname} links {link_file.path}, not {path}')
        return link_file

    def _handle_unextpath_parameters_innetwork_filesystem(self, unextpath_parameters_dict):
        """
        Internal method to handle parameters of type 'unextpath' passed to the
        plugin instance app job.

        NOTE:
        Full storage path names are now preserved in the copy process, allowing
        for each copy argument to be preserved in its own directory tree in the
        destination.

        NB: This preservation could exhaust DB string lengths!
        """
        job_id: str = self.str_job_id
        outputdir: str = self.c_plugin_inst.get_output_path()

        for param_flag in unextpath_parameters_dict:
            # each parameter value is a string of one or more paths separated by comma
            path_list = unextpath_parameters_dict[param_flag].split(',')

            for path in path_list:
                try:
                    obj_list = self.storage_manager.ls(path)
                except SoftTimeLimitExceeded:
                    raise
                except Exception as e:
                    logger.error(f'[CODE06,{job_id}]: Error while listing storage files '
                                 f'in {path}, detail: {str(e)}')
                    self.c_plugin_inst.error_code = 'CODE06'
                    raise

                if len(obj_list) == 1 and obj_list[0] == path:  # path was a file
                    path = os.path.dirname(path)
                    str_source_trace_dir = path.replace('/', '_')
                else:
                    str_source_trace_dir = path.rstrip('/').replace('/', '_')

                obj_output_path_prefix = outputdir + '/' + str_source_trace_dir

                for obj in obj_list:
                    # Uncomment the following to fire up a trace event, accessible via
                    #                   telnet localhot 6900
                    # Note, you might need to change the term_size on an ad-hoc manner
                    # set_trace(host = "0.0.0.0", port = 6900, term_size = (223, 60))
                    obj_output_path = obj_output_path_prefix + '/' + obj.replace(
                        path, '', 1).lstrip('/')
                    try:
                        if not self.storage_manager.obj_exists(obj_output_path):
                            self.storage_manager.copy_obj(obj, obj_output_path)
                    except SoftTimeLimitExceeded:
                        raise
                    except Exception as e:
                        logger.error(f'[CODE09,{job_id}]: Error while copying file '
                                     f'from {obj} to {obj_output_path} in storage, '
                                     f'detail: {str(e)}')
                        self.c_plugin_inst.error_code = 'CODE09'
                        raise
                    self.plugin_inst_output_files.add(obj_output_path)

    def _handle_ts_unextracted_input_objs(self, d_ts_input_objs, group_by_instance):
        """
        Internal method to handle a 'ts' plugin's input instances' filtered objects
        (which are not extracted from object storage).
        """
        job_id = self.str_job_id
        outputdir = self.c_plugin_inst.get_output_path()
        owner = self.c_plugin_inst.owner
        link_folders = {}

        for plg_inst_id in d_ts_input_objs:
            plg_inst_output_path = d_ts_input_objs[plg_inst_id]['output_path']
            obj_list = d_ts_input_objs[plg_inst_id]['objs']
            plg_inst_outputdir = outputdir

            if group_by_instance:
                plg_inst_outputdir = os.path.join(outputdir, str(plg_inst_id))

            for obj in obj_list:
                obj_output_path = os.path.join(plg_inst_outputdir, obj.replace(
                    plg_inst_output_path, '', 1).lstrip('/'))

                if obj.endswith('.chrislink'):
                    try:
                        path = self.storage_manager.download_obj(obj).decode().strip()
                    except SoftTimeLimitExceeded:
                        raise
                    except Exception as e:
                        logger.error(f'[CODE08,{job_id}]: Error while downloading file '
                                     f'{obj} from storage, detail: {str(e)}')
                        self.c_plugin_inst.error_code = 'CODE08'
                        raise

                    folder_path = os.path.dirname(obj_output_path)
                    parent_folder = link_folders.get(folder_path)

                    if parent_folder is None:
                        (parent_folder, _) = ChrisFolder.objects.get_or_create(
                            path=folder_path,
                            owner=owner)
                        link_folders[folder_path] = parent_folder

                    # also the one an earlier run created: the remote removed its file
                    self._create_chris_link_file(path, parent_folder)
                else:
                    try:
                        if not self.storage_manager.obj_exists(obj_output_path):
                            self.storage_manager.copy_obj(obj, obj_output_path)
                    except SoftTimeLimitExceeded:
                        raise
                    except Exception as e:
                        logger.error(f'[CODE09,{job_id}]: Error while copying file '
                                     f'from {obj} to {obj_output_path} in storage, '
                                     f'detail: {str(e)}')
                        self.c_plugin_inst.error_code = 'CODE09'
                        raise
                    self.plugin_inst_output_files.add(obj_output_path)

    def handle_finished_successfully_status(self):
        """
        Handle the 'finishedSuccessfully' status returned by the remote compute.
        """
        self._job_ended()

    def handle_finished_with_error_status(self):
        """
        Handle the 'finishedWithError' status returned by the remote compute.
        """
        self._job_ended()

    def _job_ended(self):
        """
        The remote job has ended, whichever way: the task that moves the instance on, out of
        'started', has its output uploaded (an upload job, on the queue of submissions) or
        registered (the registration asks the remote how the job ended).
        """
        now = timezone.now()

        if self.pfcon_client.requires_upload_job:
            # the upload job fetches the files into CUBE storage, and its status check
            # then registers them
            if self.c_plugin_inst.transition('uploading', 'started', end_date=now):
                self.enqueue_run('PluginInstanceUploadJob')
            return

        # the task that moves it on has a task of its own register the files, on the
        # queue of registrations
        if self.c_plugin_inst.transition('registeringFiles', 'started', end_date=now):
            self.enqueue_registration()

    #: how many times the registration asks the remote how the job ended, a few seconds
    #: apart, before it gives up
    REMOTE_STATUS_TRIES = 3

    def copies_unextpath_objects(self):
        """
        Whether registering the output copies into it every object under the paths of
        the 'unextpath' parameters (an in-network remote with 'filesystem' storage) rather
        than writing a link file per path.
        """
        return self.pfcon_client.pfcon_innetwork and self.storage_env == 'filesystem'

    def registers_at_most(self, max_files):
        """
        Whether registering the output registers ``max_files`` files at most, as far as
        storage tells before the registration fetches anything from the remote. It
        counts the files the plugin wrote (an in-network remote's are in storage already)
        and those the registration adds to them (see register_output_files_on_success):
        for each path of the 'unextpath' parameters, a copy of every object under it or
        one link file (copies_unextpath_objects), and for a 'ts' plugin a copy of every
        object of its input instances' outputs (its filters can only leave some out). It
        counts objects, not bytes. Each listing asks for one object more than the count
        has room for, so the answer costs a few requests whatever the output's size.
        False when the remote sends the output as a zip, not in storage yet, and when a
        'ts' input holds a link file, whose target the registration copies too.
        """
        if not self.pfcon_client.pfcon_innetwork:
            return False

        unextpath_parameters_dict, _ = self.get_plugin_instance_path_parameters()
        unextpaths = [path for value in unextpath_parameters_dict.values()
                      for path in value.split(',')]
        plg_inst_ids, _, _ = self.get_ts_plugin_instance_parameters()
        inputs = PluginInstance.objects.filter(
            pk__in=[int(inst_id) for inst_id in plg_inst_ids]).select_related(
            'output_folder')

        count = 0
        prefixes = [(self.c_plugin_inst.get_output_path(), False)]
        if self.copies_unextpath_objects():
            prefixes += [(path, False) for path in unextpaths]
        else:
            count += len(unextpaths)
        prefixes += [(inst.get_output_path(), True) for inst in inputs]

        for prefix, is_input in prefixes:
            if count > max_files:
                break
            objs = self.storage_manager.ls(prefix, limit=max_files - count + 1)
            if is_input and any(obj.endswith('.chrislink') for obj in objs):
                return False
            count += len(objs)
        return count <= max_files

    def register_output(self):
        """
        Register the output files of the plugin instance, which is in 'registeringFiles'
        (a status check moved it there when its remote job ended), and end it:
        'finishedSuccessfully' or 'finishedWithError' as the remote says the job ended
        (the latter however far registering its files got), or 'cancelled' when
        registering the files of a job that succeeded failed, or the remote does not say
        the job ended, as long as it is still registering files (a cancel meanwhile
        wins), with its remote cleanup.

        It may run more than once, one after another (a task redelivered after its worker
        died) or at once (redelivered while it ran): each run claims the registration with
        a token of its own, the newest wins, and only the run that owns it writes the final
        status. What a run writes before is the same whatever the runs: files already
        registered are left as they are and link files are written again.
        """
        token = self._registration_token = self._claim_registration()
        started = time.monotonic()
        # the total of what this run registers: a run again counts every file again
        self.c_plugin_inst.size = 0
        self._links_written = set()
        self._files_registered = 0
        remote_status = self._remote_job_status()

        if not self._owns_registration():
            # a newer run or a cancel has taken it meanwhile: no fetch either (on fslink
            # the remote's listing removes the link files of the output folder, which the
            # other run writes)
            final = 'cancelled'
        elif remote_status == 'finishedSuccessfully':
            final = self.register_output_files_on_success()
        elif remote_status == 'finishedWithError':
            final = self.register_output_files_on_error()
        else:
            if remote_status is not None:
                logger.error(f'[CODE10,{self.str_job_id}]: Got {remote_status} status '
                             f'from remote while registering files')
                self.c_plugin_inst.error_code = 'CODE10'
            final = 'cancelled'

        ended = self._end_registration(final, token)

        if ended is None:
            outcome = 'a newer run owns it'
        else:
            outcome = (f'it ends {final}' if ended
                       else f'it is {ended.status} already, which stays')
        logger.info('Registered %d output files and %d link files (%d bytes) of job %s in '
                    '%.1f s: %s', self._files_registered,
                    len(self._links_written), self.c_plugin_inst.size, self.str_job_id,
                    time.monotonic() - started, outcome)

    def give_up_registration(self, error_code):
        """
        Cancel the instance, still registering files, with ``error_code``: its
        registration cannot finish. A run that a newer one has taken over leaves the
        instance to that one. Returns the ``StatusWrite``, or None when it left it.
        """
        token = self._registration_token
        if token is not None and not PluginInstanceLock.objects.filter(
                plugin_inst_id=self.c_plugin_inst.pk, owner_token=token).exists():
            logger.info(f'A newer run of the registration of job {self.str_job_id} owns '
                        f'it: this one cancels nothing')
            return None
        return self.cancel_exec(error_code, 'registeringFiles')

    def _claim_registration(self):
        """
        Make this run the owner of the instance's registration, whose record keeps when
        the first run started, and return the token that says so.
        """
        token = secrets.token_hex(16)
        PluginInstanceLock.objects.update_or_create(plugin_inst_id=self.c_plugin_inst.pk,
                                                    defaults={'owner_token': token})
        return token

    def _remote_job_status(self):
        """
        How the remote says the plugin job ended, or None (with CODE02) when it does not
        answer after REMOTE_STATUS_TRIES tries.
        """
        job_id = self.str_job_id

        for attempt in range(self.REMOTE_STATUS_TRIES):
            try:
                return self._get_status(JobType.PLUGIN, job_id)['compute']['status']
            except PfconRequestException as e:
                logger.error(f'[CODE02,{job_id}]: Error getting plugin job status at '
                             f'pfcon url -->{self.pfcon_client.url}<-- while registering '
                             f'files (try {attempt + 1}), detail: {str(e)}')
                if attempt + 1 < self.REMOTE_STATUS_TRIES:
                    time.sleep(5)

        self.c_plugin_inst.error_code = 'CODE02'
        return None

    def _owns_registration(self):
        """
        Whether this run still owns the registration and the instance still registers
        files: neither a newer run nor a cancel has taken it since its claim.
        """
        return PluginInstance.objects.filter(
            pk=self.c_plugin_inst.pk, status='registeringFiles',
            lock__owner_token=self._registration_token).exists()

    def _end_registration(self, final, token):
        """
        End the instance with ``final``, its size and error code and its remote cleanup
        armed, if this run still owns the registration and the instance is still
        registering files; the final status is then logged and the remote cleanup's first
        job goes out. When a cancel won, its cleanup is armed again: it may have deleted
        the output folder before this run inserted its files. Returns the ``StatusWrite``,
        or None when a newer run owns the registration.
        """
        fields = {'size': self.c_plugin_inst.size, **ARM_REMOTE_CLEANUP}
        if self.c_plugin_inst.error_code:
            fields['error_code'] = self.c_plugin_inst.error_code

        # the claim of a newer run waits for this lock, and this for its claim
        with transaction.atomic():
            owner = PluginInstanceLock.objects.select_for_update().filter(
                plugin_inst_id=self.c_plugin_inst.pk, owner_token=token).first()
            ended = (self.c_plugin_inst.transition(final, 'registeringFiles', **fields)
                     if owner else None)

        if owner is None:
            logger.info(f'A newer run of the registration of job {self.str_job_id} owns '
                        f'it: this one writes nothing')
        elif ended:
            self.log_final_status()
            self._submit_remote_cleanup()
        elif ended.status == 'cancelled':
            self.schedule_remote_cleanup(rearm=True)
        return ended

    def register_output_files_on_success(self):
        """
        Fetch output file data/metadata from pfcon, verify/unpack files in storage,
        handle special path parameters and register all output files in the DB. Returns
        the instance's final status.
        """
        pfcon_url = self.pfcon_client.url
        job_id = self.str_job_id
        logger.info(f'Sending job data file request to pfcon url -->{pfcon_url}<-- '
                    f'for job {job_id}')
        try:
            if self.pfcon_client.pfcon_innetwork:
                job_output_path = self.c_plugin_inst.get_output_path()
                job_file_content = self._get_job_json_data(job_id, job_output_path)
            else:
                job_file_content = self._get_job_zip_data(job_id)
        except PfconRequestException as e:
            logger.error(f'[CODE03,{job_id}]: Error fetching data file from pfcon '
                         f'url -->{pfcon_url}<--, detail: {str(e)}')
            self.c_plugin_inst.error_code = 'CODE03'
            return 'cancelled'  # giving up

        try:
            self._data_fetched()

            if self.pfcon_client.pfcon_innetwork:
                logger.info('Checking that all remote output files for job '
                            '%s exist in file storage', job_id)
                self.check_files_from_json_exist(job_file_content)
            else:
                logger.info('Uploading remote output files for job %s to '
                            'file storage', job_id)
                self.unpack_zip_file(job_file_content)

            logger.info('Copying local output files for job %s in file '
                        'storage', job_id)
            # upload files from unextracted path parameters
            d_unextpath_params, _ = self.get_plugin_instance_path_parameters()
            if d_unextpath_params:
                if self.copies_unextpath_objects():
                    self._handle_unextpath_parameters_innetwork_filesystem(
                        d_unextpath_params)
                else:
                    self._handle_unextpath_parameters(d_unextpath_params)

            # upload files from filtered input instance paths ('ts' plugins)
            if self.c_plugin_inst.plugin.meta.type == 'ts':
                d_ts_input_objs, tf = self.get_ts_plugin_instance_input_objs()
                self._handle_ts_unextracted_input_objs(d_ts_input_objs, tf)

            self._register_output_files()  # register output files in the DB
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            self._log_unreported_failure()
            return 'cancelled'  # giving up
        return 'finishedSuccessfully'

    def _log_unreported_failure(self):
        """
        Log the exception that stopped the registration of the output files, with its
        traceback, unless the step that failed logged it with an error code of its own.
        """
        if not self.c_plugin_inst.error_code:
            logger.exception(f'Error while registering the output files of job '
                             f'{self.str_job_id}')

    def _data_fetched(self):
        """
        Record in the instance's summary that its output data came back, as long as it
        is still registering files.
        """
        summary = self.c_plugin_inst.summary
        summary['pullPath']['status'] = True
        self.c_plugin_inst.update_if_status('registeringFiles', summary=summary)

    def _get_job_json_data(self, job_id, job_output_path, timeout=500):
        """
        Get job json data from a remote in-network pfcon service.
        """
        try:
            json_content = self.pfcon_client.get_plugin_job_json_data(job_id, 
                                                                      job_output_path,
                                                                      timeout)
        except PfconRequestInvalidTokenException:
            logger.info(f'Auth token has expired while getting json data for plugin job'
                        f' {job_id} from pfcon url -->{self.pfcon_client.url}<--')
            self._refresh_compute_resource_auth_token()
            json_content = self.pfcon_client.get_plugin_job_json_data(job_id, 
                                                                      job_output_path,
                                                                      timeout)
        return json_content

    def _get_job_zip_data(self, job_id, timeout=9000):
        """
        Get job zip data from a remote pfcon service.
        """
        try:
            zip_content = self.pfcon_client.get_plugin_job_zip_data(job_id, timeout)
        except PfconRequestInvalidTokenException:
            logger.info(f'Auth token has expired while getting zip data for plugin job '
                        f'{job_id} from pfcon url -->{self.pfcon_client.url}<--')
            self._refresh_compute_resource_auth_token()
            zip_content = self.pfcon_client.get_plugin_job_zip_data(job_id, timeout)
        return zip_content

    def register_output_files_on_error(self):
        """
        Fetch output file data/metadata from pfcon, verify/unpack files in storage and 
        register all output files in the DB, as far as it goes. Returns the instance's
        final status, 'finishedWithError'.
        """
        pfcon_url = self.pfcon_client.url
        job_id = self.str_job_id
        logger.info(f'Sending job data file request to pfcon url -->{pfcon_url}<-- '
                    f'for job {job_id}')
        try:
            if self.pfcon_client.pfcon_innetwork:
                job_output_path = self.c_plugin_inst.get_output_path()
                job_file_content = self._get_job_json_data(job_id, job_output_path)
            else:
                job_file_content = self._get_job_zip_data(job_id)
        except PfconRequestException as e:
            logger.error(f'[CODE03,{job_id}]: Error fetching data file from pfcon '
                            f'url -->{pfcon_url}<--, detail: {str(e)}')
            self.c_plugin_inst.error_code = 'CODE03'
        else:
            try:
                self._data_fetched()

                if self.pfcon_client.pfcon_innetwork:
                    logger.info('Checking that all remote output files for job %s '
                                'exist in file storage', job_id)
                    self.check_files_from_json_exist(job_file_content)
                else:
                    logger.info('Uploading remote output files for job %s to file '
                                'storage', job_id)
                    self.unpack_zip_file(job_file_content)

                self._register_output_files()  # register output files in the DB
            except SoftTimeLimitExceeded:
                raise
            except Exception:
                self._log_unreported_failure()  # giving up
        return 'finishedWithError'

    def handle_undefined_status(self):
        """
        Handle the 'undefined' status returned by the remote compute.
        """
        job_id = self.str_job_id
        logger.error(f'[CODE10,{job_id}]: Got undefined status from remote')
        # from the status the status check checked
        self.cancel_exec('CODE10', self.c_plugin_inst.status)

    def _register_output_files(self):
        """
        Internal method to register output files generated for the plugin instance with
        the DB. Handles special cases:
            - Files with names that only contain commas and white spaces are deleted.
            - Folders with names that only contain commas and white spaces are removed
            after moving their contents to the parent folder.
        """
        job_id = self.str_job_id
        logger.info('Registering output files with job %s', job_id)

        owner = self.c_plugin_inst.owner
        outputdir = self.c_plugin_inst.get_output_path()
        files = []
        folders = {}

        # remove commas from the existing files/folders names and handle the special cases
        changed_file_paths = self.storage_manager.sanitize_obj_names(outputdir)

        for obj_path in self.plugin_inst_output_files:
            if obj_path in changed_file_paths:
                obj_path = changed_file_paths[obj_path]

            if obj_path:
                if obj_path.endswith('.chrislink'):
                    # Plugins are not authorized to produce ChRIS link files.
                    # Any link file coming from the remote compute (zip/json
                    # output) is refused and deleted from storage so subsequent
                    # jobs cannot follow it. Legit link files created by CUBE for
                    # 'unextpath' parameters or 'ts' plugins are registered
                    # directly as ChrisLinkFile objects and never reach this set.
                    logger.warning(f'Refusing to register link file '
                                   f'-->{obj_path}<-- produced by job {job_id}; '
                                   f'deleting it from storage')
                    try:
                        self.storage_manager.delete_obj(obj_path)
                    except SoftTimeLimitExceeded:
                        raise
                    except Exception as e:
                        # a non-deletable attacker-controlled link left in an
                        # output dir is a security-relevant state: fail the job
                        logger.error(f'[CODE07,{job_id}]: Error while deleting '
                                     f'unauthorized link file {obj_path} from '
                                     f'storage, detail: {str(e)}')
                        self.c_plugin_inst.error_code = 'CODE07'
                        raise
                    continue

                folder_path = os.path.dirname(obj_path)
                parent_folder = folders.get(folder_path)

                if parent_folder is None:
                    (parent_folder, _) = ChrisFolder.objects.get_or_create(
                        path=folder_path, owner=owner)
                    folders[folder_path] = parent_folder

                plg_inst_file = UserFile(owner=owner, parent_folder=parent_folder)
                plg_inst_file.fname.name = obj_path
                files.append(plg_inst_file)

        if self._registration_token and not self._owns_registration():
            # a cancel's cleanup may be deleting the output: no rows for its files
            logger.info(f'The registration of job {job_id} was cancelled or taken over: '
                        f'this run registers no files')
            self.plugin_inst_output_files = set()
            return

        self.plugin_inst_output_files = {f.fname.name for f in files}
        db_files = self._insert_files(files)
        self._files_registered = len(db_files)

        total_size = 0
        for plg_inst_file in db_files:
            total_size += plg_inst_file.fname.size
        self.c_plugin_inst.size += total_size

    @staticmethod
    def _insert_files(files):
        """
        Insert the rows of ``files`` and return them. A file that an earlier run of the
        registration registered is left as it is: the INSERT fails at once on it and is
        made again skipping the rows already there. The plain INSERT comes first because
        one that skips conflicting rows checks every row before writing it, which makes
        the insert of a large output about three-quarters longer.
        """
        try:
            with transaction.atomic():
                return UserFile.objects.bulk_create(files)
        except IntegrityError:
            return UserFile.objects.bulk_create(files, ignore_conflicts=True)
