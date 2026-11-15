
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.serializers import ValidationError
from drf_spectacular.utils import extend_schema_view, extend_schema

from collectionjson import services
from plugins.models import Plugin

from .models import (PluginInstance, PluginInstanceFilter, PluginInstanceSplit,
                     StrParameter, FloatParameter, IntParameter, BoolParameter,
                     PathParameter, UnextpathParameter, ACTIVE_STATUSES)
from .serializers import (PARAMETER_SERIALIZERS, GenericParameterSerializer,
                          PluginInstanceSplitSerializer, PluginInstanceSerializer)
from .permissions import (IsOwnerOrChrisOrHasFeedPermissionReadOnlyOrPublicFeedReadOnly,
                          IsNotDeleteFSPluginInstance)
from .tasks import delete_plugin_instance
from .utils import cancel_plugin_instance_tree, cancel_plugin_instances, run_if_ready


@extend_schema_view(
    get=extend_schema(operation_id='plugins_instances_list')
)
class PluginInstanceList(generics.ListCreateAPIView):
    """
    A view for the collection of plugin instances.
    """
    http_method_names = ['get', 'post']
    serializer_class = PluginInstanceSerializer
    queryset = Plugin.objects.all()
    permission_classes = (permissions.IsAuthenticated,)

    def perform_create(self, serializer):
        """
        Overriden to associate an owner, a plugin and a previous plugin instance with 
        the newly created plugin instance before first saving to the DB. All the plugin 
        instance's parameters in the request are also properly saved to the DB. Finally
        the plugin's app is run with the provided plugin instance's parameters.
        """
        user = self.request.user
        # get previous plugin instance and create the new plugin instance
        request_data = serializer.context['request'].data
        previous_id = request_data.get('previous_id')
        previous = serializer.validate_previous(previous_id)
        plugin = self.get_object()

        # collect and validate parameters from the request
        parameter_serializers = []
        for parameter in plugin.parameters.all():
            if parameter.name in request_data:
                data = {'value': request_data[parameter.name]}
                param_type = parameter.type

                if param_type in ('path', 'unextpath'):
                    # these serializers need the user to be passed
                    parameter_serializer = PARAMETER_SERIALIZERS[param_type](
                        data=data, user=user)
                elif param_type == 'string' and plugin.meta.type == 'ts':
                    # these serializers need the param's name, plugin type and previous
                    parameter_serializer = PARAMETER_SERIALIZERS[param_type](
                        data=data, param_name=parameter.name, plugin_type='ts',
                        previous=previous)
                else:
                    parameter_serializer = PARAMETER_SERIALIZERS[param_type](data=data)

                parameter_serializer.is_valid(raise_exception=True)
                parameter_serializers.append((parameter, parameter_serializer))
            elif not parameter.optional:
                raise ValidationError({parameter.name: ["This field is required."]})

        # if no validation errors at this point then save to the DB
        cr_data = serializer.validated_data.get('compute_resource')
        if cr_data:
            compute_resource = plugin.compute_resources.get(name=cr_data['name'])
        else:
            compute_resource = plugin.compute_resources.first()
        plg_inst = serializer.save(owner=user, plugin=plugin, previous=previous,
                                   compute_resource=compute_resource)
        for param, param_serializer in parameter_serializers:
            param_serializer.save(plugin_inst=plg_inst, plugin_param=param)

        run_if_ready(plg_inst, previous)

    def list(self, request, *args, **kwargs):
        """
        Overriden to return the list of instances for the queried plugin. A document-level
        link relation and a collection+json template are also added to the response.
        """
        queryset = self.get_plugin_instances_queryset()
        response = services.get_list_response(self, queryset)
        plugin = self.get_object()

        # append document-level link relations
        links = {'plugin': reverse('plugin-detail', request=request,
                                   kwargs={"pk": plugin.id}),
                 'compute_resources': reverse('plugin-computeresource-list',
                                              request=request, kwargs={"pk": plugin.id})
                 }
        response = services.append_collection_links(response, links)

        # append write template
        param_names = plugin.get_plugin_parameter_names()
        template_data = {'title': '', 'compute_resource_name': '', 'previous_id': '',
                         'cpu_limit': '', 'memory_limit': '', 'number_of_workers': '',
                         'gpu_limit': ''}
        for name in param_names:
            template_data[name] = ''
        return services.append_collection_template(response, template_data)

    def get_plugin_instances_queryset(self):
        """
        Custom method to get the actual plugin instances' queryset.
        """
        user = self.request.user
        plugin = self.get_object()
        return PluginInstance.add_serializer_relations(plugin.instances.filter(owner=user))


