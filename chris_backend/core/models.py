
import logging
import uuid
import os
import pathlib
from contextlib import contextmanager

from celery.exceptions import SoftTimeLimitExceeded
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError, models, router, transaction
from django.db.models.deletion import Collector
from django.db.models.functions import Length
from django.db.models.signals import post_delete
from django.utils import timezone
from psycopg.errors import UniqueViolation
from django.dispatch import receiver
from django.conf import settings
from django.contrib.auth.models import User, Group

import django_filters
from django_filters.rest_framework import FilterSet

from .storage import connect_storage, is_plain_path
# from django.core.files.base import ContentFile


logger = logging.getLogger(__name__)


PERMISSION_CHOICES = [("r", "Read"), ("w", "Write")]


def validate_permission(permission):
    """
    Custom function to determine whether a permission value is valid.
    """
    perm_list = [p[0] for p in PERMISSION_CHOICES]
    if permission not in perm_list:
        raise ValueError(f"Invalid permission '{permission}'. Allowed values "
                         f"are: {perm_list}.")
    return permission


def user_can_access_obj(obj, user):
    """
    Return whether the given user is allowed to read-access the given storage
    object (a ``ChrisFolder``, ``ChrisFile`` or ``ChrisLinkFile``).

    This is the canonical owner/superuser/public/permission read-access rule.
    The object is accessible if the user owns it, is the superuser 'chris', the
    object is public, or the user has been granted any permission to it
    (possibly through one of their groups).
    """
    return (obj.owner == user or user.username == 'chris' or obj.public
            or obj.has_user_permission(user))


class PathAccessError(Exception):
    """
    Raised by ``validate_path_access`` when a user is not allowed to access a
    storage path. The exception message is a human-readable reason suitable for
    surfacing to API clients.
    """
    pass


def validate_path_access(user, path):
    """
    Check whether the given user is allowed to access the given storage path.

    Enforces the structural path rules, the PUBLIC/SHARED link-target restriction
    and the owner/public/permission access rule. This is the single source of
    truth for path-access authorization.

    Returns the normalized path on success.
    Raises ``PathAccessError`` with a human-readable reason on failure.
    """
    path = path.strip().strip('/')
    if not is_plain_path(path):
        raise PathAccessError(f"This field may not reference an invalid path '{path}'.")
    path_parts = pathlib.Path(path).parts

    if len(path_parts) < 2:
        # trying to access a top-level folder or an unknown folder
        raise PathAccessError(
            f"This field may not reference a top-level folder path '{path}'.")

    if path_parts[0] not in ('home', 'SERVICES', 'PIPELINES'):
        raise PathAccessError(
            f"This field may not reference an invalid path '{path}'.")

    if len(path_parts) == 2 and path_parts[0] == 'home':
        raise PathAccessError(
            f"This field may not reference a home folder path '{path}'.")

    if len(path_parts) == 3 and path_parts[0] == 'home' and path_parts[2] == 'feeds':
        raise PathAccessError(
            f"This field may not reference a home's feeds folder path '{path}'.")

    try:
        obj = ChrisFolder.objects.get(path=path)
    except ChrisFolder.DoesNotExist:  # path is not a folder
        try:
            obj = ChrisFile.objects.get(fname=path)
        except ChrisFile.DoesNotExist:  # path is not a file
            try:
                obj = ChrisLinkFile.objects.get(fname=path)

                if obj.path in ('PUBLIC', 'SHARED'):
                    raise PathAccessError(
                        f"This field may not reference an invalid path '{path}'.")

            except ChrisLinkFile.DoesNotExist:  # path is not a link file
                raise PathAccessError(
                    f"This field may not reference an invalid path '{path}'.")

    if not user_can_access_obj(obj, user):
        raise PathAccessError(
            f"User does not have permission to access path '{path}'.")
    return path


class AsyncDeletableModel(models.Model):
    class DeletionStatus(models.TextChoices):
        INACTIVE = 'inactive'
        PENDING = 'pending'
        FAILED = 'failed'

    deletion_status = models.CharField(max_length=10, choices=DeletionStatus.choices,
                                       default=DeletionStatus.INACTIVE)
    deletion_requested_at = models.DateTimeField(null=True, blank=True)
    deletion_error = models.TextField(blank=True)

    class Meta:
        abstract = True

    def is_pending_deletion(self) -> bool:
        return self.deletion_status == self.DeletionStatus.PENDING

    def mark_deletion_pending(self) -> bool:
        """
        Returns True if deletion was scheduled by this call.
        Returns False if it was already pending.
        """
        if self.is_pending_deletion():
            return False

        self.deletion_status = self.DeletionStatus.PENDING
        self.deletion_requested_at = timezone.now()
        self.save(update_fields=['deletion_status', 'deletion_requested_at'])
        return True

    def mark_deletion_failed(self, error: Exception | str):
        self.deletion_status = self.DeletionStatus.FAILED
        self.deletion_error = str(error)
        self.save(update_fields=['deletion_status', 'deletion_error'])


class ChrisInstance(models.Model):
    """
    Model class that defines a singleton representing a ChRIS instance.
    """
    creation_date = models.DateTimeField(auto_now_add=True)
    name = models.CharField(max_length=100, default="ChRIS instance")
    uuid = models.UUIDField(default=uuid.uuid4)
    job_id_prefix = models.CharField(max_length=100, blank=True, default='chris-jid-')
    description = models.CharField(max_length=600, blank=True)

    class Meta:
        verbose_name = 'ChRIS instance'
        verbose_name_plural = 'ChRIS instance'

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        count = ChrisInstance.objects.all().count()
        if count > 0:
            self.id = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls):
        try:
            obj = cls.objects.get(id=1)
        except cls.DoesNotExist:
            obj = cls()
            obj.save()
        return obj


