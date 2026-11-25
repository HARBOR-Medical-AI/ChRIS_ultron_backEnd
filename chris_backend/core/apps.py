
from django.apps import AppConfig
from django.db.models.signals import post_delete, post_migrate


def setup_chris(sender, **kwargs):
    from django.contrib.auth.models import User, Group
    from django.conf import settings
    from .models import ChrisInstance, ChrisFolder

    ChrisInstance.load()  # create the ChRIS instance singleton

    # create superuser chris
    try:
        chris_user = User.objects.get(username='chris')
        chris_user.set_password(settings.CHRIS_SUPERUSER_PASSWORD)
        chris_user.save()
    except User.DoesNotExist:
        chris_user = User.objects.create_superuser('chris', 'dev@babymri.org',
                                                   settings.CHRIS_SUPERUSER_PASSWORD)
    # create required groups
    (all_grp, _) = Group.objects.get_or_create(name='all_users')
    (pacs_grp, _) = Group.objects.get_or_create(name='pacs_users')

    # create top level folders and their permissions
    (folder, _) = ChrisFolder.objects.get_or_create(path='', owner=chris_user,
                                                    public=True)

    (folder, _) = ChrisFolder.objects.get_or_create(path='home', owner=chris_user)
    if not folder.has_group_permission(all_grp):
        folder.grant_group_permission(all_grp, 'r')

    (folder, _) = ChrisFolder.objects.get_or_create(path='SHARED', owner=chris_user)
    if not folder.has_group_permission(all_grp):
        folder.grant_group_permission(all_grp, 'r')

    ChrisFolder.objects.get_or_create(path='PUBLIC', owner=chris_user, public=True)
    ChrisFolder.objects.get_or_create(path='PIPELINES', owner=chris_user, public=True)

    (folder, _) = ChrisFolder.objects.get_or_create(path='SERVICES', owner=chris_user)
    if not folder.has_group_permission(all_grp):
        folder.grant_group_permission(all_grp, 'r')

    (folder, _) = ChrisFolder.objects.get_or_create(path='SERVICES/PACS',
                                                    owner=chris_user)
    if not folder.has_group_permission(pacs_grp):
        folder.grant_group_permission(pacs_grp, 'r')


class Core(AppConfig):
    name = 'core'

    def ready(self):
        from django.apps import apps
        from .middleware import backport_thread_sensitive_error_responses
        from .models import ChrisFile, ChrisLinkFile, auto_delete_file_from_storage

        post_migrate.connect(setup_chris, sender=self)

        # a signal reaches only the receivers of its exact sender: each proxy of the file
        # models (UserFile, PACSFile...) needs the receiver as much as they do
        for model in apps.get_models():
            if issubclass(model, (ChrisFile, ChrisLinkFile)):
                post_delete.connect(auto_delete_file_from_storage, sender=model)

        # before any ASGI handler builds its middleware chain
        backport_thread_sensitive_error_responses()