@extend_schema_view(
    get=extend_schema(operation_id='all_plugins_instances_list')
)
class AllPluginInstanceList(generics.ListAPIView):
    """
    A view for the collection of all plugin instances.
    """
    http_method_names = ['get']
    serializer_class = PluginInstanceSerializer

    def list(self, request, *args, **kwargs):
        """
        Overriden to add a query list and document-level link relation to the response.
        """
        response = super(AllPluginInstanceList, self).list(request, *args, **kwargs)
        # append query list
        query_list = [reverse('allplugininstance-list-query-search', request=request)]
        response = services.append_collection_querylist(response, query_list)
        # append document-level link relations
        links = {'plugins': reverse('plugin-list', request=request)}
        return services.append_collection_links(response, links)

    def get_queryset(self):
        """
        Overriden to return a custom queryset that is only comprised by the plugin
        instances the user can see.
        """
        user = self.request.user
        return PluginInstance.add_serializer_relations(
            PluginInstance.filter_visible_to_user(PluginInstance.objects.all(), user))


class AllPluginInstanceListQuerySearch(generics.ListAPIView):
    """
    A view for the collection of plugin instances resulting from a query search.
    """
    http_method_names = ['get']
    serializer_class = PluginInstanceSerializer
    filterset_class = PluginInstanceFilter

    def get_queryset(self):
        """
        Overriden to return a custom queryset that is only comprised by the plugin
        instances the user can see.
        """
        user = self.request.user
        return PluginInstance.add_serializer_relations(
            PluginInstance.filter_visible_to_user(PluginInstance.objects.all(), user))


class PluginInstanceDetail(generics.RetrieveUpdateDestroyAPIView):
    """
    A plugin instance view.
    """
    http_method_names = ['get', 'put', 'delete']
    serializer_class = PluginInstanceSerializer
    queryset = PluginInstance.add_serializer_relations(PluginInstance.objects.all())
    permission_classes = (IsOwnerOrChrisOrHasFeedPermissionReadOnlyOrPublicFeedReadOnly,
                          IsNotDeleteFSPluginInstance,)

    def retrieve(self, request, *args, **kwargs):
        """
        Overriden to add a collection+json template to the response.
        """
        response = super(PluginInstanceDetail, self).retrieve(request, *args, **kwargs)
        template_data = {'title': '', 'status': ''}
        return services.append_collection_template(response, template_data)

    def perform_update(self, serializer):
        """
        Overriden to cancel this plugin instance and all its descendants' execution.
        """
        if serializer.validated_data.get('status') == 'cancelled':
            cancelled = cancel_plugin_instance_tree(serializer.instance)

            if not cancelled and cancelled.status != 'cancelled':
                # it ended otherwise since the request read it
                raise ValidationError({'status': [f"Can not change status from "
                                                  f"'{cancelled.status}' to 'cancelled'."]})

        super(PluginInstanceDetail, self).perform_update(serializer)

    def destroy(self, request, *args, **kwargs):
        """
        Overriden to cancel the plugin instance execution before deleting it. All the
        descendant instances are also cancelled before they are deleted by the DB
        CASCADE. Then asyncronously delete the plugin instance and its folder/files
        from storage.
        """
        instance = self.get_object()
        cancelled = (cancel_plugin_instance_tree(instance)
                     if instance.status in ACTIVE_STATUSES else None)

        if not cancelled and getattr(cancelled, 'status', None) != 'cancelled':
            # it had ended, or ended meanwhile, and keeps the status it ended with: its
            # descendants still running go too, from an active status only (the list
            # holds the instance itself too, as the request read it)
            cancel_plugin_instances([inst for inst in instance.get_descendant_instances()
                                     if inst.id != instance.id
                                     and inst.status in ACTIVE_STATUSES], ACTIVE_STATUSES)

        if instance.mark_deletion_pending():
            delete_plugin_instance.delay(instance.id)  # async task

        serializer = self.get_serializer(instance)
        return Response(serializer.data, status=status.HTTP_202_ACCEPTED)