class ChrisFolder(AsyncDeletableModel):
    creation_date = models.DateTimeField(auto_now_add=True)
    path = models.CharField(max_length=1024, unique=True)  # folder's path
    public = models.BooleanField(blank=True, default=False, db_index=True)
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True,
                               related_name='children')
    owner = models.ForeignKey('auth.User', on_delete=models.CASCADE)
    shared_groups = models.ManyToManyField(Group, related_name='shared_folders',
                                           through='FolderGroupPermission')
    shared_users = models.ManyToManyField(User, related_name='shared_folders',
                                          through='FolderUserPermission')

    class Meta:
        ordering = ('-path',)

    def __str__(self):
        return self.path

    def save(self, *args, **kwargs):
        """
        Overriden to recursively create parent folders when first saving the folder
        to the DB. A path (but the root's, the empty one) must be plain
        (``is_plain_path``).
        """
        if self.path:
            if not is_plain_path(self.path):
                raise ValueError(f"The path '{self.path}' is not plain: it starts or ends "
                                 f"with a slash, or a name in it is empty, '.' or '..'.")

            parent_path = os.path.dirname(self.path)
            try:
                parent = ChrisFolder.objects.get(path=parent_path)
            except ChrisFolder.DoesNotExist:
                parent = ChrisFolder(path=parent_path, owner=self.owner)
                parent.save()  # recursive call
            self.parent = parent

        if self.path in ('', 'home', 'PUBLIC', 'SHARED') or self.path.startswith(
                ('PIPELINES', 'SERVICES')):
            self.owner = User.objects.get(username='chris')
        super(ChrisFolder, self).save(*args, **kwargs)

    def move(self, new_path):
        """
        Move the folder's tree to ``new_path``: its objects in storage, then the rows of
        the folders, files and link files in it, in one transaction; when that fails the
        objects move back (best effort) and the folder keeps its path. A path in use
        (``path_in_use``) is refused, and the folder's own path is nothing to do.
        """
        new_path = new_path.strip('/')
        path = str(self.path)

        if new_path == path:
            return
        if path_in_use(new_path):
            raise ValueError(f"Path '{new_path}' is in use.")

        storage_manager = connect_storage(settings)
        storage_manager.move_path(path + '/', new_path + '/')  # the folder's tree, only
        try:
            with transaction.atomic(durable=True):
                self._move_rows(path, new_path)
        except Exception:
            self.path = path
            with _storage_errors_logged(f'moving folder {new_path} back to {path}'):
                storage_manager.move_path(new_path + '/', path + '/')
            raise

    def _move_rows(self, path, new_path):
        """
        Give this folder and the folders, files and link files in it the paths they have
        under ``new_path`` instead of ``path``, and the folder its new parent.
        """
        prefix = path + '/'  # avoid sibling folders with paths that start with path

        folders = [self] + list(ChrisFolder.objects.filter(path__startswith=prefix))
        for folder in folders:
            folder.path = folder.path.replace(path, new_path, 1)
        ChrisFolder.objects.bulk_update(folders, ['path'])

        files = list(ChrisFile.objects.filter(fname__startswith=prefix))
        for f in files:
            f.fname.name = f.fname.name.replace(path, new_path, 1)
        ChrisFile.objects.bulk_update(files, ['fname'])

        link_files = list(ChrisLinkFile.objects.filter(fname__startswith=prefix))
        for lf in link_files:
            lf.fname.name = lf.fname.name.replace(path, new_path, 1)
        ChrisLinkFile.objects.bulk_update(link_files, ['fname'])

        if os.path.dirname(new_path) != os.path.dirname(path):
            self.save(update_fields=['parent'])  # save finds the parent, or creates it

    def delete_contents(self):
        """
        Delete the folders, files and link files under this folder, their rows and their
        objects, and keep the folder: one deletion that starts at this folder, so that
        what it deletes leaves its objects alone, as when the folder is deleted
        (``_under_deleted_folder``), then the folder's tree in storage at once.
        """
        collector = Collector(using=router.db_for_write(ChrisFolder), origin=self)
        collector.collect(self.children.all())
        collector.collect(self.chris_files.all())
        collector.collect(self.chris_link_files.all())
        collector.delete()
        connect_storage(settings).delete_path(self.path + '/')  # the folder's, only

    def get_descendants(self):
        """
        Custom method to return all the folders that are a descendant of this
        folder (including itself).
        """
        path = str(self.path)
        return [self] + list(ChrisFolder.objects.filter(path__startswith=path + '/'))

    def has_group_permission(self, group, permission=''):
        """
        Custom method to determine whether a group has been granted a permission
        to access the folder.
        """
        if not permission:
            qs = FolderGroupPermission.objects.filter(group=group, folder=self)
        else:
            p = validate_permission(permission)
            qs = FolderGroupPermission.objects.filter(group=group, folder=self,
                                                      permission=p)
        return qs.exists()

    def has_user_permission(self, user, permission=''):
        """
        Custom method to determine whether a user has been granted a permission
        to access the folder (perhaps through one of its groups).
        """
        grp_qs = user.groups.all()

        if not permission:
            if FolderUserPermission.objects.filter(folder=self, user=user).exists():
                return True
            qs = FolderGroupPermission.objects.filter(folder=self, group__in=grp_qs)
        else:
            p = validate_permission(permission)
            if FolderUserPermission.objects.filter(folder=self, user=user,
                                                   permission=p).exists():
                return True
            qs = FolderGroupPermission.objects.filter(folder=self, permission=p,
                                                      group__in=grp_qs)
        return qs.exists()

    def get_groups_permissions_queryset(self):
        """
        Custom method to get the queryset of permissions granted to groups to access the
        folder.
        """
        return FolderGroupPermission.objects.filter(folder=self)

    def get_users_permissions_queryset(self):
        """
        Custom method to get the queryset of permissions granted to users to access the
        folder.
        """
        return FolderUserPermission.objects.filter(folder=self)

    def grant_group_permission(self, group, permission):
        """
        Custom method to grant a group a permission to access the folder and all its
        descendant folders, link files and files.
        """
        FolderGroupPermission.objects.update_or_create(folder=self, group=group,
                                                       defaults={'permission': permission})

    def remove_group_permission(self, group, permission):
        """
        Custom method to remove a group's permission to access the folder and all its
        descendant folders, link files and files.
        """
        FolderGroupPermission.objects.get(folder=self, group=group,
                                          permission=permission).delete()

    def grant_user_permission(self, user, permission):
        """
        Custom method to grant a user a permission to access the folder and all its
        descendant folders, link files and files.
        """
        FolderUserPermission.objects.update_or_create(folder=self, user=user,
                                                      defaults={'permission': permission})

    def remove_user_permission(self, user, permission):
        """
        Custom method to remove a user's permission to access the folder and all its
        descendant folders, link files and files.
        """
        FolderUserPermission.objects.get(folder=self, user=user,
                                         permission=permission).delete()

    def grant_public_access(self):
        """
        Custom method to grant public access to the folder and all its descendant folders,
        link files and files.
        """
        self._update_public_access(True)

    def remove_public_access(self):
        """
        Custom method to remove public access to the folder and all its descendant
        folders, link files and files.
        """
        self._update_public_access(False)

    def get_shared_link(self):
        """
        Custom method to get the link file in the SHARED folder pointing to
        this folder if it exists.
        """
        path = str(self.path)
        str_source_trace_dir = path.replace('/', '_')
        fname = 'SHARED/' + str_source_trace_dir + '.chrislink'

        try:
            lf = ChrisLinkFile.objects.get(fname=fname)
        except ChrisLinkFile.DoesNotExist:
            return None
        return lf

    def create_shared_link(self):
        """
        Custom method to create a link file in the SHARED folder pointing to
        this folder.
        """
        path = str(self.path)
        str_source_trace_dir = path.replace('/', '_')
        fname = 'SHARED/' + str_source_trace_dir + '.chrislink'

        try:
            lf = ChrisLinkFile.objects.get(fname=fname)
        except ChrisLinkFile.DoesNotExist:
            shared_folder = ChrisFolder.objects.get(path='SHARED')
            lf = ChrisLinkFile(path=path, owner=self.owner, parent_folder=shared_folder)
            lf.save(name=str_source_trace_dir)
        return lf

    def remove_shared_link(self):
        """
        Custom method to remove a link file in the SHARED folder pointing to
        this folder if it exists.
        """
        path = str(self.path)
        fname = 'SHARED/' + path.replace('/', '_') + '.chrislink'
        try:
            lf = ChrisLinkFile.objects.get(fname=fname)
        except ChrisLinkFile.DoesNotExist:
            pass
        else:
            lf.delete()

    def create_public_link(self):
        """
        Custom method to create a public link file in the PUBLIC folder pointing to
        this folder.
        """
        path = str(self.path)
        str_source_trace_dir = path.replace('/', '_')
        fname = 'PUBLIC/' + str_source_trace_dir + '.chrislink'

        try:
            lf = ChrisLinkFile.objects.get(fname=fname)
        except ChrisLinkFile.DoesNotExist:
            public_folder = ChrisFolder.objects.get(path='PUBLIC')
            lf = ChrisLinkFile(path=path, owner=self.owner, public=True,
                               parent_folder=public_folder)
            lf.save(name=str_source_trace_dir)

    def remove_public_link(self):
        """
        Custom method to remove a public link file in the PUBLIC folder pointing to
        this folder if it exists.
        """
        path = str(self.path)
        fname = 'PUBLIC/' + path.replace('/', '_') + '.chrislink'

        try:
            lf = ChrisLinkFile.objects.get(fname=fname)
        except ChrisLinkFile.DoesNotExist:
            pass
        else:
            lf.delete()

    def _update_public_access(self, public_tf):
        """
        Internal method to update public access to the folder and all its descendant
        folders, link files and files.
        """
        path = str(self.path)
        prefix = path + '/'  # avoid sibling folders with paths that start with path

        folders = [self] + list(ChrisFolder.objects.filter(path__startswith=prefix))
        for folder in folders:
            folder.public = public_tf
        ChrisFolder.objects.bulk_update(folders, ['public'])

        files = list(ChrisFile.objects.filter(fname__startswith=prefix))
        for f in files:
            f.public = public_tf
        ChrisFile.objects.bulk_update(files, ['public'])

        link_files = list(ChrisLinkFile.objects.filter(fname__startswith=prefix))
        for lf in link_files:
            lf.public = public_tf
        ChrisLinkFile.objects.bulk_update(link_files, ['public'])

    @classmethod
    def get_first_existing_folder_ancestor(cls, path):
        """
        Custom class method to return the closest ancestor folder (by path prefix
        including the passed path itself) that exists in the DB.
        """
        if not path:
            try:
                return cls.objects.get(path='')
            except cls.DoesNotExist:
                return None

        parts = path.strip().strip('/').split('/')
        ancestor_paths = ['/'.join(parts[:i]) for i in range(len(parts), 0, -1)]
        ancestor_paths.append('')

        return (
            cls.objects
            .filter(path__in=ancestor_paths)
            .annotate(path_length=Length('path'))
            .order_by('-path_length')
            .first()
        )


