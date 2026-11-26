
import os

from django.contrib.auth.models import User, Group
from django.db.utils import IntegrityError
from rest_framework import serializers
from rest_framework.reverse import reverse
from drf_spectacular.utils import OpenApiTypes, extend_schema_field

from collectionjson.fields import ItemLinkField
from core.models import (ChrisFolder, ChrisFile, ChrisLinkFile, FolderGroupPermission,
                         FolderUserPermission, FileGroupPermission, FileUserPermission,
                         LinkFileGroupPermission, LinkFileUserPermission)
from core.serializers import (ChrisFileSerializer, public_link_follows,
                              validate_path_not_in_use, validate_plain_path)


class FileBrowserFolderSerializer(serializers.HyperlinkedModelSerializer):
    path = serializers.CharField(max_length=1024, required=False)
    owner_username = serializers.ReadOnlyField(source='owner.username')
    parent = serializers.HyperlinkedRelatedField(view_name='chrisfolder-detail',
                                                 read_only=True)
    children = serializers.HyperlinkedIdentityField(
        view_name='chrisfolder-child-list')
    files = serializers.HyperlinkedIdentityField(view_name='chrisfolder-file-list')
    link_files = serializers.HyperlinkedIdentityField(
        view_name='chrisfolder-linkfile-list')
    group_permissions = serializers.HyperlinkedIdentityField(
        view_name='foldergrouppermission-list')
    user_permissions = serializers.HyperlinkedIdentityField(
        view_name='folderuserpermission-list')
    owner = serializers.HyperlinkedRelatedField(view_name='user-detail', read_only=True)

    class Meta:
        model = ChrisFolder
        fields = ('url', 'id', 'creation_date', 'path', 'public', 'owner_username',
                  'deletion_status', 'deletion_requested_at', 'deletion_error',
                  'parent', 'children', 'files', 'link_files', 'group_permissions',
                  'user_permissions', 'owner')
        # the deletion's state is the deletion tasks' to write
        read_only_fields = ('deletion_status', 'deletion_requested_at', 'deletion_error')

    def create(self, validated_data):
        """
        Overriden to set the parent folder. It also creates non-existent ancestors and
        sets their permissions to be the same as the first existing ancestor.
        """
        path = validated_data['path']
        parent_path = os.path.dirname(path)
        owner = validated_data['owner']

        parent = ancestor = ChrisFolder.get_first_existing_folder_ancestor(path)
        if ancestor.path != parent_path:
            parent = ChrisFolder.objects.create(path=parent_path, owner=owner)

        validated_data['parent'] = parent
        folder = super(FileBrowserFolderSerializer, self).create(validated_data)

        if ancestor.path == parent_path:
            top_created_folder = folder
        else:
            path_parts = path.split('/')
            ancestor_path_parts = ancestor.path.split('/')
            next_part = path_parts[len(ancestor_path_parts)]
            top_created_folder = ChrisFolder.objects.get(
                path=ancestor.path + '/' + next_part)

        if ancestor.public:
            top_created_folder.grant_public_access()
            folder.public = True  # update object before returning it

        for perm in ancestor.get_groups_permissions_queryset():
            top_created_folder.grant_group_permission(perm.group, perm.permission)

        for perm in ancestor.get_users_permissions_queryset():
            top_created_folder.grant_user_permission(perm.user, perm.permission)

        if owner != ancestor.owner:
            top_created_folder.grant_user_permission(ancestor.owner, 'w')
        return folder

    def update(self, instance, validated_data):
        """
        Overriden to grant or remove public access to the folder and all its
        descendant folders, link files and files depending on the new public status of
        the folder. Also move the folder's tree to a new path and recreate the public
        link to the folder if required.
        """
        public = instance.public

        if public and 'public' in validated_data and not validated_data['public']:
            instance.remove_public_link()
            instance.remove_public_access()

        new_path = validated_data.get('path')

        if new_path:
            stays_public = public and validated_data.get('public', True)
            # folder will be stored at: SWIFT_CONTAINER_NAME/<new_path>
            # where <new_path> must start with home/
            with public_link_follows(instance, stays_public):
                instance.move(new_path)

        if not public and 'public' in validated_data and validated_data['public']:
            instance.grant_public_access()
            instance.create_public_link()
        return instance

    def validate_path(self, path):
        """
        Overriden to check whether the provided path does not contain commas and
        is under a home/'s subdirectory for which the user has write permission.
        Also to check whether the folder already exists.
        """
        if ',' in path:
            raise serializers.ValidationError([f"Invalid path. Cannot contain commas."])

        # remove leading and trailing slashes
        path = path.strip().strip('/')
        validate_plain_path(path)

        if not path.startswith('home/'):
            raise serializers.ValidationError(["Invalid path. Path must start with "
                                               "'home/'."])

        ancestor = ChrisFolder.get_first_existing_folder_ancestor(path)

        if ancestor.path == path:
            raise serializers.ValidationError([f"Folder with path '{path}' already "
                                               f"exists."])
        user = self.context['request'].user

        if not (ancestor.owner == user or ancestor.public or
                ancestor.has_user_permission(user, 'w')):
            raise serializers.ValidationError([f"Invalid path. User do not have write "
                                               f"permission under the folder "
                                               f"'{ancestor.path}'."])
        validate_path_not_in_use(path)  # a file's or a link file's
        return path

    def validate_public(self, public):
        """
        Overriden to check that only the owner or superuser chris can change a folder's
        public status.
        """
        if self.instance:  # on update
            user = self.context['request'].user

            if not (self.instance.owner == user or user.username == 'chris'):
                raise serializers.ValidationError(
                    ["Public status of a folder can only be changed by its owner or"
                     "superuser 'chris'."])
        return public

    def validate(self, data):
        """
        Overriden to validate that required fields are in data when creating or
        updating a folder. Also to verify that the user's home or feeds folder is not
        being moved.
        """
        if self.instance:  # on update
            if 'public' not in data and 'path' not in data:
                raise serializers.ValidationError(
                        {'non_field_errors': ["At least one of the fields 'public' "
                                              "or 'path' must be provided."]})

            username = self.context['request'].user.username

            if 'path' in data and username != 'chris':
                inst_path_parts = self.instance.path.split('/')

                if len(inst_path_parts) > 1 and inst_path_parts[0] == 'home' and (
                        len(inst_path_parts) == 2 or (len(inst_path_parts) == 3 and
                                                      inst_path_parts[2] == 'feeds')):
                    raise serializers.ValidationError(
                        {'non_field_errors':
                             [f"Moving folder '{self.instance.path}' is not allowed."]})
        else:
            if 'path' not in data: # on create
                raise serializers.ValidationError({'path': ['This field is required.']})

            data.pop('public', None)  # can only be set to public on update
        return data


