
from django.core.exceptions import ObjectDoesNotExist
from rest_framework import serializers
from rest_framework.reverse import reverse
from drf_spectacular.utils import OpenApiTypes, extend_schema_field

from collectionjson.fields import ItemLinkField
from core.models import PathAccessError, validate_path_access
from core.serializers import save_validated_fields
from plugins.enums import TYPES
from plugins.models import Plugin

from .models import ACTIVE_STATUSES
from .models import PluginInstance, PluginInstanceSplit
from .models import (FloatParameter, IntParameter, BoolParameter, PathParameter,
                     UnextpathParameter, StrParameter)


class PluginInstanceSerializer(serializers.HyperlinkedModelSerializer):
    compute_resource_name = serializers.CharField(max_length=100, required=False,
                                                  source='compute_resource.name')
    previous_id = serializers.ReadOnlyField()
    plugin_id = serializers.ReadOnlyField()
    workflow_id = serializers.ReadOnlyField()
    feed_id = serializers.ReadOnlyField()
    plugin_name = serializers.ReadOnlyField(source='plugin.meta.name')
    plugin_version = serializers.ReadOnlyField(source='plugin.version')
    plugin_type = serializers.ReadOnlyField(source='plugin.meta.type')
    # allow_null below covers two things, because the source traverses a nullable
    # relation: drf-spectacular cannot infer the nullability on its own
    # (tfranzel/drf-spectacular#1307), and without it DRF drops the key from the
    # response instead of emitting null, which breaks the schema's `required`.
    # It does not affect input validation, which skips read-only fields.
    pipeline_id = serializers.ReadOnlyField(source='workflow.pipeline.id',
                                            allow_null=True)
    pipeline_name = serializers.ReadOnlyField(source='workflow.pipeline.name',
                                              allow_null=True)
    output_path = serializers.ReadOnlyField(source='output_folder.path',
                                            allow_null=True)
    summary = serializers.JSONField(binary=True, read_only=True)
    raw = serializers.ReadOnlyField()
    owner_username = serializers.ReadOnlyField(source='owner.username')
    size = serializers.ReadOnlyField()
    active = serializers.SerializerMethodField()
    error_code = serializers.ReadOnlyField()
    previous = serializers.HyperlinkedRelatedField(
        view_name='plugininstance-detail', read_only=True, allow_null=True
    )
    output_folder = serializers.HyperlinkedRelatedField(view_name='chrisfolder-detail',
                                                        read_only=True)
    descendants = serializers.HyperlinkedIdentityField(
        view_name='plugininstance-descendant-list'
    )
    parameters = serializers.HyperlinkedIdentityField(
        view_name='plugininstance-parameter-list'
    )
    plugin = serializers.HyperlinkedRelatedField(
        view_name='plugin-detail', read_only=True
    )
    workflow = serializers.HyperlinkedRelatedField(
        view_name='workflow-detail', read_only=True, allow_null=True
    )
    feed = serializers.HyperlinkedRelatedField(view_name='feed-detail', read_only=True)
    compute_resource = serializers.HyperlinkedRelatedField(
        view_name='computeresource-detail', read_only=True
    )
    splits = serializers.HyperlinkedIdentityField(view_name='plugininstancesplit-list')

    class Meta:
        model = PluginInstance
        fields = ('url', 'id', 'title', 'previous_id', 'compute_resource_name',
                  'plugin_id', 'plugin_name', 'plugin_version', 'plugin_type',
                  'feed_id', 'start_date', 'end_date', 'output_path', 'status', 'active',
                  'pipeline_id', 'pipeline_name', 'workflow_id', 'summary', 'raw',
                  'owner_username', 'cpu_limit', 'memory_limit', 'number_of_workers',
                  'gpu_limit', 'size', 'error_code', 'deletion_status',
                  'deletion_requested_at', 'deletion_error', 'previous', 'output_folder',
                  'feed', 'plugin', 'workflow', 'compute_resource',
                  'descendants', 'parameters', 'splits')
        # the deletion's state is the deletion tasks' to write
        read_only_fields = ('deletion_status', 'deletion_requested_at', 'deletion_error')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance is None: # on create
            self.fields['status'].read_only = True  # set to read-only before validation
        else: # on update
            # the compute resource of an instance is not changed
            self.fields['compute_resource_name'].read_only = True
            self.fields['gpu_limit'].read_only = True
            self.fields['number_of_workers'].read_only = True
            self.fields['cpu_limit'].read_only = True
            self.fields['memory_limit'].read_only = True

    def get_active(self, obj) -> bool:
        """
        Overriden to get the "active" status of the plugin instance.
        """
        return obj.status in ACTIVE_STATUSES

    def update(self, instance, validated_data):
        """
        Overriden to save only the fields the request changes and never the status, which
        the view changes with a transition: a whole save would write back whatever a task
        changed since the request read the instance.
        """
        validated_data.pop('status', None)
        return save_validated_fields(instance, validated_data)

    def validate_previous(self, previous_id):
        """
        Custom method to check that an id is provided for previous instance when
        corresponding plugin is of type 'ds' or 'ts'. Then check that the provided id
        exists in the DB and that the user can run plugins within this feed.
        """
        # using self.context['view'] in validators prevents calling is_valid when creating
        # a new serializer instance outside the Django view framework. But here is fine
        # as plugin instances are always created through the API
        plugin = self.context['view'].get_object()
        previous = None

        if plugin.meta.type in ('ds', 'ts'):
            if not previous_id:
                raise serializers.ValidationError(
                    {'previous_id': ["This field is required."]})
            try:
                pk = int(previous_id)
                previous = PluginInstance.objects.get(pk=pk)
            except (ValueError, ObjectDoesNotExist):
                err_str = "Couldn't find any 'previous' plugin instance with id %s."
                raise serializers.ValidationError(
                    {'previous_id': [err_str % previous_id]})

            # check that the user can run plugins within this feed
            user = self.context['request'].user
            feed = previous.feed

            if not (user == feed.owner or feed.has_user_permission(user)):
                err_str = "Not allowed to write to feed for previous instance with id %s."
                raise serializers.ValidationError(
                    {'previous_id': [err_str % previous_id]})
        return previous

    def validate_compute_resource_name(self, compute_resource_name):
        """
        Overriden to check the provided compute resource name is registered with the
        corresponding plugin.
        """
        plg = self.context['view'].get_object()
        if plg.compute_resources.filter(name=compute_resource_name).count() == 0:
            msg = "Plugin '%s' with version '%s' has not been registered with compute " \
                  "resource '%s'." % (plg.meta.name, plg.version, compute_resource_name)
            raise serializers.ValidationError([msg])
        return compute_resource_name

    def validate_status(self, status):
        """
        Overriden to validate a change of status.
        """
        instance = self.instance
        if instance and (status != 'cancelled' or
                         instance.status in ('finishedSuccessfully',
                                             'finishedWithError')):
            msg = "Can not change status from '%s' to '%s'."
            raise serializers.ValidationError([msg % (instance.status, status)])
        return status

    def validate_gpu_limit(self, gpu_limit):
        """
        Overriden to validate gpu_limit is within the proper limits.
        """
        plugin = self.context['view'].get_object()
        self.validate_value_within_interval(gpu_limit,
                                            plugin.min_gpu_limit,
                                            plugin.max_gpu_limit)
        return gpu_limit

    def validate_number_of_workers(self, number_of_workers):
        """
        Overriden to validate number_of_workers is within the proper limits.
        """
        plugin = self.context['view'].get_object()
        self.validate_value_within_interval(number_of_workers,
                                            plugin.min_number_of_workers,
                                            plugin.max_number_of_workers)
        return number_of_workers

    def validate_cpu_limit(self, cpu_limit):
        """
        Overriden to validate cpu_limit is within the proper limits.
        """
        plugin = self.context['view'].get_object()
        self.validate_value_within_interval(cpu_limit,
                                            plugin.min_cpu_limit,
                                            plugin.max_cpu_limit)
        return cpu_limit

    def validate_memory_limit(self, memory_limit):
        """
        Overriden to validate memory_limit is within the proper limits.
        """
        plugin = self.context['view'].get_object()
        self.validate_value_within_interval(memory_limit,
                                            plugin.min_memory_limit,
                                            plugin.max_memory_limit)
        return memory_limit

    @staticmethod
    def validate_value_within_interval(val, min_val, max_val):
        if val is None:
            return
        if val < min_val or val > max_val:
            raise serializers.ValidationError(["This field value is out of range."])