def _under_deleted_folder(path, origin):
    """
    Whether ``path`` is under the folder that a deletion started at (``origin``, which
    Django passes to the signals of a deletion: the object or queryset whose ``delete``
    was called). The folder's tree is deleted from storage at once, by its receiver or
    by ``ChrisFolder.delete_contents``, so the files, link files and folders deleted
    under it skip their own storage calls: a feed of a million files is one
    ``delete_path`` call, not a million calls, one per receiver (in storage, a request
    per 1,000 objects on S3, still one per object on Swift).
    """
    return isinstance(origin, ChrisFolder) and path.startswith(origin.path + '/')


@contextmanager
def _storage_errors_logged(doing):
    """
    Log a storage error that the block raises rather than raise it, for what runs after
    the rows it is about are gone (``doing`` says what it does): a soft time limit still
    ends the task. A receiver of a deletion runs inside the deletion's transaction, and
    other receivers of that deletion may have deleted their objects already: raising
    would roll back their rows, which would then name objects that are gone.
    """
    try:
        yield
    except SoftTimeLimitExceeded:
        raise
    except Exception as e:
        logger.error('Storage error while %s, detail: %s', doing, e)


@receiver(post_delete, sender=ChrisFolder)
def auto_delete_folder_from_storage(sender, instance, origin=None, **kwargs):
    """
    Delete a deleted folder's tree from storage (the folder only: never an object that
    has the folder's path), unless the deletion started at a folder above it
    (``_under_deleted_folder``). A storage error is logged (``_storage_errors_logged``).
    """
    if _under_deleted_folder(instance.path, origin):
        return
    with _storage_errors_logged(f'deleting folder {instance.path}'):
        connect_storage(settings).delete_path(instance.path + '/')