class FileBrowserFolderGroupPermissionSerializer(serializers.HyperlinkedModelSerializer):
    grp_name = serializers.CharField(write_only=True, required=False)
    folder_id = serializers.ReadOnlyField(source='folder.id')
    folder_path = serializers.ReadOnlyField(source='folder.path')
    group_id = serializers.ReadOnlyField(source='group.id')
    group_name = serializers.ReadOnlyField(source='group.name')
    folder = serializers.HyperlinkedRelatedField(view_name='chrisfolder-detail',
                                                 read_only=True)
    group = serializers.HyperlinkedRelatedField(view_name='group-detail', read_only=True)

    class Meta:
        model = FolderGroupPermission
        fields = ('url', 'id', 'permission', 'folder_id', 'folder_path', 'group_id',
                  'group_name', 'folder', 'group', 'grp_name')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance is not None: # on update
            self.fields['grp_name'].read_only = True  # set to read-only before validation

    def create(self, validated_data):
        """
        Overriden to handle the error when trying to create a permission for a group that
        already has a permission granted. Also a link file in the SHARED folder
        pointing to the folder is created if it doesn't exist.
        """
        folder = validated_data['folder']
        group = validated_data['group']

        try:
            perm = super(FileBrowserFolderGroupPermissionSerializer,
                         self).create(validated_data)
        except IntegrityError:
            raise serializers.ValidationError(
                {'non_field_errors':
                     [f"Group '{group.name}' already has a permission to access folder "
                      f"with id {folder.id}"]})

        lf = folder.create_shared_link()
        lf.grant_group_permission(group, 'r')
        return perm

    def validate_grp_name(self, grp_name):
        """
        Overriden to check whether the provided group name exists in the DB.
        """
        try:
            group = Group.objects.get(name=grp_name)
        except Group.DoesNotExist:
            raise serializers.ValidationError([f"Couldn't find any group with name "
                                               f"'{grp_name}'."])
        return group

    def validate(self, data):
        """
        Overriden to validate that required fields are in data when creating or
        updating a permission.
        """
        if self.instance:  # on update
            if 'permission' not in data:
                raise serializers.ValidationError({'permission':
                                                       ['This field is required.']})
        else:
            if 'grp_name' not in data: # on create
                raise serializers.ValidationError({'grp_name':
                                                       ['This field is required.']})
        return data


