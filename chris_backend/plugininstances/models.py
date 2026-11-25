
import logging
from dataclasses import dataclass

from django.db import models
from django.db.models import F
from django.db.models.signals import post_delete
from django.dispatch import receiver

import django_filters
from django_filters.rest_framework import FilterSet

from core.models import AsyncDeletableModel, ChrisFolder, ChrisInstance, delete_related
from feeds.models import Feed
from plugins.models import ComputeResource, Plugin, PluginParameter
from plugins.fields import CPUField, MemoryField
from plugins.fields import MemoryInt, CPUInt
from workflows.models import Workflow
from plugininstances.enums import (ACTIVE_STATUSES, ARM_REMOTE_CLEANUP, INACTIVE_STATUSES,
                                   REMOTE_CLEANUP_STATUS_CHOICES, REMOTE_CLEANUP_STATUSES,
                                   REMOTE_JOB_STATUSES, STATUS_CHOICES, STATUSES)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StatusWrite:
    """
    The outcome of a conditional write of a plugin instance: whether this caller made it
    (true as a bool), and a status: when it won, the one it wrote, or for a write of other
    fields the one the caller's object holds, which may be older than the row's; when it
    lost, the one another writer left in the database row, None when the row is gone.
    """
    won: bool
    status: str | None

    def __bool__(self):
        return self.won


def _statuses(source, known=STATUSES):
    """
    ``source`` (a status or several) as a list, refusing a name that is not a status:
    a typo would make a conditional write lose every time.
    """
    sources = [source] if isinstance(source, str) else list(source)

    if not sources or set(sources) - known:
        raise ValueError(f'not a list of statuses: {source!r}')
    return sources


def get_default_job_status_summary() -> dict:
    """
    Get a default job status summary JSON.
    """
    return {
        'pushPath': {'status': False},
        'pullPath': {'status': False},
        'compute': {
            'submit': {'status': False},
            'return': {'status': False,
                       'job_status': '',
                       'job_logs': ''
                }
            },
        }