def delete_related(instance, *names, origin=None):
    """
    Delete the object that ``instance`` reaches through the relations ``names`` (its
    ``folder``, its ``output_folder``'s ``parent``...), for a receiver of the deletion of
    ``instance``, which started at ``origin``. One that is not there is nothing to
    delete: a relation never set, or an object deleted already by the same deletion,
    which deleted ``instance`` with it. Nor is ``origin`` itself, which its deletion
    deletes: deleting a feed's folder deletes the feed, whose receiver would delete the
    folder again. Any other error is raised: within the deletion's transaction, a
    database error left unraised would only fail the next statement, as an aborted
    transaction.
    """
    related = instance
    for name in names:
        try:
            related = getattr(related, name)
        except ObjectDoesNotExist:
            return
        if related is None:
            return
    if related != origin:
        related.delete()


def path_in_use(path):
    """
    Whether a file, a link file or a folder has ``path``, or a file or a link file has a
    path above it, where a folder would have to be: a file moved or uploaded there, or a
    folder made there, would take an object's place or make a folder of a file's path.
    """
    parts = path.split('/')
    paths = ['/'.join(parts[:i]) for i in range(1, len(parts) + 1)]  # path and above
    return (ChrisFile.objects.filter(fname__in=paths).exists()
            or ChrisLinkFile.objects.filter(fname__in=paths).exists()
            or ChrisFolder.objects.filter(path=path).exists())


def _stored(field_file):
    """
    Whether the contents of a file field are in storage: a FieldFile given new contents
    is not ``_committed`` until the model's save uploads them (read through getattr, as
    django-stubs does not declare it).
    """
    return getattr(field_file, '_committed', True)


def _folder_for(path, owner, folder):
    """
    The folder of ``path``: ``folder`` when it is that, else the folder at the path's
    parent, created (with its missing ancestors) for ``owner`` when there is none.
    """
    folder_path = os.path.dirname(path)
    if folder.path == folder_path:
        return folder
    return ChrisFolder.objects.get_or_create(path=folder_path,
                                             defaults={'owner': owner})[0]


def _name_taken(error):
    """
    Whether ``error`` is the database refusing a row for a unique value that another
    row has (for a file, its name): what is stored under that name is that row's.
    """
    return (isinstance(error, IntegrityError)
            and isinstance(error.__cause__, UniqueViolation))


def _discard(path, after):
    """
    Delete the object at ``path``, which is no row's after ``after`` (a failed save, a
    move). A storage error is logged (``_storage_errors_logged``): what ran before is
    done, or its own error is the one to raise.
    """
    with _storage_errors_logged(f'deleting {path} after {after}'):
        connect_storage(settings).delete_obj(path)


class ChrisFolderFilter(FilterSet):
    path = django_filters.CharFilter(field_name='path')

    class Meta:
        model = ChrisFolder
        fields = ['id', 'path']


class FolderGroupPermission(models.Model):
    permission = models.CharField(choices=PERMISSION_CHOICES, default='r', max_length=1)
    folder = models.ForeignKey(ChrisFolder, on_delete=models.CASCADE)
    group = models.ForeignKey(Group, on_delete=models.CASCADE)

    class Meta:
        unique_together = ('folder', 'group',)

    def __str__(self):
        return self.permission

    def save(self, *args, **kwargs):
        """
        Overriden to grant the group permission to all the folders, files and link
        files within the folder.
        """
        super(FolderGroupPermission, self).save(*args, **kwargs)

        group = self.group
        permission = self.permission
        path = str(self.folder.path)
        prefix = path + '/'  # avoid sibling folders with paths that start with path

        folders = ChrisFolder.objects.filter(path__startswith=prefix)
        objs = []
        for folder in folders:
            perm = FolderGroupPermission(folder=folder, group=group,
                                         permission=permission)
            objs.append(perm)
        FolderGroupPermission.objects.bulk_create(objs, update_conflicts=True,
                                                  update_fields=['permission'],
                                                  unique_fields=['folder_id', 'group_id'])

        files = ChrisFile.objects.filter(fname__startswith=prefix)
        objs = []
        for f in files:
            perm = FileGroupPermission(file=f, group=group, permission=permission)
            objs.append(perm)
        FileGroupPermission.objects.bulk_create(objs, update_conflicts=True,
                                                update_fields=['permission'],
                                                unique_fields=['file_id', 'group_id'])

        link_files = ChrisLinkFile.objects.filter(fname__startswith=prefix)
        objs = []
        for lf in link_files:
            perm = LinkFileGroupPermission(link_file=lf, group=group,
                                           permission=permission)
            objs.append(perm)
        LinkFileGroupPermission.objects.bulk_create(objs, update_conflicts=True,
                                                    update_fields=['permission'],
                                                    unique_fields=['link_file_id',
                                                                   'group_id'])

    def delete(self, *args, **kwargs):
        """
        Overriden to remove the group permission to all the folders, files and
        link files within the folder.
        """
        super(FolderGroupPermission, self).delete(*args, **kwargs)

        group = self.group
        permission = self.permission
        path = str(self.folder.path)
        prefix = path + '/'  # avoid sibling folders with paths that start with path

        FolderGroupPermission.objects.filter(folder__path__startswith=prefix, group=group,
                                             permission=permission).delete()

        FileGroupPermission.objects.filter(file__fname__startswith=prefix, group=group,
                                           permission=permission).delete()

        LinkFileGroupPermission.objects.filter(link_file__fname__startswith=prefix,
                                               group=group,
                                               permission=permission).delete()


