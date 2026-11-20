
from typing import List, Dict
from collections import deque

from rest_framework import generics, permissions
from rest_framework.reverse import reverse
from drf_spectacular.utils import extend_schema, extend_schema_view

from collectionjson import services
from pipelines.models import Pipeline
from plugininstances.models import PluginInstance, PARAMETER_MODELS
from plugininstances.serializers import PluginInstanceSerializer
from plugininstances.tasks import PublishError
from plugininstances.utils import cancel_plugin_instances, run_if_ready
from ._types import (GivenNodeInfo, WorkflowPluginInstanceTemplateFactory,
                     WorkflowPluginInstanceTemplate, PipingId)
from .models import Workflow, WorkflowFilter
from .permissions import IsOwnerOrChrisOrReadOnly
from .serializers import WorkflowSerializer

@extend_schema_view(
    get=extend_schema(operation_id="workflows_list")
)
class WorkflowList(generics.ListCreateAPIView):
    """
    A view for the collection of pipeline-specific workflows.
    """
    http_method_names = ['get', 'post']
    serializer_class = WorkflowSerializer
    queryset = Pipeline.objects.all()
    permission_classes = (permissions.IsAuthenticated,)

    def perform_create(self, serializer):
        """
        Overriden to associate a pipeline and owner with the newly created workflow
        before first saving to the DB. All the workflow's parameters in the request are
        parsed and properly saved to the DB with the corresponding plugin instances.
        """
        previous_plugin_inst = serializer.validated_data['previous_plugin_inst_id']
        nodes_info: List[GivenNodeInfo] = serializer.validated_data['nodes_info']
        pipeline = self.get_object()
        
        title = self._resolve_workflow_title(serializer, pipeline)

        workflow = serializer.save(owner=self.request.user, pipeline=pipeline,
                                   title=title)

        tree, root_id, inst_data = self._build_inst_templates(pipeline, nodes_info)

        plugin_instances_dict = self._create_plugin_instance_tree(
            tree, root_id, inst_data, previous_plugin_inst, workflow)
        
        self._dispatch_runs(plugin_instances_dict)

    @staticmethod
    def _resolve_workflow_title(serializer, pipeline) -> str:
        """Return the workflow title, falling back to ``pipeline.name`` when omitted."""
        title = serializer.validated_data.get('title')
        return pipeline.name if title is None else title

    @staticmethod
    def _build_inst_templates(pipeline, nodes_info: List[GivenNodeInfo]):
        """
        Build the per-piping :class:`WorkflowPluginInstanceTemplate` map from
        ``nodes_info``. Returns ``(tree, root_id, inst_data)`` where ``tree`` and
        ``root_id`` come from :meth:`Pipeline.get_pipings_tree` and ``inst_data``
        is keyed by piping id.
        """
        pipings_tree = pipeline.get_pipings_tree()
        tree = pipings_tree['tree']

        factory = WorkflowPluginInstanceTemplateFactory(tree=tree)

        inst_data: Dict[PipingId, WorkflowPluginInstanceTemplate] = {
            info['piping_id']: factory.inflate(info)
            for info in nodes_info
        }
        return tree, pipings_tree['root_id'], inst_data

    def _create_plugin_instance_tree(self, tree, root_id, inst_data, previous,
                                     workflow) -> Dict[PipingId, PluginInstance]:
        """
        Create plugin instances for every node in ``tree`` via breadth-first
        traversal so each child's ``previous`` is its parent's freshly-created
        plugin instance. Returns a piping-id -> plugin-instance map.
        """
        root_inst = self.create_plugin_inst(inst_data[root_id], previous, workflow)
        plugin_instances_dict: Dict[PipingId, PluginInstance] = {root_id: root_inst}

        pip_id_queue = deque([root_id])
        plugin_inst_queue = deque([root_inst])

        while pip_id_queue:
            curr_id = pip_id_queue.popleft()
            curr_plugin_inst = plugin_inst_queue.popleft()

            for child_id in tree[curr_id]['child_ids']:
                child_inst = self.create_plugin_inst(
                    inst_data[child_id], curr_plugin_inst, workflow)
                
                plugin_instances_dict[child_id] = child_inst

                pip_id_queue.append(child_id)
                plugin_inst_queue.append(child_inst)
        return plugin_instances_dict

    def _dispatch_runs(self,
                       plugin_instances_dict: Dict[PipingId, PluginInstance]) -> None:
        """
        Rewrite the ``plugininstances`` param of every ``ts`` plugin instance from
        piping-id refs to plugin-instance-id refs, then submit each instance, parents
        first, via :func:`run_if_ready`. When a run task cannot be published
        (``PublishError``: its instance is cancelled with CODE01), the instances still in
        'created' are cancelled too before the error goes on, as no periodic task looks
        at that status.
        """
        instances = list(plugin_instances_dict.values())

        for plg_inst in instances:
            if plg_inst.plugin.meta.type == 'ts':
                self._rewrite_ts_parent_ids(plg_inst, plugin_instances_dict)
        try:
            for plg_inst in instances:
                run_if_ready(plg_inst, plg_inst.previous)
        except PublishError:
            cancel_plugin_instances(instances, source='created')
            raise

    @staticmethod
    def _rewrite_ts_parent_ids(
            plg_inst: PluginInstance,
            plugin_instances_dict: Dict[PipingId, PluginInstance]) -> None:
        """
        Translate the comma-separated ``plugininstances`` string param of a ``ts``
        plugin instance from piping ids (as stored on the pipeline) to the actual
        plugin-instance ids created for this workflow.
        """
        param = plg_inst.string_param.filter(plugin_param__name='plugininstances').first()
        if not (param and param.value):
            return
        
        parent_pip_ids = [int(pip_id) for pip_id in param.value.split(',')]

        parent_plg_inst_ids = [str(plugin_instances_dict[pip_id].id)
                               for pip_id in parent_pip_ids]
        
        param.value = ','.join(parent_plg_inst_ids)
        param.save()

    def list(self, request, *args, **kwargs):
        """
        Overriden to return the list of workflows for the queried pipeline.
        A document-level link relation and a collection+json template are also added to
        the response.
        """
        queryset = self.get_workflows_queryset()
        response = services.get_list_response(self, queryset)
        pipeline = self.get_object()

        # append document-level link relations
        links = {'pipeline': reverse('pipeline-detail', request=request,
                                     kwargs={"pk": pipeline.id})}
        response = services.append_collection_links(response, links)

        # append write template
        template_data = {'previous_plugin_inst_id': '', 'title': '', 'nodes_info': ''}
        return services.append_collection_template(response, template_data)

    def get_workflows_queryset(self):
        """
        Custom method to get the actual workflows' queryset.
        """
        pipeline = self.get_object()
        return Workflow.add_jobs_status_count(pipeline.workflows.all())

    def create_plugin_inst(self, data: WorkflowPluginInstanceTemplate, previous:
    PluginInstance, workflow: Workflow) -> PluginInstance:
        """
        Custom method to create a plugin instance and set its parameters.
        """
        plg_inst = PluginInstance.objects.create(
            plugin=data.piping.plugin,
            owner=self.request.user,
            previous=previous,
            title=data.title,
            compute_resource=data.compute_resource,
            workflow=workflow,
            cpu_limit=data.cpu_limit,
            memory_limit=data.memory_limit,
            number_of_workers=data.number_of_workers,
            gpu_limit=data.gpu_limit,
        )
        for plugin_param, value in data.params:
            PARAMETER_MODELS[plugin_param.type].objects.create(
                plugin_inst=plg_inst,
                plugin_param=plugin_param,
                value=value
            )
        return plg_inst