class FileBrowserFolderUserPermissionSerializer(serializers.HyperlinkedModelSerializer):
    username = serializers.CharField(write_only=True, min_length=4, max_length=32,
                                     required=False)
    folder_id = serializers.ReadOnlyField(source='folder.id')
    folder_path = serializers.ReadOnlyField(source='folder.path')
    user_id = serializers.ReadOnlyField(source='user.id')
    user_username = serializers.ReadOnlyField(source='user.username')
    folder = serializers.HyperlinkedRelatedField(view_name='chrisfolder-detail',
                                                 read_only=True)
    user = serializers.HyperlinkedRelatedField(view_name='user-detail', read_only=True)

    class Meta:
        model = FolderUserPermission
        fields = ('url', 'id', 'permission', 'folder_id', 'folder_path', 'user_id',
                  'user_username', 'folder', 'user', 'username')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance is not None: # on update
            self.fields['username'].read_only = True  # set to read-only before validation

    def create(self, validated_data):
        """
        Overriden to handle the error when trying to create a permission for a user that
        already has a permission granted. Also a link file in the SHARED folder
        pointing to the folder is created if it doesn't exist.
        """
        folder = validated_data['folder']
        user = validated_data['user']

        try:
            perm = super(FileBrowserFolderUserPermissionSerializer,
                         self).create(validated_data)
        except IntegrityError:
            raise serializers.ValidationError(
                {'non_field_errors':
                     [f"User '{user.username}' already has a permission to access "
                      f"folder with id {folder.id}"]})

        lf = folder.create_shared_link()
        lf.grant_user_permission(user, 'r')
        return perm

    def validate_username(self, username):
        """
        Overriden to check whether the provided username exists in the DB.
        """
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise serializers.ValidationError([f"Couldn't find any user with username "
                                               f"'{username}'."])
        return user

    def validate(self, data):
        """
        Overriden to validate that required fields are in data when creating or
        updating a permission.
        """
        if self.instance:  # on update
            if 'permission' not in data:
                raise serializers.ValidationError({'permission':
                                                       ['This field is required.']})
        else:
            if 'username' not in data: # on create
                raise serializers.ValidationError({'username':
                                                       ['This field is required.']})
        return data