class FolderGroupPermissionFilter(FilterSet):
    group_name = django_filters.CharFilter(field_name='group__name', lookup_expr='exact')

    class Meta:
        model = FolderGroupPermission
        fields = ['id', 'group_name']


class FolderUserPermission(models.Model):
    permission = models.CharField(choices=PERMISSION_CHOICES, default='r', max_length=1)
    folder = models.ForeignKey(ChrisFolder, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)

    class Meta:
        unique_together = ('folder', 'user',)

    def __str__(self):
        return self.permission

    def save(self, *args, **kwargs):
        """
        Overriden to grant the user permission to all the folders, files and link
        files within the folder.
        """
        super(FolderUserPermission, self).save(*args, **kwargs)

        user = self.user
        permission = self.permission
        path = str(self.folder.path)
        prefix = path + '/'  # avoid sibling folders with paths that start with path

        folders = ChrisFolder.objects.filter(path__startswith=prefix)
        objs = []
        for folder in folders:
            perm = FolderUserPermission(folder=folder, user=user, permission=permission)
            objs.append(perm)
        FolderUserPermission.objects.bulk_create(objs, update_conflicts=True,
                                                 update_fields=['permission'],
                                                 unique_fields=['folder_id', 'user_id'])

        files = ChrisFile.objects.filter(fname__startswith=prefix)
        objs = []
        for f in files:
            perm = FileUserPermission(file=f, user=user, permission=permission)
            objs.append(perm)
        FileUserPermission.objects.bulk_create(objs, update_conflicts=True,
                                               update_fields=['permission'],
                                               unique_fields=['file_id', 'user_id'])

        link_files = ChrisLinkFile.objects.filter(fname__startswith=prefix)
        objs = []
        for lf in link_files:
            perm = LinkFileUserPermission(link_file=lf, user=user, permission=permission)
            objs.append(perm)
        LinkFileUserPermission.objects.bulk_create(objs, update_conflicts=True,
                                                   update_fields=['permission'],
                                                   unique_fields=['link_file_id',
                                                                  'user_id'])

    def delete(self, *args, **kwargs):
        """
        Overriden to remove the user permission to all the folders, files and
        link files within the folder.
        """
        super(FolderUserPermission, self).delete(*args, **kwargs)

        user = self.user
        permission = self.permission
        path = str(self.folder.path)
        prefix = path + '/'  # avoid sibling folders with paths that start with path

        FolderUserPermission.objects.filter(folder__path__startswith=prefix, user=user,
                                            permission=permission).delete()

        FileUserPermission.objects.filter(file__fname__startswith=prefix, user=user,
                                          permission=permission).delete()

        LinkFileUserPermission.objects.filter(link_file__fname__startswith=prefix,
                                              user=user, permission=permission).delete()


class FolderUserPermissionFilter(FilterSet):
    username = django_filters.CharFilter(field_name='user__username', lookup_expr='exact')

    class Meta:
        model = FolderUserPermission
        fields = ['id', 'username']


