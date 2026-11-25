
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver

import django_filters
from django_filters.rest_framework import FilterSet

from core.models import AsyncDeletableModel, ChrisFolder, ChrisFile, delete_related
from core.utils import filter_files_by_n_slashes, json_zip2str
from .services import PfdcmClient
from .enums import PACS_QUERY_STATUS_CHOICES, PACS_RETRIEVE_STATUS_CHOICES


class PACS(models.Model):
    identifier = models.CharField(max_length=100, unique=True)
    active = models.BooleanField(blank=True, default=True)
    # pacs top folder
    folder = models.OneToOneField(ChrisFolder, on_delete=models.CASCADE,
                                  related_name='pacs')

    def __str__(self):
        return self.identifier


@receiver(post_delete, sender=PACS)
def auto_delete_pacs_folder_with_pacs(sender, instance, origin=None, **kwargs):
    delete_related(instance, 'folder', origin=origin)


class PACSFilter(FilterSet):

    class Meta:
        model = PACS
        fields = ['id', 'identifier', 'active']


class PACSQuery(models.Model):
    creation_date = models.DateTimeField(auto_now_add=True)
    title = models.CharField(max_length=300, db_index=True)
    query = models.JSONField()
    description = models.CharField(max_length=700, blank=True)
    execute = models.BooleanField(blank=True, default=True)
    result = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=PACS_QUERY_STATUS_CHOICES,
                              default='created')
    pacs = models.ForeignKey(PACS, on_delete=models.CASCADE, related_name='query_list')
    owner = models.ForeignKey('auth.User', on_delete=models.CASCADE)

    class Meta:
        ordering = ('pacs', 'owner', '-creation_date',)
        unique_together = ('pacs', 'owner', 'title',)

    def __str__(self):
        return self.title

    def send(self):
        """
        Custom method to send the query request to pfdcm service.
        """
        pacs_name = str(self.pacs.identifier)
        query = self.query
        pfdcm_cl = PfdcmClient()

        self.status = 'sent'
        self.save()
        try:
            result = pfdcm_cl.query(pacs_name, query)
        except Exception:
            self.status = 'errored'
            self.save()
        else:
            if result:
                self.result = json_zip2str(result)
            self.status = 'succeeded'
            self.save()


class PACSQueryFilter(FilterSet):
    min_creation_date = django_filters.IsoDateTimeFilter(field_name='creation_date',
                                                         lookup_expr='gte')
    max_creation_date = django_filters.IsoDateTimeFilter(field_name='creation_date',
                                                         lookup_expr='lte')
    title_exact = django_filters.CharFilter(field_name='title', lookup_expr='exact')
    title = django_filters.CharFilter(field_name='title', lookup_expr='icontains')
    description = django_filters.CharFilter(field_name='description',
                                            lookup_expr='icontains')
    pacs_id = django_filters.CharFilter(field_name='pacs_id', lookup_expr='exact')
    pacs_identifier = django_filters.CharFilter(field_name='pacs__identifier',
                                                lookup_expr='exact')
    owner_username = django_filters.CharFilter(field_name='owner__username',
                                                lookup_expr='exact')

    class Meta:
        model = PACSQuery
        fields = ['id', 'min_creation_date', 'max_creation_date', 'title_exact',
                  'title', 'status', 'execute', 'description', 'pacs_id',
                  'pacs_identifier', 'owner_username']