class FileBrowserFileSerializer(ChrisFileSerializer):
    new_file_path = serializers.CharField(max_length=1024, write_only=True,
                                          required=False)
    group_permissions = serializers.HyperlinkedIdentityField(
        view_name='filegrouppermission-list')
    user_permissions = serializers.HyperlinkedIdentityField(
        view_name='fileuserpermission-list')

    class Meta:
        model = ChrisFile
        fields = ('url', 'id', 'creation_date', 'fname', 'fsize', 'public',
                  'new_file_path', 'owner_username', 'file_resource', 'parent_folder',
                  'group_permissions', 'user_permissions', 'owner')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance is not None: # on update
            self.fields['fname'].read_only = True  # set to read-only before validation

    def update(self, instance, validated_data):
        """
        Overriden to grant or remove public access to the file and/or move it to a new
        path.
        """
        public = instance.public

        if public and 'public' in validated_data and not validated_data['public']:
            instance.remove_public_link()
            instance.remove_public_access()

        new_file_path = validated_data.pop('new_file_path', None)

        if new_file_path:
            stays_public = public and validated_data.get('public', True)
            # user file will be stored at: SWIFT_CONTAINER_NAME/<new_file_path>
            # where <new_file_path> must start with home/
            with public_link_follows(instance, stays_public):
                instance.move(new_file_path)

        if not public and 'public' in validated_data and validated_data['public']:
            instance.grant_public_access()
            instance.create_public_link()
        return instance

    def validate_new_file_path(self, new_file_path):
        """
        Overriden to check whether the provided path is under a home/'s subdirectory
        for which the user has write permission.
        """
        # remove leading and trailing slashes
        new_file_path = new_file_path.strip().strip('/')
        validate_plain_path(new_file_path)

        if new_file_path.endswith('.chrislink'):
            raise serializers.ValidationError(["Invalid path. This is not a ChRIS link "
                                               "file."])
        if not new_file_path.startswith('home/'):
            raise serializers.ValidationError(["Invalid path. Path must start with "
                                               "'home/'."])
        user = self.context['request'].user
        folder_path = os.path.dirname(new_file_path)

        while True:
            try:
                folder = ChrisFolder.objects.get(path=folder_path)
            except ChrisFolder.DoesNotExist:
                folder_path = os.path.dirname(folder_path)
            else:
                break

        if not (folder.owner == user or folder.public or
                folder.has_user_permission(user, 'w')):
            raise serializers.ValidationError([f"Invalid path. User do not have write "
                                               f"permission under the folder "
                                               f"'{folder_path}'."])
        validate_path_not_in_use(new_file_path, self.instance)
        return new_file_path

    def validate_public(self, public):
        """
        Overriden to check that only the owner or superuser chris can change a file's
        public status.
        """
        if self.instance:  # on update
            user = self.context['request'].user

            if not (self.instance.owner == user or user.username == 'chris'):
                raise serializers.ValidationError(
                    ["Public status of a file can only be changed by its owner or"
                     "superuser 'chris'."])
        return public

    def validate(self, data):
        """
        Overriden to validate that at least one of two fields are in data when
        updating a file.
        """
        if self.instance:  # on update
            if 'public' not in data and 'new_file_path' not in data:
                raise serializers.ValidationError(
                    {'non_field_errors': ["At least one of the fields 'public' "
                                          "or 'new_file_path' must be provided."]})
        return data


class FileBrowserFileGroupPermissionSerializer(serializers.HyperlinkedModelSerializer):
    grp_name = serializers.CharField(write_only=True, required=False)
    file_id = serializers.ReadOnlyField(source='file.id')
    file_fname = serializers.SerializerMethodField()
    group_id = serializers.ReadOnlyField(source='group.id')
    group_name = serializers.ReadOnlyField(source='group.name')
    file = serializers.HyperlinkedRelatedField(view_name='chrisfile-detail',
                                               read_only=True)
    group = serializers.HyperlinkedRelatedField(view_name='group-detail', read_only=True)

    class Meta:
        model = FileGroupPermission
        fields = ('url', 'id', 'permission', 'file_id', 'file_fname', 'group_id',
                  'group_name', 'file', 'group', 'grp_name')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance is not None: # on update
            self.fields['grp_name'].read_only = True  # set to read-only before validation

    def get_file_fname(self, obj) -> str:
        return obj.file.fname.name

    def create(self, validated_data):
        """
        Overriden to handle the error when trying to create a permission for a group that
        already has a permission granted. Also a link file in the SHARED folder
        pointing to the file is created if it doesn't exist.
        """
        f = validated_data['file']
        group = validated_data['group']

        try:
            perm = super(FileBrowserFileGroupPermissionSerializer,
                         self).create(validated_data)
        except IntegrityError:
            raise serializers.ValidationError(
                {'non_field_errors':
                     [f"Group '{group.name}' already has a permission to access file "
                      f"with id {f.id}"]})

        lf = f.create_shared_link()
        lf.grant_group_permission(group, 'r')
        return perm

    def validate_grp_name(self, grp_name):
        """
        Overriden to check whether the provided group name exists in the DB.
        """
        try:
            group = Group.objects.get(name=grp_name)
        except Group.DoesNotExist:
            raise serializers.ValidationError([f"Couldn't find any group with name "
                                               f"'{grp_name}'."])
        return group

    def validate(self, data):
        """
        Overriden to validate that required fields are in data when creating or
        updating a permission.
        """
        if self.instance:  # on update
            if 'permission' not in data:
                raise serializers.ValidationError({'permission':
                                                       ['This field is required.']})
        else:
            if 'grp_name' not in data: # on create
                raise serializers.ValidationError({'grp_name':
                                                       ['This field is required.']})
        return data