class PartialPluginInstanceSerializer(PluginInstanceSerializer):
    """
    A compact serializer exposing only a small subset of PluginInstance fields.
    """
    class Meta(PluginInstanceSerializer.Meta):
        fields = ('url', 'id', 'title', 'previous_id', 'compute_resource_name',
                  'plugin_id', 'plugin_name', 'plugin_version', 'plugin_type',
                  'feed_id', 'start_date', 'end_date', 'output_path', 'status', 'active',
                  'owner_username', 'cpu_limit', 'memory_limit', 'number_of_workers',
                  'gpu_limit', 'size', 'error_code', 'deletion_status', 'deletion_error',
                  'output_folder')


class PluginInstanceSplitSerializer(serializers.HyperlinkedModelSerializer):
    compute_resource_name = serializers.CharField(max_length=100, write_only=True,
                                                  required=False)
    created_plugin_inst_ids = serializers.ReadOnlyField()
    plugin_inst_id = serializers.ReadOnlyField(source='plugin_inst.id')
    plugin_inst = serializers.HyperlinkedRelatedField(view_name='plugininstance-detail',
                                                      read_only=True)

    class Meta:
        model = PluginInstanceSplit
        fields = ('url', 'id', 'creation_date', 'filter', 'plugin_inst_id',
                  'created_plugin_inst_ids', 'compute_resource_name', 'plugin_inst')

    def validate_filter(self, filter_value):
        """
        Overriden to check that the provided filter is a string of regular expressions
        separated by commas).
        """
        if filter_value:
            regexs = [r.strip() for r in filter_value.split(',')]
            filter_value = ','.join(regexs)
        return filter_value

    def validate_compute_resource_name(self, compute_resource_name):
        """
        Overriden to check the provided compute resource name is registered with
        pl-topologicalplugin.
        """
        plg_topologcopy = Plugin.objects.filter(meta__name='pl-topologicalcopy').first()
        if not plg_topologcopy:
            raise serializers.ValidationError([f"Could not find plugin "
                                               f"'pl-topologicalcopy'. Please contact "
                                               f"your ChRIS admin."])
        if plg_topologcopy.compute_resources.filter(
                name=compute_resource_name).count() == 0:
            raise serializers.ValidationError([f"Plugin 'pl-topologicalcopy' with version"
                                               f" '{plg_topologcopy.version}' has not"
                                               f" been registered with compute resource"
                                               f" '{compute_resource_name}'."])
        return compute_resource_name