class ChrisFile(models.Model):
    creation_date = models.DateTimeField(auto_now_add=True)
    fname = models.FileField(max_length=1024, unique=True)
    public = models.BooleanField(blank=True, default=False, db_index=True)
    parent_folder = models.ForeignKey(ChrisFolder, on_delete=models.CASCADE,
                                      related_name='chris_files')
    owner = models.ForeignKey('auth.User', on_delete=models.CASCADE)
    shared_groups = models.ManyToManyField(Group, related_name='shared_files',
                                           through='FileGroupPermission')
    shared_users = models.ManyToManyField(User, related_name='shared_files',
                                          through='FileUserPermission')

    class Meta:
        ordering = ('-fname',)

    def __str__(self):
        return self.fname.name

    def save(self, *args, **kwargs):
        """
        Overriden to ensure file paths are plain (``is_plain_path``). Also, when the
        save stores the file's contents (an upload) and then its row fails, to delete
        what it stored, under the name the storage gave it: a name no object had, as no
        storage of CUBE's lets an upload replace an object it finds. Nothing else: a row
        saved again, or one for an object already in storage, leaves storage alone, and
        so does a row refused because another row took its name meanwhile (a unique
        violation, ``_name_taken``), as what is under that name is the other row's.
        """
        path = str(self.fname)
        if not is_plain_path(path):
            raise ValueError(f"The path '{path}' is not plain: it starts or ends with a "
                             f"slash, or a name in it is empty, '.' or '..'.")

        uploads = not _stored(self.fname)
        try:
            super(ChrisFile, self).save(*args, **kwargs)
        except Exception as e:
            if uploads and _stored(self.fname) and not _name_taken(e):
                _discard(self.fname.name, 'a failed save')  # stored, then the row failed
            raise

    def move(self, new_path):
        """
        Move the file to ``new_path``: in one transaction, which must be the outermost
        (``durable``), its row, which takes the path (the unique index settles a race
        with another file for it before storage is touched), then a copy of its object
        there; then the old object deleted, best effort. A failure leaves the file whole
        at one path or the other. A path in use (``path_in_use``) is refused, and the
        file's own path is nothing to do.
        """
        new_path = new_path.strip('/')
        old_path, old_folder = str(self.fname), self.parent_folder

        if new_path == old_path:
            return
        if path_in_use(new_path):
            raise ValueError(f"Path '{new_path}' is in use.")

        storage_manager = connect_storage(settings)
        copied = False
        try:
            with transaction.atomic(durable=True):
                self.parent_folder = _folder_for(new_path, self.owner, old_folder)
                self.fname.name = new_path
                self.save(update_fields=['fname', 'parent_folder'])
                # no row's object: one a failed upload or a deletion left
                storage_manager.delete_obj(new_path)
                storage_manager.copy_obj(old_path, new_path)
                copied = True
        except Exception:
            self.fname.name, self.parent_folder = old_path, old_folder
            if copied:  # the commit failed
                _discard(new_path, 'a failed move')
            raise
        _discard(old_path, 'a move')

    def has_group_permission(self, group, permission=''):
        """
        Custom method to determine whether a group has been granted a permission to
        access the file.
        """
        if not permission:
            qs = FileGroupPermission.objects.filter(group=group, file=self)
        else:
            p = validate_permission(permission)
            qs = FileGroupPermission.objects.filter(group=group, file=self, permission=p)
        return qs.exists()

    def has_user_permission(self, user, permission=''):
        """
        Custom method to determine whether a user has been granted a permission to
        access the file (perhaps through one of its groups).
        """
        grp_qs = user.groups.all()

        if not permission:
            if FileUserPermission.objects.filter(file=self, user=user).exists():
                return True
            qs = FileGroupPermission.objects.filter(file=self, group__in=grp_qs)
        else:
            p = validate_permission(permission)
            if FileUserPermission.objects.filter(file=self, user=user,
                                                 permission=p).exists():
                return True
            qs = FileGroupPermission.objects.filter(file=self, permission=p,
                                                    group__in=grp_qs)
        return qs.exists()

    def get_groups_permissions_queryset(self):
        """
        Custom method to get the queryset of permissions granted to groups to access the
        file.
        """
        return FileGroupPermission.objects.filter(file=self)

    def get_users_permissions_queryset(self):
        """
        Custom method to get the queryset of permissions granted to users to access the
        file.
        """
        return FileUserPermission.objects.filter(file=self)

    def grant_group_permission(self, group, permission):
        """
        Custom method to grant a group a permission to access the file.
        """
        FileGroupPermission.objects.update_or_create(file=self, group=group,
                                                     defaults={'permission': permission})

    def remove_group_permission(self, group, permission):
        """
        Custom method to remove a group's permission to access the file.
        """
        try:
            perm = FileGroupPermission.objects.get(file=self, group=group,
                                                   permission=permission)
        except FileGroupPermission.DoesNotExist:
            pass
        else:
            perm.delete()

    def grant_user_permission(self, user, permission):
        """
        Custom method to grant a user a permission to access the file.
        """
        FileUserPermission.objects.update_or_create(file=self, user=user,
                                                    defaults={'permission': permission})

    def remove_user_permission(self, user, permission):
        """
        Custom method to remove a user's permission to access the file.
        """
        try:
            perm = FileUserPermission.objects.get(file=self, user=user,
                                                  permission=permission)
        except FileUserPermission.DoesNotExist:
            pass
        else:
            perm.delete()

    def grant_public_access(self):
        """
        Custom method to grant public access to the file.
        """
        self.public = True
        self.save()

    def remove_public_access(self):
        """
        Custom method to remove public access to the file.
        """
        self.public = False
        self.save()

    def get_shared_link(self):
        """
        Custom method to get the link file in the SHARED folder pointing to
        this file if it exists.
        """
        str_source_trace_dir = self.fname.name.replace('/', '_')
        fname = 'SHARED/' + str_source_trace_dir + '.chrislink'

        try:
            lf = ChrisLinkFile.objects.get(fname=fname)
        except ChrisLinkFile.DoesNotExist:
            return None
        return lf

    def create_shared_link(self):
        """
        Custom method to create a link file in the SHARED folder pointing to
        this file.
        """
        path = self.fname.name
        str_source_trace_dir = path.replace('/', '_')
        fname = 'SHARED/' + str_source_trace_dir + '.chrislink'

        try:
            lf = ChrisLinkFile.objects.get(fname=fname)
        except ChrisLinkFile.DoesNotExist:
            shared_folder = ChrisFolder.objects.get(path='SHARED')
            lf = ChrisLinkFile(path=path, owner=self.owner, parent_folder=shared_folder)
            lf.save(name=str_source_trace_dir)
        return lf

    def remove_shared_link(self):
        """
        Custom method to remove a link file in the SHARED folder pointing to
        this file if it exists.
        """
        fname = 'SHARED/' + self.fname.name.replace('/', '_') + '.chrislink'
        try:
            lf = ChrisLinkFile.objects.get(fname=fname)
        except ChrisLinkFile.DoesNotExist:
            pass
        else:
            lf.delete()

    def create_public_link(self):
        """
        Custom method to create a public link file in the PUBLIC folder pointing to
        this file.
        """
        path = self.fname.name
        str_source_trace_dir = path.replace('/', '_')
        fname = 'PUBLIC/' + str_source_trace_dir + '.chrislink'

        try:
            lf = ChrisLinkFile.objects.get(fname=fname)
        except ChrisLinkFile.DoesNotExist:
            public_folder = ChrisFolder.objects.get(path='PUBLIC')
            lf = ChrisLinkFile(path=path, owner=self.owner, public=True,
                               parent_folder=public_folder)
            lf.save(name=str_source_trace_dir)

    def remove_public_link(self):
        """
        Custom method to remove a public link file in the PUBLIC folder pointing to
        this file if it exists.
        """
        fname = 'PUBLIC/' + self.fname.name.replace('/', '_') + '.chrislink'
        try:
            lf = ChrisLinkFile.objects.get(fname=fname)
        except ChrisLinkFile.DoesNotExist:
            pass
        else:
            lf.delete()

    @classmethod
    def get_base_queryset(cls):
        """
        Custom method to return a queryset that is only comprised by all files
        registered in storage.
        """
        return cls.objects.all()