class FileBrowserFileUserPermissionSerializer(serializers.HyperlinkedModelSerializer):
    username = serializers.CharField(write_only=True, min_length=4, max_length=32,
                                     required=False)
    file_id = serializers.ReadOnlyField(source='file.id')
    file_fname = serializers.SerializerMethodField()
    user_id = serializers.ReadOnlyField(source='user.id')
    user_username = serializers.ReadOnlyField(source='user.username')
    file = serializers.HyperlinkedRelatedField(view_name='chrisfile-detail',
                                               read_only=True)
    user = serializers.HyperlinkedRelatedField(view_name='user-detail', read_only=True)

    class Meta:
        model = FileUserPermission
        fields = ('url', 'id', 'permission', 'file_id', 'file_fname', 'user_id',
                  'user_username', 'file', 'user', 'username')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance is not None: # on update
            self.fields['username'].read_only = True  # set to read-only before validation

    def get_file_fname(self, obj) -> str:
        return obj.file.fname.name

    def create(self, validated_data):
        """
        Overriden to handle the error when trying to create a permission for a user that
        already has a permission granted. Also a link file in the SHARED folder
        pointing to the file is created if it doesn't exist.
        """
        f = validated_data['file']
        user = validated_data['user']

        try:
            perm = super(FileBrowserFileUserPermissionSerializer,
                         self).create(validated_data)
        except IntegrityError:
            raise serializers.ValidationError(
                {'non_field_errors':
                     [f"User '{user.username}' already has a permission to access "
                      f"file with id {f.id}"]})

        lf = f.create_shared_link()
        lf.grant_user_permission(user, 'r')
        return perm

    def validate_username(self, username):
        """
        Overriden to check whether the provided username exists in the DB.
        """
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise serializers.ValidationError([f"Couldn't find any user with username "
                                               f"'{username}'."])
        return user

    def validate(self, data):
        """
        Overriden to validate that required fields are in data when creating or
        updating a permission.
        """
        if self.instance:  # on update
            if 'permission' not in data:
                raise serializers.ValidationError({'permission':
                                                       ['This field is required.']})
        else:
            if 'username' not in data: # on create
                raise serializers.ValidationError({'username':
                                                       ['This field is required.']})
        return data