class PluginInstanceDescendantList(generics.ListAPIView):
    """
    A view for the collection of plugin instances that are a descendant of this plugin
    instance.
    """
    http_method_names = ['get']
    serializer_class = PluginInstanceSerializer
    queryset = PluginInstance.add_serializer_relations(PluginInstance.objects.all())
    permission_classes = (IsOwnerOrChrisOrHasFeedPermissionReadOnlyOrPublicFeedReadOnly,)

    def list(self, request, *args, **kwargs):
        """
        Overriden to return a list of the plugin instance descendants.
        """
        queryset = self.get_descendants_queryset()
        return services.get_list_response(self, queryset)

    def paginate_queryset(self, queryset):
        """
        Overriden to fetch, with one query per relation, the related objects the
        serializer reads for the descendants in the page only. Joining them in the walk
        that finds the descendants would cost a join at every visited node instead.
        """
        page = super(PluginInstanceDescendantList, self).paginate_queryset(queryset)
        if page is not None:
            PluginInstance.prefetch_serializer_relations(page)
        return page

    def get_descendants_queryset(self):
        """
        Custom method to get the actual descendants queryset.
        """
        instance = self.get_object()
        return self.filter_queryset(instance.get_descendant_instances())


class PluginInstanceSplitList(generics.ListCreateAPIView):
    """
    A view for the collection of splits for a plugin instance.
    """
    http_method_names = ['get', 'post']
    serializer_class = PluginInstanceSplitSerializer
    queryset = PluginInstance.objects.all()
    permission_classes = (IsOwnerOrChrisOrHasFeedPermissionReadOnlyOrPublicFeedReadOnly,)

    def perform_create(self, serializer):
        """
        Overriden to associate a plugin instance and a list of newly created
        'pl-topologicalcopy' plugin instance ids with the newly created split before
        first saving to the DB.
        """
        user = self.request.user
        instance = self.get_object()
        plg_topologcopy = Plugin.objects.filter(meta__name='pl-topologicalcopy').first()

        cr_name = serializer.validated_data.pop('compute_resource_name', '')
        if cr_name:
            compute_resource = plg_topologcopy.compute_resources.get(name=cr_name)
        else:
            compute_resource = plg_topologcopy.compute_resources.first()

        plg_filter_param = plg_topologcopy.parameters.get(name='filter')
        plg_plugininstances_param = plg_topologcopy.parameters.get(name='plugininstances')

        created_plg_inst_ids = []
        filter_list = serializer.validated_data.get('filter', '').split(',')

        for f in filter_list:
            plg_inst = PluginInstance.objects.create(
                plugin=plg_topologcopy, owner=user, previous=instance,
                compute_resource=compute_resource
            )
            StrParameter.objects.create(plugin_inst=plg_inst,
                                        plugin_param=plg_plugininstances_param,
                                        value=str(instance.id))
            if f:
                StrParameter.objects.create(plugin_inst=plg_inst,
                                            plugin_param=plg_filter_param,
                                            value=f)
            run_if_ready(plg_inst, instance)
            created_plg_inst_ids.append(str(plg_inst.id))

        serializer.save(
            plugin_inst=instance, created_plugin_inst_ids=','.join(created_plg_inst_ids)
        )

    def list(self, request, *args, **kwargs):
        """
        Overriden to return the list of splits for the queried plugin instance.
        A document-level link relation and a collection+json template are also added
        to the response.
        """
        queryset = self.get_splits_queryset()
        response = services.get_list_response(self, queryset)
        instance = self.get_object()
        # append document-level link relations
        links = {'plugin_inst': reverse('plugininstance-detail', request=request,
                                        kwargs={"pk": instance.id})}
        response = services.append_collection_links(response, links)
        # append write template
        template_data = {'filter': '', 'compute_resource_name': ''}
        return services.append_collection_template(response, template_data)

    def get_splits_queryset(self):
        """
        Custom method to get the actual splits queryset.
        """
        instance = self.get_object()
        return self.filter_queryset(instance.splits.all())


