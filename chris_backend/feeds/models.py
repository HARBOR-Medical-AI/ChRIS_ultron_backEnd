
from django.db import models
from django.db.models import Count, Case, When, IntegerField
from django.db.models.signals import post_delete
from django.contrib.auth.models import User, Group
from django.dispatch import receiver

import django_filters
from django_filters.rest_framework import FilterSet

from core.models import (AsyncDeletableModel, ChrisFolder, ChrisFile, ChrisLinkFile,
                         FolderGroupPermission, FolderUserPermission, FileGroupPermission,
                         FileUserPermission, LinkFileGroupPermission,
                         LinkFileUserPermission, delete_related)
from userfiles.models import UserFile


class Feed(AsyncDeletableModel):
    creation_date = models.DateTimeField(auto_now_add=True)
    modification_date = models.DateTimeField(auto_now_add=True)
    name = models.CharField(max_length=200, blank=True, db_index=True)
    public = models.BooleanField(blank=True, default=False, db_index=True)
    folder = models.OneToOneField(ChrisFolder, on_delete=models.CASCADE, null=True,
                                  related_name='feed')
    owner = models.ForeignKey('auth.User', on_delete=models.CASCADE)
    shared_groups = models.ManyToManyField(Group, related_name='shared_feeds',
                                           through='FeedGroupPermission')
    shared_users = models.ManyToManyField(User, related_name='shared_feeds',
                                          through='FeedUserPermission')

    class Meta:
        ordering = ('-creation_date',)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        """
        Overriden to save a new note to the DB the first time the feed is saved.
        """
        super(Feed, self).save(*args, **kwargs)
        if not hasattr(self, 'note'):
            self._save_note()

    def _save_note(self):
        """
        Custom method to create and save a new note to the DB.
        """
        note = Note()
        note.feed = self
        note.save()

    @staticmethod
    def add_jobs_status_count(feed_qs):
        """
        Custom static method to add the number of associated plugin instances per
        execution status to each element of a Feed queryset. The owner, folder and
        note the serializer reads for every feed are fetched in the same query.
        """
        return feed_qs.select_related('owner', 'folder', 'note').annotate(
            created_jobs=Count(Case(When(plugin_instances__status='created', then=1),
                               output_field=IntegerField())),
            waiting_jobs=Count(Case(When(plugin_instances__status='waiting', then=1),
                               output_field=IntegerField())),
            copying_jobs=Count(Case(When(plugin_instances__status='copying', then=1),
                               output_field=IntegerField())),
            scheduled_jobs=Count(Case(When(plugin_instances__status='scheduled', then=1),
                                    output_field=IntegerField())),
            started_jobs=Count(Case(When(plugin_instances__status='started', then=1),
                                    output_field=IntegerField())),
            uploading_jobs=Count(Case(When(plugin_instances__status='uploading', then=1),
                               output_field=IntegerField())),
            registering_jobs=Count(Case(When(plugin_instances__status='registeringFiles',
                                             then=1), output_field=IntegerField())),
            finished_jobs=Count(Case(When(plugin_instances__status='finishedSuccessfully',
                                          then=1), output_field=IntegerField())),
            errored_jobs=Count(Case(When(plugin_instances__status='finishedWithError',
                                         then=1), output_field=IntegerField())),
            cancelled_jobs=Count(Case(When(plugin_instances__status='cancelled', then=1),
                                    output_field=IntegerField()))
        ).order_by('-creation_date')

    def get_jobs_status_count(self):
        """
        Custom method to get the number of associated plugin instances per
        execution status.
        """
        return self.plugin_instances.aggregate(
            created_jobs=Count(Case(When(status='created', then=1),
                               output_field=IntegerField())),
            waiting_jobs=Count(Case(When(status='waiting', then=1),
                               output_field=IntegerField())),
            copying_jobs=Count(Case(When(status='copying', then=1),
                               output_field=IntegerField())),
            scheduled_jobs=Count(Case(When(status='scheduled', then=1),
                               output_field=IntegerField())),
            started_jobs=Count(Case(When(status='started', then=1),
                               output_field=IntegerField())),
            uploading_jobs=Count(Case(When(status='uploading', then=1),
                               output_field=IntegerField())),
            registering_jobs=Count(Case(When(status='registeringFiles', then=1),
                               output_field=IntegerField())),
            finished_jobs=Count(Case(When(status='finishedSuccessfully', then=1),
                               output_field=IntegerField())),
            errored_jobs=Count(Case(When(status='finishedWithError', then=1),
                               output_field=IntegerField())),
            cancelled_jobs=Count(Case(When(status='cancelled', then=1),
                               output_field=IntegerField()))
        )

    def has_group_permission(self, group):
        """
        Custom method to determine whether a group has been granted permission to access
        the feed.
        """
        return FeedGroupPermission.objects.filter(feed=self, group=group).exists()

    def has_user_permission(self, user):
        """
        Custom method to determine whether a user has been granted permission to access
        the feed (perhaps through one of its groups).
        """
        lookup = models.Q(shared_feeds=self) | models.Q(groups__shared_feeds=self)
        return User.objects.filter(username=user.username).filter(lookup).exists()

    def grant_group_permission(self, group):
        """
        Custom method to grant a group permission to access the feed and all its
        folder's descendant folders, link files and files.
        """
        FeedGroupPermission.objects.get_or_create(feed=self, group=group)

    def remove_group_permission(self, group):
        """
        Custom method to remove a group's permission to access the feed and all its
        folder's descendant folders, link files and files.
        """
        try:
            perm = FeedGroupPermission.objects.get(feed=self, group=group)
        except FeedGroupPermission.DoesNotExist:
            pass
        else:
            perm.delete()

    def grant_user_permission(self, user):
        """
        Custom method to grant a user permission to access the feed and all its
        folder's descendant folders, link files and files.
        """
        FeedUserPermission.objects.get_or_create(feed=self, user=user)

    def remove_user_permission(self, user):
        """
        Custom method to remove a user's permission to access the feed and all its
        folder's descendant folders, link files and files.
        """
        try:
            perm = FeedUserPermission.objects.get(feed=self, user=user)
        except FeedUserPermission.DoesNotExist:
            pass
        else:
            perm.delete()

    def grant_public_access(self):
        """
        Custom method to grant public access to the feed and all its folder's descendant
        folders, link files and files.
        """
        self.public = True
        self.folder.grant_public_access()
        self.folder.create_public_link()
        self.save()

    def remove_public_access(self):
        """
        Custom method to remove public access to the feed and all its folder's descendant
        folders, link files and files.
        """
        self.public = False
        self.folder.remove_public_link()
        self.folder.remove_public_access()
        self.save()