class PACSRetrieve(models.Model):
    creation_date = models.DateTimeField(auto_now_add=True)
    result = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=PACS_RETRIEVE_STATUS_CHOICES,
                              default='created')
    pacs_query = models.ForeignKey(PACSQuery, on_delete=models.CASCADE,
                                   related_name='retrieve_list')
    owner = models.ForeignKey('auth.User', on_delete=models.CASCADE)

    class Meta:
        ordering = ('pacs_query', '-creation_date',)

    def __str__(self):
        return self.pacs_query.title

    def send(self):
        """
        Custom method to send the retrieve request to pfdcm service.
        """
        pacs_query = self.pacs_query
        pacs_name = str(pacs_query.pacs.identifier)
        query = pacs_query.query
        pfdcm_cl = PfdcmClient()

        self.status = 'sent'
        self.save()
        try:
            result = pfdcm_cl.retrieve(pacs_name, query)
        except Exception:
            self.status = 'errored'
            self.save()
        else:
            if result:
                self.result = json_zip2str(result)
            self.save()


class PACSRetrieveFilter(FilterSet):
    min_creation_date = django_filters.IsoDateTimeFilter(field_name='creation_date',
                                                         lookup_expr='gte')
    max_creation_date = django_filters.IsoDateTimeFilter(field_name='creation_date',
                                                         lookup_expr='lte')
    owner_username = django_filters.CharFilter(field_name='owner__username',
                                                lookup_expr='exact')

    class Meta:
        model = PACSRetrieve
        fields = ['id', 'min_creation_date', 'max_creation_date', 'status',
                  'owner_username']


class PACSSeries(AsyncDeletableModel):
    creation_date = models.DateTimeField(auto_now_add=True)
    PatientID = models.CharField(max_length=100, db_index=True)
    PatientName = models.CharField(max_length=150, blank=True)
    PatientBirthDate = models.DateField(blank=True, null=True)
    PatientAge = models.IntegerField(blank=True, null=True)
    PatientSex = models.CharField(max_length=1, choices=[('M', 'Male'), ('F', 'Female'),
                                                         ('O', 'Other')], blank=True)
    StudyDate = models.DateField(db_index=True)
    StudyTime = models.TimeField(blank=True, null=True)
    AccessionNumber = models.CharField(max_length=100, blank=True, db_index=True)
    Modality = models.CharField(max_length=15, blank=True, db_index=True)
    Manufacturer = models.CharField(max_length=64, blank=True)
    BodyPartExamined = models.CharField(max_length=16, blank=True)
    ProtocolName = models.CharField(max_length=64, blank=True)
    StudyInstanceUID = models.CharField(max_length=100, db_index=True)
    StudyDescription = models.CharField(max_length=400, blank=True)
    SeriesInstanceUID = models.CharField(max_length=100, db_index=True)
    SeriesNumber = models.IntegerField(blank=True, null=True)
    SeriesDescription = models.CharField(max_length=400, blank=True)
    PerformedProcedureStepStartDate = models.DateField(blank=True, null=True)
    PerformedProcedureStepStartTime = models.TimeField(blank=True, null=True)
    folder = models.OneToOneField(ChrisFolder, on_delete=models.CASCADE,
                                  related_name='pacs_series')
    pacs = models.ForeignKey(PACS, on_delete=models.CASCADE, related_name='series_list')
    study = models.ForeignKey(
        'dicomweb.PACSStudy',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='series',
    )

    class Meta:
        ordering = ('pacs', 'PatientID',)
        unique_together = ('pacs', 'SeriesInstanceUID',)
        indexes = [
            models.Index(fields=['pacs', 'StudyInstanceUID'],
                         name='pacsseries_pacs_study_idx'),
        ]

    def __str__(self):
        return self.SeriesInstanceUID


@receiver(post_delete, sender=PACSSeries)
def auto_delete_series_folder_with_series(sender, instance, origin=None, **kwargs):
    delete_related(instance, 'folder', origin=origin)