class FileGroupPermission(models.Model):
    permission = models.CharField(choices=PERMISSION_CHOICES, default='r', max_length=1)
    file = models.ForeignKey(ChrisFile, on_delete=models.CASCADE)
    group = models.ForeignKey(Group, on_delete=models.CASCADE)

    class Meta:
        unique_together = ('file', 'group',)

    def __str__(self):
        return self.permission


class FileGroupPermissionFilter(FilterSet):
    group_name = django_filters.CharFilter(field_name='group__name', lookup_expr='exact')

    class Meta:
        model = FileGroupPermission
        fields = ['id', 'group_name']


class FileUserPermission(models.Model):
    permission = models.CharField(choices=PERMISSION_CHOICES, default='r', max_length=1)
    file = models.ForeignKey(ChrisFile, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)

    class Meta:
        unique_together = ('file', 'user',)

    def __str__(self):
        return self.permission


class FileUserPermissionFilter(FilterSet):
    username = django_filters.CharFilter(field_name='user__username', lookup_expr='exact')

    class Meta:
        model = FileUserPermission
        fields = ['id', 'username']


class ChrisLinkFile(models.Model):
    creation_date = models.DateTimeField(auto_now_add=True)
    path = models.CharField(max_length=1024, db_index=True)  # pointed path
    fname = models.FileField(max_length=1024, unique=True)
    public = models.BooleanField(blank=True, default=False, db_index=True)
    parent_folder = models.ForeignKey(ChrisFolder, on_delete=models.CASCADE,
                                      related_name='chris_link_files')
    owner = models.ForeignKey('auth.User', on_delete=models.CASCADE)
    shared_groups = models.ManyToManyField(Group, related_name='shared_link_files',
                                           through='LinkFileGroupPermission')
    shared_users = models.ManyToManyField(User, related_name='shared_link_files',
                                          through='LinkFileUserPermission')

    def __str__(self):
        return self.fname.name

    def save(self, *args, **kwargs):
        """
        Overriden to create and save the associated link file when the link is
        saved.
        """
        name = kwargs.pop('name')  # must provide a name for the link
        link_file_path = os.path.join(self.parent_folder.path, f'{name}.chrislink')
        if not is_plain_path(link_file_path):
            raise ValueError(f"The path '{link_file_path}' is not plain.")
        self._write_object(link_file_path)
        self.fname.name = link_file_path
        super(ChrisLinkFile, self).save(*args, **kwargs)

    def _write_object(self, link_file_path):
        """
        Write the link file's object, the path it links, at ``link_file_path``: a new
        object, not one written over, as on a filesystem the object there may be a hard
        link (a copy) that another path shares.
        """
        storage_manager = connect_storage(settings)
        storage_manager.delete_obj(link_file_path)
        storage_manager.upload_obj(link_file_path, f'{self.path}',
                                   content_type='text/plain')

    def move(self, new_path):
        """
        Move the link file to ``new_path``, as ``ChrisFile.move`` moves a file: its row,
        then its object written there, in one transaction (the unique index settles a
        race with another link file), then the old object deleted.
        """
        new_path = new_path.strip('/')
        if not new_path.endswith('.chrislink'):
            raise ValueError("The new path must end with '.chrislink' sufix.")
        old_path, old_folder = str(self.fname), self.parent_folder

        if new_path == old_path:
            return
        if path_in_use(new_path):
            raise ValueError(f"Path '{new_path}' is in use.")

        written = False
        try:
            with transaction.atomic(durable=True):
                self.parent_folder = _folder_for(new_path, self.owner, old_folder)
                self.fname.name = new_path
                super(ChrisLinkFile, self).save(update_fields=['fname', 'parent_folder'])
                self._write_object(new_path)
                written = True
        except Exception:
            self.fname.name, self.parent_folder = old_path, old_folder
            if written:  # the commit failed
                _discard(new_path, 'a failed move')
            raise
        _discard(old_path, 'a move')

    def has_group_permission(self, group, permission=''):
        """
        Custom method to determine whether a group has been granted a permission to
        access the link file.
        """
        if not permission:
            qs = LinkFileGroupPermission.objects.filter(group=group, link_file=self)
        else:
            p = validate_permission(permission)
            qs = LinkFileGroupPermission.objects.filter(group=group, link_file=self,
                                                        permission=p)
        return qs.exists()

    def has_user_permission(self, user, permission=''):
        """
        Custom method to determine whether a user has been granted a permission to
        access the link file (perhaps through one of its groups).
        """
        grp_qs = user.groups.all()

        if not permission:
            if LinkFileUserPermission.objects.filter(link_file=self, user=user).exists():
                return True
            qs = LinkFileGroupPermission.objects.filter(link_file=self, group__in=grp_qs)
        else:
            p = validate_permission(permission)
            if LinkFileUserPermission.objects.filter(link_file=self, user=user,
                                                     permission=p).exists():
                return True
            qs = LinkFileGroupPermission.objects.filter(link_file=self, permission=p,
                                                        group__in=grp_qs)
        return qs.exists()

    def get_groups_permissions_queryset(self):
        """
        Custom method to get the queryset of permissions granted to groups to access the
        link file.
        """
        return LinkFileGroupPermission.objects.filter(link_file=self)

    def get_users_permissions_queryset(self):
        """
        Custom method to get the queryset of permissions granted to users to access the
        link file.
        """
        return LinkFileUserPermission.objects.filter(link_file=self)

    def grant_group_permission(self, group, permission):
        """
        Custom method to grant a group a permission to access the link file.
        """
        LinkFileGroupPermission.objects.update_or_create(link_file=self, group=group,
                                                         defaults={'permission': permission})

    def remove_group_permission(self, group, permission):
        """
        Custom method to remove a group's permission to access the link file.
        """
        try:
            perm = LinkFileGroupPermission.objects.get(link_file=self, group=group,
                                                       permission=permission)
        except LinkFileGroupPermission.DoesNotExist:
            pass
        else:
            perm.delete()

    def grant_user_permission(self, user, permission):
        """
        Custom method to grant a user a permission to access the link file.
        """
        LinkFileUserPermission.objects.update_or_create(link_file=self, user=user,
                                                        defaults={'permission': permission})

    def remove_user_permission(self, user, permission):
        """
        Custom method to remove a user's permission to access the link file.
        """
        try:
            perm = LinkFileUserPermission.objects.get(link_file=self, user=user,
                                                      permission=permission)
        except LinkFileUserPermission.DoesNotExist:
            pass
        else:
            perm.delete()

    def grant_public_access(self):
        """
        Custom method to grant public access to the link file.
        """
        self.public = True
        path = self.fname.name
        link_name = os.path.basename(path).rsplit('.chrislink', 1)[0]
        self.save(name=link_name)

    def remove_public_access(self):
        """
        Custom method to remove public access to the link file.
        """
        self.public = False
        path = self.fname.name
        link_name = os.path.basename(path).rsplit('.chrislink', 1)[0]
        self.save(name=link_name)

    def get_shared_link(self):
        """
        Custom method to get the link file in the SHARED folder pointing to
        this file if it exists.
        """
        str_source_trace_dir = self.fname.name.replace('/', '_')
        fname = 'SHARED/' + str_source_trace_dir + '.chrislink'

        try:
            lf = ChrisLinkFile.objects.get(fname=fname)
        except ChrisLinkFile.DoesNotExist:
            return None
        return lf

    def create_shared_link(self):
        """
        Custom method to create a link file in the SHARED folder pointing to
        this file.
        """
        path = self.fname.name
        str_source_trace_dir = path.replace('/', '_')
        fname = 'SHARED/' + str_source_trace_dir + '.chrislink'

        try:
            lf = ChrisLinkFile.objects.get(fname=fname)
        except ChrisLinkFile.DoesNotExist:
            shared_folder = ChrisFolder.objects.get(path='SHARED')
            lf = ChrisLinkFile(path=path, owner=self.owner, parent_folder=shared_folder)
            lf.save(name=str_source_trace_dir)
        return lf

    def remove_shared_link(self):
        """
        Custom method to remove a link file in the SHARED folder pointing to
        this link file if it exists.
        """
        fname = 'SHARED/' + self.fname.name.replace('/', '_') + '.chrislink'
        try:
            lf = ChrisLinkFile.objects.get(fname=fname)
        except ChrisLinkFile.DoesNotExist:
            pass
        else:
            lf.delete()

    def create_public_link(self):
        """
        Custom method to create a public link file in the PUBLIC folder pointing to
        this link file.
        """
        path = self.fname.name
        str_source_trace_dir = path.replace('/', '_')
        fname = 'PUBLIC/' + str_source_trace_dir + '.chrislink'

        try:
            lf = ChrisLinkFile.objects.get(fname=fname)
        except ChrisLinkFile.DoesNotExist:
            public_folder = ChrisFolder.objects.get(path='PUBLIC')
            lf = ChrisLinkFile(path=path, owner=self.owner, public=True,
                               parent_folder=public_folder)
            lf.save(name=str_source_trace_dir)

    def remove_public_link(self):
        """
        Custom method to remove a public link file in the PUBLIC folder pointing to
        this link file if it exists.
        """
        fname = 'PUBLIC/' + self.fname.name.replace('/', '_') + '.chrislink'
        try:
            lf = ChrisLinkFile.objects.get(fname=fname)
        except ChrisLinkFile.DoesNotExist:
            pass
        else:
            lf.delete()