class PluginInstance(AsyncDeletableModel):
    MAX_COPY_RETRIES = 3
    MAX_UPLOAD_RETRIES = 3
    MAX_REMOTE_CLEANUP_RETRIES = 4
    # related objects PluginInstanceSerializer reads from other db tables
    SERIALIZER_RELATIONS = ('plugin__meta', 'output_folder', 'owner', 'compute_resource',
                            'workflow__pipeline')
    # the ones PartialPluginInstanceSerializer reads, which leaves out the workflow
    PARTIAL_SERIALIZER_RELATIONS = ('plugin__meta', 'output_folder', 'owner',
                                    'compute_resource')

    title = models.CharField(max_length=100, blank=True)
    start_date = models.DateTimeField(auto_now_add=True)
    end_date = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='created',
                              db_index=True)
    summary = models.JSONField(blank=True, default=get_default_job_status_summary)
    raw = models.TextField(blank=True)
    size = models.BigIntegerField(default=0)
    error_code = models.CharField(max_length=7, blank=True)
    copy_retry_count = models.IntegerField(default=0)
    upload_retry_count = models.IntegerField(default=0)
    remote_cleanup_status = models.CharField(max_length=30,
                                             choices=REMOTE_CLEANUP_STATUS_CHOICES,
                                             default='notStarted')
    remote_cleanup_retry_count = models.IntegerField(default=0)
    previous = models.ForeignKey("self", on_delete=models.CASCADE, null=True,
                                 related_name='next')
    plugin = models.ForeignKey(Plugin, on_delete=models.CASCADE, related_name='instances')
    feed = models.ForeignKey(Feed, on_delete=models.CASCADE,
                             related_name='plugin_instances')
    output_folder = models.OneToOneField(ChrisFolder, on_delete=models.CASCADE, null=True,
                                         related_name='plugin_inst')
    owner = models.ForeignKey('auth.User', on_delete=models.CASCADE)
    compute_resource = models.ForeignKey(ComputeResource, null=True,
                                         on_delete=models.SET_NULL,
                                         related_name='plugin_instances')
    workflow = models.ForeignKey(Workflow, null=True,
                                 on_delete=models.SET_NULL,
                                 related_name='plugin_instances')
    cpu_limit = CPUField(null=True)
    memory_limit = MemoryField(null=True)
    number_of_workers = models.IntegerField(null=True)
    gpu_limit = models.IntegerField(null=True)

    class Meta:
        ordering = ('-start_date',)

    def __str__(self):
        return self.title

    @staticmethod
    def add_serializer_relations(plg_inst_qs, relations=SERIALIZER_RELATIONS):
        """
        Custom static method to fetch in the same query the related objects that the
        serializers read for every plugin instance in a queryset. Without it every
        serialized instance costs one extra query per relation.
        """
        return plg_inst_qs.select_related(*relations)

    @staticmethod
    def prefetch_serializer_relations(plg_insts, relations=SERIALIZER_RELATIONS):
        """
        Custom static method to fetch the related objects that the serializers read for
        every plugin instance in a list of instances that is not a queryset (e.g. a
        page of descendants), with one query per relation for the whole list.
        """
        models.prefetch_related_objects(plg_insts, *relations)

    @staticmethod
    def filter_visible_to_user(plg_inst_qs, user):
        """
        Custom static method to filter a queryset down to the plugin instances a user
        can see: those in public feeds if the user is not authenticated, all of them for
        the superuser 'chris' and otherwise those in public feeds or in feeds owned by
        the user or shared with the user or one of the user's groups.
        """
        if not user.is_authenticated:
            return plg_inst_qs.filter(feed__public=True)

        if user.username == 'chris':
            return plg_inst_qs

        # matching the feed against a subquery, instead of joining the feeds' sharing
        # tables, does not repeat an instance for every user or group its feed is
        # shared with
        lookup = models.Q(public=True) | models.Q(owner=user) | models.Q(
            shared_users=user) | models.Q(shared_groups__in=user.groups.all())
        return plg_inst_qs.filter(feed__in=Feed.objects.filter(lookup))

    def save(self, *args, **kwargs):
        """
        Overriden to save a new output folder to the DB the first time the instance
        is saved. In addition, a new feed is saved for 'fs' instances. For 'ds' and
        'ts' instances the feed of the previous instance is assigned.
        """
        if not hasattr(self, 'feed'):
            plugin_type = self.plugin.meta.type
            if plugin_type == 'fs':
                self.feed = self._save_feed()
            elif plugin_type in ('ds', 'ts'):
                self.feed = self.previous.feed

        self._set_compute_defaults()

        super(PluginInstance, self).save(*args, **kwargs)

        if self.output_folder is None:
            self.output_folder = self._save_output_folder()
            super(PluginInstance, self).save(update_fields=['output_folder'])

    def _save_feed(self):
        """
        Custom internal method to create and save a new feed to the DB.
        """
        feed = Feed()
        feed.name = self.title or self.plugin.meta.name
        feed.owner = self.owner
        feed.save()

        # feed's folder path: SWIFT_CONTAINER_NAME/home/<username>/feeds/feed_<id>
        feed_folder_path = f'home/{self.owner.username}/feeds/feed_{feed.id}'
        folder = ChrisFolder(path=feed_folder_path, owner=self.owner)
        folder.save()

        feed.folder = folder
        feed.save()
        return feed

    def _save_output_folder(self):
        """
        Custom internal method to create and save a new output folder to the DB.
        """
        # 'fs' plugins will output files to:
        # SWIFT_CONTAINER_NAME/home/<username>/feeds/feed_<id>/<plugin_name>_
        # <plugin_inst_id>/data
        # 'ds' and 'ts' plugins will output files to:
        # SWIFT_CONTAINER_NAME/home/<username>/feeds/feed_<id>/...
        # /<previous_plugin_name>_<plugin_inst_id>/<plugin_name>_<plugin_inst_id>/data
        current = self
        path = '/{0}_{1}/data'.format(current.plugin.meta.name, current.id)
        while not current.plugin.meta.type == 'fs':
            current = current.previous
            path = '/{0}_{1}'.format(current.plugin.meta.name, current.id) + path

        feed = current.feed
        # username = self.owner.username
        username = feed.owner.username  # use creator of the feed for shared
        # feeds
        output_path = 'home/{0}/feeds/feed_{1}'.format(username, feed.id) + path

        folder = ChrisFolder(path=output_path, owner=self.owner)
        folder.save()
        return folder

    def _set_compute_defaults(self):
        """
        Custom internal method to set compute-related defaults.
        """
        if not self.cpu_limit:
            self.cpu_limit = CPUInt(self.plugin.min_cpu_limit)
        if not self.memory_limit:
            self.memory_limit = MemoryInt(self.plugin.min_memory_limit)
        if not self.number_of_workers:
            self.number_of_workers = self.plugin.min_number_of_workers
        if not self.gpu_limit:
            self.gpu_limit = self.plugin.min_gpu_limit

    def get_root_instance(self):
        """
        Custom method to return the root plugin instance for this plugin instance.
        """
        current = self
        while not current.plugin.meta.type == 'fs':
            current = current.previous
        return current

    def get_descendant_instances(self):
        """
        Custom method to return all the plugin instances that are a descendant of this
        plugin instance.
        """
        descendant_instances = []
        queue = [self]
        while len(queue) > 0:
            visited = queue.pop()
            queue.extend(list(visited.next.all()))
            descendant_instances.append(visited)
        return descendant_instances

    def get_parameter_instances(self):
        """
        Custom method to get all the parameter instances associated with this plugin
        instance regardless of their type.
        """
        parameter_instances = []
        parameter_instances.extend(list(self.unextpath_param.all()))
        parameter_instances.extend(list(self.path_param.all()))
        parameter_instances.extend(list(self.string_param.all()))
        parameter_instances.extend(list(self.integer_param.all()))
        parameter_instances.extend(list(self.float_param.all()))
        parameter_instances.extend(list(self.boolean_param.all()))
        return parameter_instances

    def get_output_path(self):
        """
        Custom method to get the output directory for files generated by
        the plugin instance object.
        """
        return str(self.output_folder.path)

    # -- conditional writes ------------------------------------------------------------
    # Several tasks and API requests can act on one instance at once. Therefore the
    # instance status is only ever written by transition() (cancel() included): one
    # UPDATE conditional on the status the writer expects. What else a task writes is
    # conditional too, on the status (update_if_status()) or, for the remote cleanup, on
    # the cleanup's own status (move_remote_cleanup(), arm_remote_cleanup()), but for the
    # retry counters (increment()); a writer that lost does not do what would have
    # followed.

    def transition(self, to, source, **fields):
        """
        Move this instance to status ``to`` if the database has it in one of the statuses
        ``source`` (one or a list), writing ``fields`` in the same UPDATE. Returns a
        ``StatusWrite``: when it won, this object has the values written; when it lost, it
        is left as it was and the result says what status the row has.
        """
        sources = _statuses(source)
        _statuses(to)
        return self._write({'status__in': sources}, {'status': to, **fields})

    def cancel(self, source=None, job_id=None, **fields):
        """
        Move this instance to 'cancelled' from the statuses ``source`` (the active
        ones by default), writing ``fields``, with its remote cleanup armed in the same
        UPDATE when the database row is in a status with a job in the remote compute
        (``REMOTE_JOB_STATUSES``): the status a caller read may be stale by then.
        The periodic task handle_remote_cleanup submits the cleanup's jobs. The cancel
        that wins logs the final status, naming the instance's jobs ``job_id`` (read
        from the ``ChrisInstance`` when not given, a query: a caller that cancels many
        reads the prefix once). Returns the ``StatusWrite``.
        """
        sources = _statuses(source or ACTIVE_STATUSES)
        remote = [s for s in sources if s in REMOTE_JOB_STATUSES]
        local = [s for s in sources if s not in REMOTE_JOB_STATUSES]
        attempts = [(statuses, armed) for statuses, armed in
                    ((remote, ARM_REMOTE_CLEANUP), (local, {})) if statuses]
        written = StatusWrite(False, self.status)

        # a row moves forward only: out of a local status into a remote one, and out of
        # that into a terminal one, so the third round finds it where it stays. A loss
        # with the row still in one of ``sources`` means it moved between the UPDATE and
        # the read of its status: the other half of ``sources`` gets its turn.
        for _ in range(3):
            for statuses, armed in attempts:
                written = self.transition('cancelled', statuses, **fields, **armed)

                if written:
                    self.log_final_status(job_id)
                if written or written.status not in sources:
                    return written
        return written

    def log_final_status(self, job_id=None):
        """
        Log the terminal status, and the error code if any, that a write has just given
        this instance: the line that tells how it ended, which only the writer that won
        logs. ``job_id`` is the id of its jobs in the remote compute, read from the
        ``ChrisInstance`` when not given.
        """
        if job_id is None:
            job_id = ChrisInstance.load().job_id_prefix + str(self.id)
        error_code = self.error_code
        logger.info(f"Saved plugin instance final status for job {job_id} as "
                    f"'{self.status}'"
                    + (f' with error code {error_code}' if error_code else ''))

    def update_if_status(self, source, **fields):
        """
        Write ``fields``, which may not include the status, if the database has this
        instance in one of the statuses ``source``. Returns a ``StatusWrite`` as
        ``transition`` does.
        """
        if 'status' in fields:
            raise ValueError('only transition() writes the status')
        return self._write({'status__in': _statuses(source)}, fields)

    def move_remote_cleanup(self, to, source, **fields):
        """
        Move this instance's ``remote_cleanup_status`` to ``to`` if the database has it in
        one of ``source`` (one or a list), writing ``fields`` (not the status) in the same
        UPDATE. Returns whether it moved; when it did, this object has the values written.
        """
        sources = _statuses(source, REMOTE_CLEANUP_STATUSES)
        _statuses(to, REMOTE_CLEANUP_STATUSES)
        if 'status' in fields:
            raise ValueError('only transition() writes the status')
        return bool(self._write({'remote_cleanup_status__in': sources},
                                {'remote_cleanup_status': to, **fields}))

    def arm_remote_cleanup(self, rearm=False):
        """
        Start this instance's remote cleanup, if it has ended (a terminal status) and its
        cleanup has not started, or with ``rearm`` has ended too. Returns whether it
        started it.
        """
        sources = ['notStarted', 'complete', 'failed'] if rearm else ['notStarted']
        return bool(self._write(
            {'status__in': INACTIVE_STATUSES, 'remote_cleanup_status__in': sources},
            ARM_REMOTE_CLEANUP))

    def increment(self, field):
        """
        Add one to the counter ``field`` in the database and return its new value, which
        this object then has too.
        """
        type(self).objects.filter(pk=self.pk).update(**{field: F(field) + 1})
        self.refresh_from_db(fields=[field])
        return getattr(self, field)

    def _write(self, condition, values):
        """
        UPDATE this instance's database row with ``values`` where ``condition`` holds and
        mirror them into this object when it did.
        """
        written = type(self).objects.filter(pk=self.pk, **condition).update(**values)

        if written:
            for name, value in values.items():
                setattr(self, name, value)
            return StatusWrite(True, self.status)

        status = type(self).objects.filter(pk=self.pk).values_list('status',
                                                                   flat=True).first()
        return StatusWrite(False, status)