@receiver(post_delete, sender=Feed)
def auto_delete_folder_with_feed(sender, instance, origin=None, **kwargs):
    delete_related(instance, 'folder', origin=origin)


class FeedFilter(FilterSet):
    min_id = django_filters.NumberFilter(field_name="id", lookup_expr='gte')
    max_id = django_filters.NumberFilter(field_name="id", lookup_expr='lte')
    min_creation_date = django_filters.IsoDateTimeFilter(field_name="creation_date",
                                                         lookup_expr='gte')
    max_creation_date = django_filters.IsoDateTimeFilter(field_name="creation_date",
                                                         lookup_expr='lte')
    name = django_filters.CharFilter(field_name='name', lookup_expr='icontains')
    name_exact = django_filters.CharFilter(field_name='name', lookup_expr='exact')
    name_startswith = django_filters.CharFilter(field_name='name',
                                                lookup_expr='startswith')
    files_fname_icontains = django_filters.CharFilter(
        method='filter_by_fname_icontains')

    class Meta:
        model = Feed
        fields = ['id', 'name', 'name_exact', 'name_startswith', 'min_id', 'max_id',
                  'min_creation_date', 'max_creation_date', 'files_fname_icontains',
                  'deletion_status']

    def filter_by_fname_icontains(self, queryset, name, value):
        """
        Custom method to return the feeds that have files containing all the substrings
        from the queried string (which in turn represents a white-space-separated list of
        query strings) case insensitive anywhere in their fname.
        """
        # assume value is a string representing a white-space-separated list
        # of query strings
        value_l = value.split()
        qs_l = []

        for feed in queryset:
            qs = UserFile.objects.filter(fname__startswith=feed.folder.path)
            for val in value_l:
                qs = qs.filter(fname__icontains=val)
            qs_l.append(qs)

        files_qs = UserFile.objects.none().union(*qs_l)

        feed_ids = set()
        for f in files_qs:
            path = f.fname.name
            path_tokens = path.split('/', 4)
            feed_id = int(path_tokens[3].split('_')[1])
            feed_ids.add(feed_id)

        return queryset.filter(pk__in=list(feed_ids))