def auto_delete_file_from_storage(sender, instance, origin=None, **kwargs):
    """
    Delete the object of a deleted file or link file, unless the deletion started at a
    folder above it (``_under_deleted_folder``). A storage error is logged
    (``_storage_errors_logged``). ``Core.ready`` connects it to the deletions of
    ChrisFile, ChrisLinkFile and each proxy of theirs, as a signal reaches only the
    receivers of its exact sender; ChrisFolder has no proxy, so its receiver is
    connected where it is defined.
    """
    path = instance.fname.name
    if _under_deleted_folder(path, origin):
        return
    with _storage_errors_logged(f'deleting {path}'):
        connect_storage(settings).delete_obj(path)


class LinkFileGroupPermission(models.Model):
    permission = models.CharField(choices=PERMISSION_CHOICES, default='r', max_length=1)
    link_file = models.ForeignKey(ChrisLinkFile, on_delete=models.CASCADE)
    group = models.ForeignKey(Group, on_delete=models.CASCADE)

    class Meta:
        unique_together = ('link_file', 'group',)

    def __str__(self):
        return self.permission


class LinkFileGroupPermissionFilter(FilterSet):
    group_name = django_filters.CharFilter(field_name='group__name', lookup_expr='exact')

    class Meta:
        model = LinkFileGroupPermission
        fields = ['id', 'group_name']


class LinkFileUserPermission(models.Model):
    permission = models.CharField(choices=PERMISSION_CHOICES, default='r', max_length=1)
    link_file = models.ForeignKey(ChrisLinkFile, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)

    class Meta:
        unique_together = ('link_file', 'user',)

    def __str__(self):
        return self.permission


class LinkFileUserPermissionFilter(FilterSet):
    username = django_filters.CharFilter(field_name='user__username', lookup_expr='exact')

    class Meta:
        model = LinkFileUserPermission
        fields = ['id', 'username']


class FileDownloadToken(models.Model):
    creation_date = models.DateTimeField(auto_now_add=True)
    owner = models.ForeignKey('auth.User', on_delete=models.CASCADE)
    token = models.CharField(max_length=300, db_index=True)

    class Meta:
        ordering = ('owner', 'creation_date')

    def __str__(self):
        return str(self.token)


class FileDownloadTokenFilter(FilterSet):

    class Meta:
        model = FileDownloadToken
        fields = ['id']