class FileBrowserLinkFileSerializer(ChrisFileSerializer):
    new_link_file_path = serializers.CharField(max_length=1024, write_only=True,
                                               required=False)
    path = serializers.CharField(max_length=1024, required=False)
    linked_folder = ItemLinkField('get_linked_folder_link')
    linked_file = ItemLinkField('get_linked_file_link')
    group_permissions = serializers.HyperlinkedIdentityField(
        view_name='linkfilegrouppermission-list')
    user_permissions = serializers.HyperlinkedIdentityField(
        view_name='linkfileuserpermission-list')

    class Meta:
        model = ChrisLinkFile
        fields = ('url', 'id', 'creation_date', 'path', 'fname', 'fsize', 'public',
                  'new_link_file_path', 'owner_username', 'file_resource',
                  'linked_folder', 'linked_file', 'parent_folder', 'group_permissions',
                  'user_permissions', 'owner')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance is not None: # on update
            self.fields['fname'].read_only = True  # set to read-only before validation
            self.fields['path'].read_only = True

    def update(self, instance, validated_data):
        """
        Overriden to grant or remove public access to the file and/or move it to a new
        path.
        """
        public = instance.public

        if public and 'public' in validated_data and not validated_data['public']:
            instance.remove_public_link()
            instance.remove_public_access()

        new_link_file_path = validated_data.pop('new_link_file_path', None)

        if new_link_file_path:
            stays_public = public and validated_data.get('public', True)
            # link file will be stored at: SWIFT_CONTAINER_NAME/<new_link_file_path>
            # where <new_link_file_path> must start with home/
            with public_link_follows(instance, stays_public):
                instance.move(new_link_file_path)

        if not public and 'public' in validated_data and validated_data['public']:
            instance.grant_public_access()
            instance.create_public_link()
        return instance

    @extend_schema_field(OpenApiTypes.URI)
    def get_linked_folder_link(self, obj):
        """
        Custom method to get the hyperlink to the linked folder if the ChRIS link
        points to a folder.
        """
        try:
            linked_folder = ChrisFolder.objects.get(path=obj.path)
        except ChrisFolder.DoesNotExist:
            return None
        request = self.context['request']
        return reverse('chrisfolder-detail', request=request,
                       kwargs={'pk': linked_folder.pk})

    @extend_schema_field(OpenApiTypes.URI)
    def get_linked_file_link(self, obj):
        """
        Custom method to get the hyperlink to the linked file if the ChRIS link
        points to a file.
        """
        try:
            ChrisFolder.objects.get(path=obj.path)
        except ChrisFolder.DoesNotExist:
            parent_folder_path = os.path.dirname(obj.path)

            try:
                parent_folder = ChrisFolder.objects.get(path=parent_folder_path)
            except ChrisFolder.DoesNotExist: # no parent folder then no file
                return None

            try:
                linked_file = parent_folder.chris_files.get(fname=obj.path)
            except ChrisFile.DoesNotExist:  # file not found
                return None

            request = self.context['request']
            return reverse('chrisfile-detail', request=request,
                           kwargs={'pk': linked_file.pk})

    def validate_new_link_file_path(self, new_link_file_path):
        """
        Overriden to check whether the provided path is under a home/'s subdirectory
        for which the user has write permission.
        """
        # remove leading and trailing slashes
        new_link_file_path = new_link_file_path.strip().strip('/')
        validate_plain_path(new_link_file_path)

        if not new_link_file_path.endswith('.chrislink'):
            raise serializers.ValidationError(["Invalid path. The new path must end with"
                                               " '.chrislink' sufix."])
        if not new_link_file_path.startswith('home/'):
            raise serializers.ValidationError(["Invalid path. Path must start with "
                                               "'home/'."])
        user = self.context['request'].user
        folder_path = os.path.dirname(new_link_file_path)

        while True:
            try:
                folder = ChrisFolder.objects.get(path=folder_path)
            except ChrisFolder.DoesNotExist:
                folder_path = os.path.dirname(folder_path)
            else:
                break

        if not (folder.owner == user or folder.public or
                folder.has_user_permission(user, 'w')):
            raise serializers.ValidationError([f"Invalid path. User do not have write "
                                               f"permission under the folder "
                                               f"'{folder_path}'."])
        validate_path_not_in_use(new_link_file_path, self.instance)
        return new_link_file_path

    def validate_public(self, public):
        """
        Overriden to check that only the owner or superuser chris can change a link
        file's public status.
        """
        if self.instance:  # on update
            user = self.context['request'].user

            if not (self.instance.owner == user or user.username == 'chris'):
                raise serializers.ValidationError(
                    ["Public status of a link file can only be changed by its owner or"
                     "superuser 'chris'."])
        return public

    def validate(self, data):
        """
        Overriden to validate that at least one of two fields are in data when
        updating a link file. Also to verify that the user's home's system-predefined
        link files are not being moved.
        """
        if self.instance:  # on update
            if 'public' not in data and 'new_link_file_path' not in data:
                raise serializers.ValidationError(
                    {'non_field_errors': ["At least one of the fields 'public' "
                                          "or 'new_link_file_path' must be provided."]})

            username = self.context['request'].user.username

            if 'new_link_file_path' in data and username != 'chris':
                fname = self.instance.fname.name
                fname_parts = fname.split('/')

                if len(fname_parts) == 3 and fname_parts[0] == 'home' and (
                        fname_parts[2] in ('public.chrislink', 'shared.chrislink')):

                    raise serializers.ValidationError(
                        {'non_field_errors':
                             [f"Moving link file '{fname}' is not allowed."]})
        else: # on create
            if 'path' not in data:
                raise serializers.ValidationError({'path': ['This field is required.']})
        return data