class FeedGroupPermission(models.Model):
    feed = models.ForeignKey(Feed, on_delete=models.CASCADE)
    group = models.ForeignKey(Group, on_delete=models.CASCADE)

    class Meta:
        unique_together = ('feed', 'group',)

    def __str__(self):
        return str(self.id)

    def save(self, *args, **kwargs):
        """
        Overriden to grant the group write permission to all the folders, files and link
        files within the feed's folder. In addition, the same permission is applied to
        all objects pointed by the linked files under the feed's folder if they are owned
        by the feed's owner.
        """
        super(FeedGroupPermission, self).save(*args, **kwargs)

        feed_folder = self.feed.folder
        feed_folder.grant_group_permission(self.group, 'w')

        linked_paths = ChrisLinkFile.objects.filter(
            fname__startswith=feed_folder.path + '/'
        ).values_list('path', flat=True)

        if linked_paths:
            linked_owned_folder_paths = ChrisFolder.objects.filter(
                path__in=linked_paths,
                owner=feed_folder.owner
            ).values_list('path', flat=True)

            if linked_owned_folder_paths:
                lookup = models.Q()

                for path in linked_owned_folder_paths:
                    lookup |= models.Q(path=path)
                    lookup |= models.Q(path__startswith=path + '/')

                folders = ChrisFolder.objects.filter(lookup)
                objs = []
                for folder in folders:
                    perm = FolderGroupPermission(folder=folder, group=self.group,
                                                 permission='w')
                    objs.append(perm)
                FolderGroupPermission.objects.bulk_create(objs, update_conflicts=True,
                                                          update_fields=['permission'],
                                                          unique_fields=['folder_id',
                                                                         'group_id'])

            lookup = models.Q(fname__in=linked_paths, owner=feed_folder.owner)
            for path in linked_owned_folder_paths:
                lookup |= models.Q(fname__startswith=path + '/')

            files = ChrisFile.objects.filter(lookup)
            objs = []
            for f in files:
                perm = FileGroupPermission(file=f, group=self.group, permission='w')
                objs.append(perm)
            FileGroupPermission.objects.bulk_create(objs, update_conflicts=True,
                                                    update_fields=['permission'],
                                                    unique_fields=['file_id', 'group_id'])

            link_files = ChrisLinkFile.objects.filter(lookup)
            objs = []
            for lf in link_files:
                perm = LinkFileGroupPermission(link_file=lf, group=self.group,
                                               permission='w')
                objs.append(perm)
            LinkFileGroupPermission.objects.bulk_create(objs, update_conflicts=True,
                                                        update_fields=['permission'],
                                                        unique_fields=['link_file_id',
                                                                       'group_id'])

    def delete(self, *args, **kwargs):
        """
        Overriden to remove the group's write permission to all the folders, files and
        link files within the feed's folder. In addition, the same permission is removed
        for all objects pointed by the linked files under the feed's folder if they are
        owned by the feed's owner.
        """
        super(FeedGroupPermission, self).delete(*args, **kwargs)

        feed_folder = self.feed.folder
        feed_folder.remove_group_permission(self.group, 'w')

        linked_paths = ChrisLinkFile.objects.filter(
            fname__startswith=feed_folder.path + '/'
        ).values_list('path', flat=True)

        if linked_paths:
            linked_owned_folder_paths = ChrisFolder.objects.filter(
                path__in=linked_paths,
                owner=feed_folder.owner
            ).values_list('path', flat=True)

            if linked_owned_folder_paths:
                lookup = models.Q()

                for path in linked_owned_folder_paths:
                    lookup |= models.Q(path=path)
                    lookup |= models.Q(path__startswith=path + '/')

                folders = ChrisFolder.objects.filter(lookup)

                FolderGroupPermission.objects.filter(folder__in=folders,
                                                     group=self.group,
                                                     permission='w').delete()

            lookup = models.Q(file__fname__in=linked_paths,
                              file__owner=feed_folder.owner,
                              group=self.group, permission='w')

            for path in linked_owned_folder_paths:
                lookup |= models.Q(file__fname__startswith=path + '/',
                                   group=self.group, permission='w')

            FileGroupPermission.objects.filter(lookup).delete()

            lookup = models.Q(link_file__fname__in=linked_paths,
                              link_file__owner=feed_folder.owner,
                              group=self.group, permission='w')

            for path in linked_owned_folder_paths:
                lookup |= models.Q(link_file__fname__startswith=path + '/',
                                   group=self.group, permission='w')

            LinkFileGroupPermission.objects.filter(lookup).delete()