@receiver(post_delete, sender=PluginInstance)
def auto_delete_output_folder_with_plugin_instance(sender, instance, origin=None,
                                                   **kwargs):
    # the output folder's parent
    delete_related(instance, 'output_folder', 'parent', origin=origin)


class PluginInstanceFilter(FilterSet):
    min_start_date = django_filters.IsoDateTimeFilter(field_name='start_date',
                                                      lookup_expr='gte')
    max_start_date = django_filters.IsoDateTimeFilter(field_name='start_date',
                                                      lookup_expr='lte')
    min_end_date = django_filters.IsoDateTimeFilter(field_name='end_date',
                                                    lookup_expr='gte')
    max_end_date = django_filters.IsoDateTimeFilter(field_name='end_date',
                                                    lookup_expr='lte')
    title = django_filters.CharFilter(field_name='title', lookup_expr='icontains')
    owner_username = django_filters.CharFilter(field_name='owner__username',
                                               lookup_expr='exact')
    feed_id = django_filters.CharFilter(field_name='feed_id', lookup_expr='exact')
    root_id = django_filters.CharFilter(method='filter_by_root_id')
    previous_id = django_filters.CharFilter(method='filter_by_previous_id')
    plugin_id = django_filters.CharFilter(field_name='plugin_id', lookup_expr='exact')
    workflow_id = django_filters.CharFilter(field_name='workflow_id', lookup_expr='exact')
    plugin_name = django_filters.CharFilter(field_name='plugin__meta__name',
                                            lookup_expr='icontains')
    plugin_name_exact = django_filters.CharFilter(field_name='plugin__meta__name',
                                                  lookup_expr='exact')
    plugin_version = django_filters.CharFilter(field_name='plugin__version',
                                               lookup_expr='exact')
    plugin_type = django_filters.CharFilter(field_name='plugin__meta__type',
                                            lookup_expr='exact')
    active = django_filters.BooleanFilter(method='filter_by_active_status')

    class Meta:
        model = PluginInstance
        fields = ['id', 'min_start_date', 'max_start_date', 'min_end_date',
                  'max_end_date', 'root_id', 'previous_id', 'title', 'status', 'active',
                  'owner_username', 'feed_id', 'plugin_id', 'plugin_name',
                  'plugin_name_exact', 'plugin_version', 'plugin_type', 'workflow_id',
                  'deletion_status']

    def filter_by_root_id(self, queryset, name, value):
        """
        Custom method to return the plugin instances in a queryset with a common root
        plugin instance.
        """
        root_queryset = queryset.filter(pk=value)
        # check whether the root id value is in the DB
        if not root_queryset.exists():
            return root_queryset
        descendants = root_queryset[0].get_descendant_instances()
        return queryset.filter(pk__in=[inst.id for inst in descendants])

    def filter_by_previous_id(self, queryset, name, value):
        """
        Custom method to return the plugin instances in a queryset with a common previous
        plugin instance.
        """
        previous_queryset = queryset.filter(pk=value)
        # check whether the previous id value is in the DB
        if not previous_queryset.exists():
            return previous_queryset
        previous = previous_queryset.first()
        return queryset.filter(previous=previous)

    def filter_by_active_status(self, queryset, name, value):
        """
        Custom method to return the plugin instances in a queryset with an
        "active" status.
        """
        statuses = ACTIVE_STATUSES if value else INACTIVE_STATUSES
        return queryset.filter(status__in=statuses)


