
import django_filters
from django_filters.rest_framework import FilterSet

from core.models import ChrisFolder, ChrisFile
from core.utils import filter_files_by_n_slashes


class UserFile(ChrisFile):

    class Meta:
        ordering = ('-fname',)
        proxy = True

    @classmethod
    def get_base_queryset(cls):
        """
        Custom method to return a queryset that is only comprised by the files
        in the user space tree.
        """
        return cls.objects.filter(fname__startswith='home/')


class UserFileFilter(FilterSet):
    min_creation_date = django_filters.IsoDateTimeFilter(field_name='creation_date',
                                                         lookup_expr='gte')
    max_creation_date = django_filters.IsoDateTimeFilter(field_name='creation_date',
                                                         lookup_expr='lte')
    fname = django_filters.CharFilter(field_name='fname', lookup_expr='startswith')
    fname_exact = django_filters.CharFilter(field_name='fname', lookup_expr='exact')
    fname_icontains = django_filters.CharFilter(field_name='fname',
                                                lookup_expr='icontains')
    fname_icontains_multiple = django_filters.CharFilter(
        method='filter_by_icontains_multiple')
    fname_nslashes = django_filters.CharFilter(method='filter_by_n_slashes')
    owner_username = django_filters.CharFilter(field_name='owner__username',
                                               lookup_expr='exact')

    class Meta:
        model = UserFile
        fields = ['id', 'min_creation_date', 'max_creation_date', 'fname', 'fname_exact',
                  'fname_icontains', 'fname_nslashes', 'owner_username']

    def filter_by_n_slashes(self, queryset, name, value):
        """
        Custom method to return the files that have the queried number of slashes in
        their fname property. If the queried number ends in 'u' or 'U' then only one
        file per each last "folder" in the path is returned (useful to efficiently get
        the list of immediate folders under the path).
        """
        return filter_files_by_n_slashes(queryset, value)

    def filter_by_icontains_multiple(self, queryset, name, value):
        """
        Custom method to return the files containing all the substrings from the queried
        string (which in turn represents a white-space-separated list of query strings)
        case-insensitive anywhere in their fname.
        """
        # assume value is a string representing a white-space-separated list
        # of query strings
        value_l = value.split()
        qs = queryset
        for val in value_l:
            qs = qs.filter(fname__icontains=val)
        return qs