class FeedGroupPermissionFilter(FilterSet):
    group_name = django_filters.CharFilter(field_name='group__name', lookup_expr='exact')

    class Meta:
        model = FeedGroupPermission
        fields = ['id', 'group_name']


class FeedUserPermission(models.Model):
    feed = models.ForeignKey(Feed, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)

    class Meta:
        unique_together = ('feed', 'user',)

    def __str__(self):
        return str(self.id)

    def save(self, *args, **kwargs):
        """
        Overriden to grant the user write permission to all the folders, files and link
        files within the feed's folder. In addition, the same permission is applied to
        all objects pointed by the linked files under the feed's folder if they are owned
        by the feed's owner.
        """
        super(FeedUserPermission, self).save(*args, **kwargs)

        feed_folder = self.feed.folder
        feed_folder.grant_user_permission(self.user, 'w')

        linked_paths = ChrisLinkFile.objects.filter(
            fname__startswith=feed_folder.path + '/'
        ).values_list('path', flat=True)

        if linked_paths:
            linked_owned_folder_paths = ChrisFolder.objects.filter(
                path__in=linked_paths,
                owner=feed_folder.owner
            ).values_list('path', flat=True)

            if linked_owned_folder_paths:
                lookup = models.Q()

                for path in linked_owned_folder_paths:
                    lookup |= models.Q(path=path)
                    lookup |= models.Q(path__startswith=path + '/')

                folders = ChrisFolder.objects.filter(lookup)
                objs = []
                for folder in folders:
                    perm = FolderUserPermission(folder=folder, user=self.user,
                                                permission='w')
                    objs.append(perm)
                FolderUserPermission.objects.bulk_create(objs, update_conflicts=True,
                                                          update_fields=['permission'],
                                                          unique_fields=['folder_id',
                                                                         'user_id'])

            lookup = models.Q(fname__in=linked_paths, owner=feed_folder.owner)
            for path in linked_owned_folder_paths:
                lookup |= models.Q(fname__startswith=path + '/')

            files = ChrisFile.objects.filter(lookup)
            objs = []
            for f in files:
                perm = FileUserPermission(file=f, user=self.user, permission='w')
                objs.append(perm)
            FileUserPermission.objects.bulk_create(objs, update_conflicts=True,
                                                    update_fields=['permission'],
                                                    unique_fields=['file_id', 'user_id'])

            link_files = ChrisLinkFile.objects.filter(lookup)
            objs = []
            for lf in link_files:
                perm = LinkFileUserPermission(link_file=lf, user=self.user, permission='w')
                objs.append(perm)
            LinkFileUserPermission.objects.bulk_create(objs, update_conflicts=True,
                                                        update_fields=['permission'],
                                                        unique_fields=['link_file_id',
                                                                       'user_id'])

    def delete(self, *args, **kwargs):
        """
        Overriden to remove the user's write permission to all the folders, files and
        link files within the feed's folder. In addition, the same permission is removed
        for all objects pointed by the linked files under the feed's folder if they are
        owned by the feed's owner.
        """
        super(FeedUserPermission, self).delete(*args, **kwargs)

        feed_folder = self.feed.folder
        feed_folder.remove_user_permission(self.user, 'w')

        linked_paths = ChrisLinkFile.objects.filter(
            fname__startswith=feed_folder.path + '/'
        ).values_list('path', flat=True)

        if linked_paths:
            linked_owned_folder_paths = ChrisFolder.objects.filter(
                path__in=linked_paths,
                owner=feed_folder.owner
            ).values_list('path', flat=True)

            if linked_owned_folder_paths:
                lookup = models.Q()

                for path in linked_owned_folder_paths:
                    lookup |= models.Q(path=path)
                    lookup |= models.Q(path__startswith=path + '/')

                folders = ChrisFolder.objects.filter(lookup)
                FolderUserPermission.objects.filter(folder__in=folders,
                                                    user=self.user,
                                                    permission='w').delete()

            lookup = models.Q(file__fname__in=linked_paths,
                              file__owner=feed_folder.owner,
                              user=self.user, permission='w')

            for path in linked_owned_folder_paths:
                lookup |= models.Q(file__fname__startswith=path + '/',
                                   user=self.user, permission='w')

            FileUserPermission.objects.filter(lookup).delete()

            lookup = models.Q(link_file__fname__in=linked_paths,
                              link_file__owner=feed_folder.owner,
                              user=self.user, permission='w')

            for path in linked_owned_folder_paths:
                lookup |= models.Q(link_file__fname__startswith=path + '/',
                                   user=self.user, permission='w')

            LinkFileUserPermission.objects.filter(lookup).delete()