class StrParameterSerializer(serializers.HyperlinkedModelSerializer):
    param_name = serializers.ReadOnlyField(source='plugin_param.name')
    type = serializers.ReadOnlyField(source='plugin_param.type')
    plugin_inst = serializers.HyperlinkedRelatedField(view_name='plugininstance-detail',
                                                      read_only=True)
    plugin_param = serializers.HyperlinkedRelatedField(view_name='pluginparameter-detail',
                                                       read_only=True)

    class Meta:
        model = StrParameter
        fields = ('url', 'id', 'param_name', 'value', 'type', 'plugin_inst',
                  'plugin_param')

    def __init__(self, *args, **kwargs):
        """
        Overriden to get the plugin parameter as a keyword argument at object creation.
        """
        self.param_name = kwargs.pop('param_name', None)
        self.plugin_type = kwargs.pop('plugin_type', None)
        self.previous = kwargs.pop('previous', None)
        super(StrParameterSerializer, self).__init__(*args, **kwargs)

    def validate_value(self, value):
        """
        Overriden to check that all the provided plugin instance ids exist in the DB and
        belong to the same feed for a 'plugininstances' parameter in a 'ts' plugin
        (value should be a string of plugin instance ids separated by commas).
        """
        if value and self.param_name and self.plugin_type and self.previous:
            if (self.param_name == 'plugininstances') and (self.plugin_type == 'ts'):
                plg_inst_ids = [inst_id.strip() for inst_id in value.split(',')]
                if str(self.previous.id) not in plg_inst_ids:
                    raise serializers.ValidationError(
                        [f"Previous instance id '{self.previous.id}' must be included in "
                         f"the 'plugininstances' list for a non-empty list."])
                for inst_id in plg_inst_ids:
                    try:
                        plg_inst = PluginInstance.objects.get(pk=int(inst_id))
                    except (ValueError, ObjectDoesNotExist):
                        raise serializers.ValidationError(
                            [f"Couldn't find any plugin instance with id '{inst_id}'."])
                    if plg_inst.feed.id != self.previous.feed.id:
                        raise serializers.ValidationError(
                            [f"Plugin instance with id '{inst_id}' is not in this feed."])
                value = ','.join(plg_inst_ids)
            elif self.param_name == 'filter' and self.plugin_type == 'ts':
                regexs = [r.strip() for r in value.split(',')]
                value = ','.join(regexs)
        return value