class PACSSeriesFilter(FilterSet):
    min_creation_date = django_filters.IsoDateTimeFilter(field_name='creation_date',
                                                         lookup_expr='gte')
    max_creation_date = django_filters.IsoDateTimeFilter(field_name='creation_date',
                                                         lookup_expr='lte')
    PatientName = django_filters.CharFilter(field_name='PatientName',
                                            lookup_expr='icontains')
    ProtocolName = django_filters.CharFilter(field_name='ProtocolName',
                                             lookup_expr='icontains')
    StudyDescription = django_filters.CharFilter(field_name='StudyDescription',
                                                 lookup_expr='icontains')
    SeriesDescription = django_filters.CharFilter(field_name='SeriesDescription',
                                                 lookup_expr='icontains')
    min_PatientAge = django_filters.NumberFilter(field_name='PatientAge',
                                                 lookup_expr='gte')
    max_PatientAge = django_filters.NumberFilter(field_name='PatientAge',
                                                 lookup_expr='lte')
    pacs_id = django_filters.CharFilter(field_name='pacs_id', lookup_expr='exact')
    pacs_identifier = django_filters.CharFilter(field_name='pacs__identifier',
                                                lookup_expr='exact')

    class Meta:
        model = PACSSeries
        fields = ['id', 'min_creation_date', 'max_creation_date', 'PatientID',
                  'PatientName', 'PatientSex', 'PatientAge', 'min_PatientAge',
                  'max_PatientAge', 'PatientBirthDate', 'StudyDate', 'AccessionNumber',
                  'ProtocolName', 'StudyInstanceUID', 'StudyDescription',
                  'SeriesInstanceUID', 'SeriesDescription', 'pacs_id', 'pacs_identifier',
                  'deletion_status']


class PACSFile(ChrisFile):

    class Meta:
        ordering = ('-fname',)
        proxy = True

    @classmethod
    def get_base_queryset(cls):
        """
        Custom method to return a queryset that is only comprised by the files
        in the pacs space tree.
        """
        return cls.objects.filter(fname__startswith='SERVICES/PACS/')


class PACSFileFilter(FilterSet):
    min_creation_date = django_filters.IsoDateTimeFilter(field_name='creation_date',
                                                         lookup_expr='gte')
    max_creation_date = django_filters.IsoDateTimeFilter(field_name='creation_date',
                                                         lookup_expr='lte')
    fname = django_filters.CharFilter(field_name='fname', lookup_expr='startswith')
    fname_exact = django_filters.CharFilter(field_name='fname', lookup_expr='exact')
    fname_icontains = django_filters.CharFilter(field_name='fname',
                                                lookup_expr='icontains')
    fname_icontains_topdir_unique = django_filters.CharFilter(
        method='filter_by_icontains_topdir_unique')
    fname_nslashes = django_filters.CharFilter(method='filter_by_n_slashes')

    class Meta:
        model = PACSFile
        fields = ['id', 'min_creation_date', 'max_creation_date', 'fname', 'fname_exact',
                  'fname_icontains', 'fname_icontains_topdir_unique']

    def filter_by_n_slashes(self, queryset, name, value):
        """
        Custom method to return the files that have the queried number of slashes in
        their fname property. If the queried number ends in 'u' or 'U' then only one
        file per each last "folder" in the path is returned (useful to efficiently get
        the list of immediate folders under the path).
        """
        return filter_files_by_n_slashes(queryset, value)

    def filter_by_icontains_topdir_unique(self, queryset, name, value):
        """
        Custom method to return the files containing all the substrings from the queried
        string (which in turn represents a white-space-separated list of query strings)
        case insensitive anywhere in their fname. But only one file is returned per top
        level directory under SERVICES/PACS/pacs_name. This is useful to efficiently
        determine the top level directories containing a file that matches the query.
        """
        # assume value is a string representing a white-space-separated list
        # of query strings
        value_l = value.split()
        qs = queryset
        for val in value_l:
            qs = qs.filter(fname__icontains=val)
        ids = []
        hash_set = set()
        for f in qs.all():
            path = f.fname.name
            l = path.split('/', 4)  # only split 4 times
            top_dir = '/'.join(l[:4])
            if top_dir not in hash_set:
                ids.append(f.id)
                hash_set.add(top_dir)
        return qs.filter(pk__in=ids)