class PluginInstanceLock(models.Model):
    """
    The registration of a plugin instance's output files: when it first started, and
    the token of the run that owns it, the newest (PluginInstanceAppJob.register_output).
    """
    plugin_inst = models.OneToOneField(PluginInstance, on_delete=models.CASCADE,
                                       related_name='lock')
    start_date = models.DateTimeField(auto_now_add=True)
    owner_token = models.CharField(max_length=32, blank=True)

    def __str__(self):
        return str(self.plugin_inst.id)


class PluginInstanceSplit(models.Model):
    creation_date = models.DateTimeField(auto_now_add=True)
    filter = models.CharField(max_length=600, blank=True)
    created_plugin_inst_ids = models.CharField(max_length=600)
    plugin_inst = models.ForeignKey(PluginInstance, on_delete=models.CASCADE,
                                    related_name='splits')

    class Meta:
        ordering = ('plugin_inst', '-creation_date',)

    def __str__(self):
        return self.created_plugin_inst_ids


class StrParameter(models.Model):
    value = models.CharField(max_length=600, blank=True)
    plugin_inst = models.ForeignKey(PluginInstance, on_delete=models.CASCADE,
                                    related_name='string_param')
    plugin_param = models.ForeignKey(PluginParameter, on_delete=models.CASCADE,
                                     related_name='string_inst')

    class Meta:
        unique_together = ('plugin_inst', 'plugin_param',)

    def __str__(self):
        return self.value