@extend_schema_view(
    get=extend_schema(operation_id="all_workflows_list")
)
class AllWorkflowList(generics.ListAPIView):
    """
    A view for the collection of all workflows.
    """
    http_method_names = ['get']
    serializer_class = WorkflowSerializer
    queryset = Workflow.add_jobs_status_count(Workflow.objects.all())
    permission_classes = (permissions.IsAuthenticated,)

    def list(self, request, *args, **kwargs):
        """
        Overriden to add a query list and document-level link relation to the response.
        """
        response = super(AllWorkflowList, self).list(request, *args, **kwargs)

        # append query list
        query_list = [reverse('allworkflow-list-query-search', request=request)]
        response = services.append_collection_querylist(response, query_list)

        # append document-level link relations
        links = {'pipelines': reverse('pipeline-list', request=request)}
        return services.append_collection_links(response, links)


class AllWorkflowListQuerySearch(generics.ListAPIView):
    """
    A view for the collection of workflows resulting from a query search.
    """
    http_method_names = ['get']
    serializer_class = WorkflowSerializer
    queryset = Workflow.add_jobs_status_count(Workflow.objects.all())
    permission_classes = (permissions.IsAuthenticated,)
    filterset_class = WorkflowFilter


class WorkflowDetail(generics.RetrieveUpdateDestroyAPIView):
    """
    A workflow view.
    """
    http_method_names = ['get', 'put', 'delete']
    serializer_class = WorkflowSerializer
    queryset = Workflow.add_jobs_status_count(Workflow.objects.all())
    permission_classes = (permissions.IsAuthenticated, IsOwnerOrChrisOrReadOnly,)

    def retrieve(self, request, *args, **kwargs):
        """
        Overriden to append a collection+json template to the response.
        """
        response = super(WorkflowDetail, self).retrieve(request, *args, **kwargs)
        template_data = {'title': ''}
        return services.append_collection_template(response, template_data)

    def destroy(self, request, *args, **kwargs):
        """
        Overriden to cancel all the associated plugin instances before attempting to
        delete the workflow.
        """
        workflow = self.get_object()
        cancel_plugin_instances(workflow.plugin_instances.all())
        return super(WorkflowDetail, self).destroy(request, *args, **kwargs)


class WorkflowPluginInstanceList(generics.ListAPIView):
    """
    A view for the collection of plugin instances that compose the workflow.
    """
    http_method_names = ['get']
    serializer_class = PluginInstanceSerializer
    queryset = Workflow.objects.all()
    permission_classes = (permissions.IsAuthenticated,)

    def list(self, request, *args, **kwargs):
        """
        Overriden to return a list with the plugin instances that are part of the
        queried workflow and that the user can see.
        """
        queryset = self.get_plugin_instances_queryset()
        return services.get_list_response(self, queryset)

    def get_plugin_instances_queryset(self):
        """
        Custom method to get a queryset with the plugin instances that are part of the
        queried workflow and that the user can see. Any authenticated user can read a
        workflow, but its plugin instances belong to a feed that may not be shared with
        the user.
        """
        workflow = self.get_object()
        plg_inst_qs = PluginInstance.filter_visible_to_user(
            workflow.plugin_instances.all(), self.request.user)
        return self.filter_queryset(PluginInstance.add_serializer_relations(plg_inst_qs))