class FeedUserPermissionFilter(FilterSet):
    username = django_filters.CharFilter(field_name='user__username', lookup_expr='exact')

    class Meta:
        model = FeedUserPermission
        fields = ['id', 'username']


class Note(models.Model):
    creation_date = models.DateTimeField(auto_now_add=True)
    modification_date = models.DateTimeField(auto_now_add=True)
    title = models.CharField(max_length=100, blank=True)
    content = models.TextField(blank=True)
    feed = models.OneToOneField(Feed, on_delete=models.CASCADE, related_name='note')

    def __str__(self):
        return self.title


class Tag(models.Model):
    name = models.CharField(max_length=100, blank=True)
    color = models.CharField(max_length=20)
    feeds = models.ManyToManyField(Feed, related_name='tags',
                                   through='Tagging')
    owner = models.ForeignKey('auth.User', on_delete=models.CASCADE)

    def __str__(self):
        return self.name


class TagFilter(FilterSet):
    name = django_filters.CharFilter(field_name='name', lookup_expr='icontains')
    color = django_filters.CharFilter(field_name='color', lookup_expr='icontains')
    owner_username = django_filters.CharFilter(field_name='owner__username',
                                               lookup_expr='exact')

    class Meta:
        model = Tag
        fields = ['id', 'name', 'color', 'owner_username']


class Tagging(models.Model):
    feed = models.ForeignKey(Feed, on_delete=models.CASCADE)
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE)

    class Meta:
        unique_together = ('feed', 'tag',)

    def __str__(self):
        return str(self.id)


class Comment(models.Model):
    creation_date = models.DateTimeField(auto_now_add=True)
    title = models.CharField(max_length=100, blank=True)
    content = models.TextField(blank=True)
    feed = models.ForeignKey(Feed, on_delete=models.CASCADE, related_name='comments')
    owner = models.ForeignKey('auth.User', on_delete=models.CASCADE)

    class Meta:
        ordering = ('-creation_date',)

    def __str__(self):
        return self.title


class CommentFilter(FilterSet):

    class Meta:
        model = Comment
        fields = ['id']
