
import os

from rest_framework import serializers

from core.models import ChrisFolder
from core.serializers import (ChrisFileSerializer, public_link_follows,
                              validate_path_not_in_use)
from .models import UserFile


class UserFileSerializer(ChrisFileSerializer):
    upload_path = serializers.CharField(max_length=1024, write_only=True, required=False)
    group_permissions = serializers.HyperlinkedIdentityField(
        view_name='filegrouppermission-list')
    user_permissions = serializers.HyperlinkedIdentityField(
        view_name='fileuserpermission-list')

    class Meta:
        model = UserFile
        fields = ('url', 'id', 'creation_date', 'upload_path', 'fname', 'fsize', 'public',
                  'owner_username', 'file_resource', 'parent_folder', 'group_permissions',
                  'user_permissions','owner')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance is not None: # on update
            self.fields['fname'].read_only = True  # set to read-only before validation

    def create(self, validated_data):
        """
        Overriden to set the file's saving path and parent folder. It also creates
        non-existent ancestor folders and sets their permissions to be the same as the
        first existing ancestor folder.
        """
        # user file will be stored at: SWIFT_CONTAINER_NAME/<upload_path>
        # where <upload_path> must start with home/<username>/
        upload_path = validated_data.pop('upload_path')
        folder_path = os.path.dirname(upload_path)
        owner = validated_data['owner']

        parent_folder = ancestor_folder = ChrisFolder.get_first_existing_folder_ancestor(
            upload_path)
        if ancestor_folder.path != folder_path:
            parent_folder = ChrisFolder.objects.create(path=folder_path, owner=owner)

        validated_data['parent_folder'] = parent_folder
        user_file = UserFile(**validated_data)
        user_file.fname.name = upload_path
        user_file.save()

        if ancestor_folder.path == folder_path:
            top_created_obj = user_file
        else:
            parent_folder_path_parts = folder_path.split('/')
            ancestor_folder_path_parts = ancestor_folder.path.split('/')
            next_part = parent_folder_path_parts[len(ancestor_folder_path_parts)]
            top_created_obj_path = ancestor_folder.path + '/' + next_part

            if top_created_obj_path == folder_path:
                top_created_obj = parent_folder
            else:
                top_created_obj = ChrisFolder.objects.get(path=top_created_obj_path)

        if ancestor_folder.public:
            top_created_obj.grant_public_access()
            user_file.public = True  # update object before returning it

        for perm in ancestor_folder.get_groups_permissions_queryset():
            top_created_obj.grant_group_permission(perm.group, perm.permission)

        for perm in ancestor_folder.get_users_permissions_queryset():
            top_created_obj.grant_user_permission(perm.user, perm.permission)

        if owner != ancestor_folder.owner:
            top_created_obj.grant_user_permission(ancestor_folder.owner, 'w')
        return user_file

    def update(self, instance, validated_data):
        """
        Overriden to grant or remove public access to the file and/or move it to a new
        path.
        """
        public = instance.public

        if public and 'public' in validated_data and not validated_data['public']:
            instance.remove_public_link()
            instance.remove_public_access()

        upload_path = validated_data.pop('upload_path', None)

        if upload_path:
            stays_public = public and validated_data.get('public', True)
            # user file will be stored at: SWIFT_CONTAINER_NAME/<upload_path>
            # where <upload_path> must start with home/
            with public_link_follows(instance, stays_public):
                instance.move(upload_path)

        if not public and 'public' in validated_data and validated_data['public']:
            instance.grant_public_access()
            instance.create_public_link()
        return instance

    def validate_upload_path(self, upload_path):
        """
        Overriden to check whether the provided path does not contain commas and is
        under a home/'s subdirectory for which the user has write permission.
        """
        if ',' in upload_path:
            raise serializers.ValidationError([f"Invalid path. Cannot contain commas."])

        upload_path = upload_path.strip().strip('/')

        if upload_path.endswith('.chrislink'):
            raise serializers.ValidationError(["Invalid path. Uploading ChRIS link "
                                               "files is not allowed."])
        if not upload_path.startswith('home/'):
            raise serializers.ValidationError(["Invalid path. Path must start with "
                                               "'home/'."])

        ancestor_folder = ChrisFolder.get_first_existing_folder_ancestor(upload_path)

        if ancestor_folder.path == upload_path:
            raise serializers.ValidationError([f"A folder with path '{upload_path}' "
                                               f"already exists."])
        user = self.context['request'].user
        if not (ancestor_folder.owner == user or ancestor_folder.public or
                ancestor_folder.has_user_permission(user, 'w')):
            raise serializers.ValidationError([f"Invalid path. User does not have write "
                                               f"permission under the folder "
                                               f"'{ancestor_folder.path}'."])
        validate_path_not_in_use(upload_path, self.instance)
        return upload_path

    def validate(self, data):
        """
        Overriden to validate that at least one of two fields are in data when
        updating a file. Also to validate that required fields are in data on create
        and remove the 'public' field if passed.
        """
        if self.instance:  # on update
            if 'public' not in data and 'upload_path' not in data:
                raise serializers.ValidationError(
                    {'non_field_errors': ["At least one of the fields 'public' "
                                          "or 'upload_path' must be provided."]})
        else:  # on create
            if 'upload_path' not in data:
                raise serializers.ValidationError(
                    {'upload_path': ["This field is required."]})
            if 'fname' not in data:
                raise serializers.ValidationError(
                    {'fname': ["This field is required."]})

            data.pop('public', None)  # can only be set to public on update
        return data