class IntParameterSerializer(serializers.HyperlinkedModelSerializer):
    param_name = serializers.ReadOnlyField(source='plugin_param.name')
    type = serializers.ReadOnlyField(source='plugin_param.type')
    plugin_inst = serializers.HyperlinkedRelatedField(view_name='plugininstance-detail',
                                                      read_only=True)
    plugin_param = serializers.HyperlinkedRelatedField(view_name='pluginparameter-detail',
                                                       read_only=True)

    class Meta:
        model = IntParameter
        fields = ('url', 'id', 'param_name', 'value', 'type', 'plugin_inst',
                  'plugin_param')


class FloatParameterSerializer(serializers.HyperlinkedModelSerializer):
    param_name = serializers.ReadOnlyField(source='plugin_param.name')
    type = serializers.ReadOnlyField(source='plugin_param.type')
    plugin_inst = serializers.HyperlinkedRelatedField(view_name='plugininstance-detail',
                                                      read_only=True)
    plugin_param = serializers.HyperlinkedRelatedField(view_name='pluginparameter-detail',
                                                       read_only=True)

    class Meta:
        model = FloatParameter
        fields = ('url', 'id', 'param_name', 'value', 'type', 'plugin_inst',
                  'plugin_param')


class BoolParameterSerializer(serializers.HyperlinkedModelSerializer):
    param_name = serializers.ReadOnlyField(source='plugin_param.name')
    type = serializers.ReadOnlyField(source='plugin_param.type')
    plugin_inst = serializers.HyperlinkedRelatedField(view_name='plugininstance-detail',
                                                      read_only=True)
    plugin_param = serializers.HyperlinkedRelatedField(view_name='pluginparameter-detail',
                                                       read_only=True)

    class Meta:
        model = BoolParameter
        fields = ('url', 'id', 'param_name', 'value', 'type', 'plugin_inst',
                  'plugin_param')


def validate_paths(user, string):
    """
    Custom function to check whether a user is allowed to access the provided
    paths (a string of one or more paths separated by commas).
    """
    path_list = [s.strip().strip('/') for s in string.split(',')]

    for path in path_list:
        try:
            validate_path_access(user, path)
        except PathAccessError as e:
            raise serializers.ValidationError([str(e)])

    return ','.join(path_list)