class PluginInstanceSplitDetail(generics.RetrieveAPIView):
    """
    A view for a plugin instance split.
    """
    http_method_names = ['get']
    queryset = PluginInstanceSplit.objects.all()
    serializer_class = PluginInstanceSplitSerializer
    permission_classes = (IsOwnerOrChrisOrHasFeedPermissionReadOnlyOrPublicFeedReadOnly,)


class PluginInstanceParameterList(generics.ListAPIView):
    """
    A view for the collection of parameters that the plugin instance was run with.
    """
    http_method_names = ['get']
    serializer_class = GenericParameterSerializer
    queryset = PluginInstance.objects.all()
    permission_classes = (IsOwnerOrChrisOrHasFeedPermissionReadOnlyOrPublicFeedReadOnly,)

    def list(self, request, *args, **kwargs):
        """
        Overriden to return a list with all the parameter values used by the queried
        plugin instance.
        """
        queryset = self.get_parameters_queryset()
        return services.get_list_response(self, queryset)

    def get_parameters_queryset(self):
        """
        Custom method to get a queryset with all the parameters regardless of their type.
        """
        instance = self.get_object()
        return self.filter_queryset(instance.get_parameter_instances())


class StrParameterDetail(generics.RetrieveAPIView):
    """
    A string parameter view.
    """
    http_method_names = ['get']
    serializer_class = PARAMETER_SERIALIZERS['string']
    queryset = StrParameter.objects.all()
    permission_classes = (IsOwnerOrChrisOrHasFeedPermissionReadOnlyOrPublicFeedReadOnly,)
    

class IntParameterDetail(generics.RetrieveAPIView):
    """
    An integer parameter view.
    """
    http_method_names = ['get']
    serializer_class = PARAMETER_SERIALIZERS['integer']
    queryset = IntParameter.objects.all()
    permission_classes = (IsOwnerOrChrisOrHasFeedPermissionReadOnlyOrPublicFeedReadOnly,)


class FloatParameterDetail(generics.RetrieveAPIView):
    """
    A float parameter view.
    """
    http_method_names = ['get']
    serializer_class = PARAMETER_SERIALIZERS['float']
    queryset = FloatParameter.objects.all()
    permission_classes = (IsOwnerOrChrisOrHasFeedPermissionReadOnlyOrPublicFeedReadOnly,)
    

class BoolParameterDetail(generics.RetrieveAPIView):
    """
    A boolean parameter view.
    """
    http_method_names = ['get']
    serializer_class = PARAMETER_SERIALIZERS['boolean']
    queryset = BoolParameter.objects.all()
    permission_classes = (IsOwnerOrChrisOrHasFeedPermissionReadOnlyOrPublicFeedReadOnly,)


class PathParameterDetail(generics.RetrieveAPIView):
    """
    A path parameter view.
    """
    http_method_names = ['get']
    serializer_class = PARAMETER_SERIALIZERS['path']
    queryset = PathParameter.objects.all()
    permission_classes = (IsOwnerOrChrisOrHasFeedPermissionReadOnlyOrPublicFeedReadOnly,)


class UnextpathParameterDetail(generics.RetrieveAPIView):
    """
    A unextpath parameter view.
    """
    http_method_names = ['get']
    serializer_class = PARAMETER_SERIALIZERS['unextpath']
    queryset = UnextpathParameter.objects.all()
    permission_classes = (IsOwnerOrChrisOrHasFeedPermissionReadOnlyOrPublicFeedReadOnly,)