class FileBrowserLinkFileGroupPermissionSerializer(serializers.HyperlinkedModelSerializer):
    grp_name = serializers.CharField(write_only=True, required=False)
    link_file_id = serializers.ReadOnlyField(source='link_file.id')
    link_file_fname = serializers.SerializerMethodField()
    group_id = serializers.ReadOnlyField(source='group.id')
    group_name = serializers.ReadOnlyField(source='group.name')
    link_file = serializers.HyperlinkedRelatedField(view_name='chrislinkfile-detail',
                                                    read_only=True)
    group = serializers.HyperlinkedRelatedField(view_name='group-detail', read_only=True)

    class Meta:
        model = LinkFileGroupPermission
        fields = ('url', 'id', 'permission', 'link_file_id', 'link_file_fname',
                  'group_id', 'group_name', 'link_file', 'group', 'grp_name')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance is not None: # on update
            self.fields['grp_name'].read_only = True  # set to read-only before validation

    def get_link_file_fname(self, obj) -> str:
        return obj.link_file.fname.name

    def create(self, validated_data):
        """
        Overriden to handle the error when trying to create a permission for a group that
        already has a permission granted. Also a link file in the SHARED folder
        pointing to this link file is created if it doesn't exist.
        """
        lf = validated_data['link_file']
        group = validated_data['group']

        try:
            perm = super(FileBrowserLinkFileGroupPermissionSerializer,
                         self).create(validated_data)
        except IntegrityError:
            raise serializers.ValidationError(
                {'non_field_errors':
                     [f"Group '{group.name}' already has a permission to access link "
                      f"file with id {lf.id}"]})

        shared_lf = lf.create_shared_link()
        shared_lf.grant_group_permission(group, 'r')
        return perm

    def validate_grp_name(self, grp_name):
        """
        Overriden to check whether the provided group name exists in the DB.
        """
        try:
            group = Group.objects.get(name=grp_name)
        except Group.DoesNotExist:
            raise serializers.ValidationError([f"Couldn't find any group with name "
                                               f"'{grp_name}'."])
        return group

    def validate(self, data):
        """
        Overriden to validate that required fields are in data when creating or
        updating a permission.
        """
        if self.instance:  # on update
            if 'permission' not in data:
                raise serializers.ValidationError({'permission':
                                                       ['This field is required.']})
        else:
            if 'grp_name' not in data: # on create
                raise serializers.ValidationError({'grp_name':
                                                       ['This field is required.']})
        return data


class FileBrowserLinkFileUserPermissionSerializer(serializers.HyperlinkedModelSerializer):
    username = serializers.CharField(write_only=True, min_length=4, max_length=32,
                                     required=False)
    link_file_id = serializers.ReadOnlyField(source='link_file.id')
    link_file_fname = serializers.SerializerMethodField()
    user_id = serializers.ReadOnlyField(source='user.id')
    user_username = serializers.ReadOnlyField(source='user.username')
    link_file = serializers.HyperlinkedRelatedField(view_name='chrislinkfile-detail',
                                                    read_only=True)
    user = serializers.HyperlinkedRelatedField(view_name='user-detail', read_only=True)

    class Meta:
        model = LinkFileUserPermission
        fields = ('url', 'id', 'permission', 'link_file_id', 'link_file_fname', 'user_id',
                  'user_username', 'link_file', 'user', 'username')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance is not None: # on update
            self.fields['username'].read_only = True  # set to read-only before validation

    def get_link_file_fname(self, obj) -> str:
        return obj.link_file.fname.name

    def create(self, validated_data):
        """
        Overriden to handle the error when trying to create a permission for a user that
        already has a permission granted. Also a link file in the SHARED folder
        pointing to this link file is created if it doesn't exist.
        """
        lf = validated_data['link_file']
        user = validated_data['user']

        try:
            perm = super(FileBrowserLinkFileUserPermissionSerializer,
                         self).create(validated_data)
        except IntegrityError:
            raise serializers.ValidationError(
                {'non_field_errors':
                     [f"User '{user.username}' already has a permission to access "
                      f"link file with id {lf.id}"]})

        shared_lf = lf.create_shared_link()
        shared_lf.grant_user_permission(user, 'r')
        return perm

    def validate_username(self, username):
        """
        Overriden to check whether the provided username exists in the DB.
        """
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise serializers.ValidationError([f"Couldn't find any user with username "
                                               f"'{username}'."])
        return user

    def validate(self, data):
        """
        Overriden to validate that required fields are in data when creating or
        updating a permission.
        """
        if self.instance:  # on update
            if 'permission' not in data:
                raise serializers.ValidationError({'permission':
                                                       ['This field is required.']})
        else:
            if 'username' not in data: # on create
                raise serializers.ValidationError({'username':
                                                       ['This field is required.']})
        return data