class PathParameterSerializer(serializers.HyperlinkedModelSerializer):
    param_name = serializers.ReadOnlyField(source='plugin_param.name')
    type = serializers.ReadOnlyField(source='plugin_param.type')
    plugin_inst = serializers.HyperlinkedRelatedField(view_name='plugininstance-detail',
                                                      read_only=True)
    plugin_param = serializers.HyperlinkedRelatedField(view_name='pluginparameter-detail',
                                                       read_only=True)

    class Meta:
        model = PathParameter
        fields = ('url', 'id', 'param_name', 'value', 'type', 'plugin_inst',
                  'plugin_param')

    def __init__(self, *args, **kwargs):
        """
        Overriden to get the request user as a keyword argument at object creation.
        """
        if 'user' in kwargs:
            self.user = kwargs.pop('user')
        super(PathParameterSerializer, self).__init__(*args, **kwargs)

    def validate_value(self, value):
        """
        Overriden to check that the user making the request is allowed to access
        the provided paths (value should be a string of paths separated by commas).
        """
        return validate_paths(self.user, value)


class UnextpathParameterSerializer(serializers.HyperlinkedModelSerializer):
    param_name = serializers.ReadOnlyField(source='plugin_param.name')
    type = serializers.ReadOnlyField(source='plugin_param.type')
    plugin_inst = serializers.HyperlinkedRelatedField(view_name='plugininstance-detail',
                                                      read_only=True)
    plugin_param = serializers.HyperlinkedRelatedField(view_name='pluginparameter-detail',
                                                       read_only=True)

    class Meta:
        model = UnextpathParameter
        fields = ('url', 'id', 'param_name', 'value', 'type', 'plugin_inst',
                  'plugin_param')

    def __init__(self, *args, **kwargs):
        """
        Overriden to get the request user as a keyword argument at object creation.
        """
        if 'user' in kwargs:
            self.user = kwargs.pop('user')
        super(UnextpathParameterSerializer, self).__init__(*args, **kwargs)

    def validate_value(self, value):
        """
        Overriden to check that the user making the request is allowed to access
        the provided paths (value should be a string of paths separated by commas).
        """
        return validate_paths(self.user, value)


class GenericParameterSerializer(serializers.HyperlinkedModelSerializer):
    param_name = serializers.ReadOnlyField(source='plugin_param.name')
    type = serializers.ReadOnlyField(source='plugin_param.type')
    value = serializers.SerializerMethodField()
    url = ItemLinkField('_get_url')
    plugin_inst = serializers.HyperlinkedRelatedField(view_name='plugininstance-detail',
                                                      read_only=True)
    plugin_param = serializers.HyperlinkedRelatedField(view_name='pluginparameter-detail',
                                                       read_only=True)

    class Meta:
        model = StrParameter
        fields = ('url', 'id', 'param_name', 'value', 'type', 'plugin_inst',
                  'plugin_param')

    @extend_schema_field(OpenApiTypes.URI)
    def _get_url(self, obj):
        """
        Custom method to get the correct url for the serialized object regardless of
        its type.
        """
        request = self.context['request']
        # here parameter detail view names are assumed to follow a convention
        view_name = TYPES[obj.plugin_param.type] + 'parameter-detail'
        return reverse(view_name, request=request, kwargs={"pk": obj.id})

    def get_value(self, obj) -> str | int | float | bool:
        """
        Overriden to get the default parameter value regardless of its type.
        """
        return obj.value


PARAMETER_SERIALIZERS = {'string': StrParameterSerializer,
                         'integer': IntParameterSerializer,
                         'float': FloatParameterSerializer,
                         'boolean': BoolParameterSerializer,
                         'path': PathParameterSerializer,
                         'unextpath': UnextpathParameterSerializer}