class IntParameter(models.Model):
    value = models.IntegerField()
    plugin_inst = models.ForeignKey(PluginInstance, on_delete=models.CASCADE,
                                    related_name='integer_param')
    plugin_param = models.ForeignKey(PluginParameter, on_delete=models.CASCADE,
                                     related_name='integer_inst')

    class Meta:
        unique_together = ('plugin_inst', 'plugin_param',)

    def __str__(self):
        return str(self.value)


class FloatParameter(models.Model):
    value = models.FloatField()
    plugin_inst = models.ForeignKey(PluginInstance, on_delete=models.CASCADE,
                                    related_name='float_param')
    plugin_param = models.ForeignKey(PluginParameter, on_delete=models.CASCADE,
                                     related_name='float_inst')

    class Meta:
        unique_together = ('plugin_inst', 'plugin_param',)

    def __str__(self):
        return str(self.value)


class BoolParameter(models.Model):
    value = models.BooleanField()
    plugin_inst = models.ForeignKey(PluginInstance, on_delete=models.CASCADE,
                                    related_name='boolean_param')
    plugin_param = models.ForeignKey(PluginParameter, on_delete=models.CASCADE,
                                     related_name='boolean_inst')

    class Meta:
        unique_together = ('plugin_inst', 'plugin_param',)

    def __str__(self):
        return str(self.value)


class PathParameter(models.Model):
    value = models.CharField(max_length=16000)  # this string can be a list of long paths
    plugin_inst = models.ForeignKey(PluginInstance, on_delete=models.CASCADE,
                                    related_name='path_param')
    plugin_param = models.ForeignKey(PluginParameter, on_delete=models.CASCADE,
                                     related_name='path_inst')

    class Meta:
        unique_together = ('plugin_inst', 'plugin_param',)

    def __str__(self):
        return self.value


class UnextpathParameter(models.Model):
    value = models.CharField(max_length=16000)  # this string can be a list of long paths
    plugin_inst = models.ForeignKey(PluginInstance, on_delete=models.CASCADE,
                                    related_name='unextpath_param')
    plugin_param = models.ForeignKey(PluginParameter, on_delete=models.CASCADE,
                                     related_name='unextpath_inst')

    class Meta:
        unique_together = ('plugin_inst', 'plugin_param',)

    def __str__(self):
        return self.value


PARAMETER_MODELS = {'string': StrParameter,
                    'integer': IntParameter,
                    'float': FloatParameter,
                    'boolean': BoolParameter,
                    'path': PathParameter,
                    'unextpath': UnextpathParameter}
