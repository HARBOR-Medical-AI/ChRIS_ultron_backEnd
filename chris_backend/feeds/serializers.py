
from django.contrib.auth.models import User, Group
from django.core.exceptions import ObjectDoesNotExist
from django.db.utils import IntegrityError
from rest_framework import serializers

from core.serializers import save_validated_fields
from plugininstances.enums import STATUS_CHOICES
from .models import (Note, Feed, Tag, Tagging, Comment, FeedGroupPermission,
                     FeedUserPermission)


class NoteSerializer(serializers.HyperlinkedModelSerializer):
    feed = serializers.HyperlinkedRelatedField(view_name='feed-detail', read_only=True)

    class Meta:
        model = Note
        fields = ('url', 'id', 'title', 'content', 'feed')


class TagSerializer(serializers.HyperlinkedModelSerializer):
    owner_username = serializers.ReadOnlyField(source='owner.username')
    feeds = serializers.HyperlinkedIdentityField(view_name='tag-feed-list')
    taggings = serializers.HyperlinkedIdentityField(view_name='tag-tagging-list')

    class Meta:
        model = Tag
        fields = ('url', 'id', 'name', 'owner_username', 'color', 'feeds', 'taggings')


class TaggingSerializer(serializers.HyperlinkedModelSerializer):
    tag_id = serializers.ReadOnlyField(source='tag.id')
    feed_id = serializers.ReadOnlyField(source='feed.id')
    feed = serializers.HyperlinkedRelatedField(view_name='feed-detail', read_only=True)
    tag = serializers.HyperlinkedRelatedField(view_name='tag-detail', read_only=True)

    class Meta:
        model = Tagging
        fields = ('url', 'id', 'tag_id', 'feed_id', 'tag', 'feed')

    def create(self, validated_data):
        """
        Overriden to handle the error when trying to create an already existing tagging.
        """
        try:
            return super(TaggingSerializer, self).create(validated_data)
        except IntegrityError:
            feed = validated_data['feed']
            tag = validated_data['tag']
            raise serializers.ValidationError(
                {'non_field_errors':
                     ["Tagging for feed_id %s and tag_id %s already exists." %
                      (feed.id, tag.id)]})

    def validate_tag(self, tag_id):
        """
        Custom method to check that a tag id is provided and exists in the DB.
        """
        if not tag_id:
            raise serializers.ValidationError({'tag_id': ["A tag id is required."]})
        try:
            pk = int(tag_id)
            tag = Tag.objects.get(pk=pk)
        except (ValueError, ObjectDoesNotExist):
            raise serializers.ValidationError(
                {'tag_id': ["Couldn't find any tag with id %s." % tag_id]})
        return tag

    def validate_feed(self, feed_id):
        """
        Custom method to check that a feed id is provided, exists in the DB and
        the user has feed permission.
        """
        if not feed_id:
            raise serializers.ValidationError({'feed_id': ["A feed id is required."]})
        try:
            pk = int(feed_id)
            feed = Feed.objects.get(pk=pk)
        except (ValueError, ObjectDoesNotExist):
            raise serializers.ValidationError(
                {'feed_id': ["Couldn't find any feed with id %s." % feed_id]})

        user = self.context['request'].user
        if not (feed.owner == user or user.username == 'chris' or
                feed.has_user_permission(user)):
            raise serializers.ValidationError(
                {'feed_id': ["User does not have permission to tag feed with feed_id "
                             "%s." % feed_id]})
        return feed


class FeedSerializer(serializers.HyperlinkedModelSerializer):
    owner_username = serializers.ReadOnlyField(source='owner.username')
    folder_path = serializers.ReadOnlyField(source='folder.path', allow_null=True)
    created_jobs = serializers.SerializerMethodField()
    waiting_jobs = serializers.SerializerMethodField()
    copying_jobs = serializers.SerializerMethodField()
    scheduled_jobs = serializers.SerializerMethodField()
    started_jobs = serializers.SerializerMethodField()
    uploading_jobs = serializers.SerializerMethodField()
    registering_jobs = serializers.SerializerMethodField()
    finished_jobs = serializers.SerializerMethodField()
    errored_jobs = serializers.SerializerMethodField()
    cancelled_jobs = serializers.SerializerMethodField()
    folder = serializers.HyperlinkedRelatedField(view_name='chrisfolder-detail',
                                                 read_only=True)
    note = serializers.HyperlinkedRelatedField(view_name='note-detail', read_only=True)
    group_permissions = serializers.HyperlinkedIdentityField(
        view_name='feedgrouppermission-list')
    user_permissions = serializers.HyperlinkedIdentityField(
        view_name='feeduserpermission-list')
    tags = serializers.HyperlinkedIdentityField(view_name='feed-tag-list')
    taggings = serializers.HyperlinkedIdentityField(view_name='feed-tagging-list')
    comments = serializers.HyperlinkedIdentityField(view_name='comment-list')
    plugin_instances = serializers.HyperlinkedIdentityField(
        view_name='feed-plugininstance-list')
    owner = serializers.HyperlinkedRelatedField(view_name='user-detail', read_only=True)

    class Meta:
        model = Feed
        fields = ('url', 'id', 'creation_date', 'modification_date', 'name', 'public',
                  'owner_username', 'folder_path', 'created_jobs', 'waiting_jobs',
                  'copying_jobs', 'scheduled_jobs', 'started_jobs', 'uploading_jobs', 
                  'registering_jobs', 'finished_jobs',  'errored_jobs', 'cancelled_jobs', 
                  'deletion_status', 'deletion_requested_at', 'deletion_error', 'folder',
                  'note', 'group_permissions', 'user_permissions', 'tags', 'taggings',
                  'comments', 'plugin_instances', 'owner')
        # the deletion's state is the deletion tasks' to write
        read_only_fields = ('deletion_status', 'deletion_requested_at', 'deletion_error')

    def update(self, instance, validated_data):
        """
        Overriden to grant or remove public access to the feed's folder and all its
        descendant folders, link files and files depending on the new public status of
        the feed, and to save only the fields the request changes: a whole save would
        write back the state of a deletion requested since the request read the feed.
        """
        if 'public' in validated_data:
            if instance.public and not validated_data['public']:
                instance.folder.remove_public_link()
                instance.folder.remove_public_access()

            elif not instance.public and validated_data['public']:
                instance.folder.grant_public_access()
                instance.folder.create_public_link()
        return save_validated_fields(instance, validated_data)

    def validate_public(self, public):
        """
        Overriden to check that only the owner or superuser chris can change a feed's
        public status.
        """
        if self.instance:  # validation on update
            user = self.context['request'].user

            if not (self.instance.owner == user or user.username == 'chris'):
                raise serializers.ValidationError(
                    ["Public status of a feed can only be changed by its owner or"
                     "superuser 'chris'."])
        return public

    def get_created_jobs(self, obj) -> int:
        """
        Overriden to get the number of plugin instances in 'created' status.
        """
        if 'created' not in [status[0] for status in STATUS_CHOICES]:
            raise KeyError("Undefined plugin instance execution status: 'created'.")
        return obj.created_jobs if hasattr(obj, 'created_jobs') else 0

    def get_waiting_jobs(self, obj) -> int:
        """
        Overriden to get the number of plugin instances in 'waiting' status.
        """
        if 'waiting' not in [status[0] for status in STATUS_CHOICES]:
            msg = "Undefined plugin instance execution status: 'waiting'."
            raise KeyError(msg)
        return obj.waiting_jobs if hasattr(obj, 'waiting_jobs') else 0

    def get_copying_jobs(self, obj) -> int:
        """
        Overriden to get the number of plugin instances in 'copying' status.
        """
        if 'copying' not in [status[0] for status in STATUS_CHOICES]:
            msg = "Undefined plugin instance execution status: 'copying'."
            raise KeyError(msg)
        return obj.copying_jobs if hasattr(obj, 'copying_jobs') else 0

    def get_scheduled_jobs(self, obj) -> int:
        """
        Overriden to get the number of plugin instances in 'scheduled' status.
        """
        if 'scheduled' not in [status[0] for status in STATUS_CHOICES]:
            raise KeyError("Undefined plugin instance execution status: 'scheduled'.")
        return obj.scheduled_jobs if hasattr(obj, 'scheduled_jobs') else 0

    def get_started_jobs(self, obj) -> int:
        """
        Overriden to get the number of plugin instances in 'started' status.
        """
        if 'started' not in [status[0] for status in STATUS_CHOICES]:
            raise KeyError("Undefined plugin instance execution status: 'started'.")
        return obj.started_jobs if hasattr(obj, 'started_jobs') else 0

    def get_uploading_jobs(self, obj) -> int:
        """
        Overriden to get the number of plugin instances in 'uploading' status.
        """
        if 'uploading' not in [status[0] for status in STATUS_CHOICES]:
            msg = "Undefined plugin instance execution status: 'uploading'."
            raise KeyError(msg)
        return obj.uploading_jobs if hasattr(obj, 'uploading_jobs') else 0

    def get_registering_jobs(self, obj) -> int:
        """
        Overriden to get the number of plugin instances in 'registeringFiles' status.
        """
        if 'registeringFiles' not in [status[0] for status in STATUS_CHOICES]:
            msg = "Undefined plugin instance execution status: 'registeringFiles'."
            raise KeyError(msg)
        return obj.registering_jobs if hasattr(obj, 'registering_jobs') else 0

    def get_finished_jobs(self, obj) -> int:
        """
        Overriden to get the number of plugin instances in 'finishedSuccessfully' status.
        """
        if 'finishedSuccessfully' not in [status[0] for status in STATUS_CHOICES]:
            raise KeyError("Undefined plugin instance execution status: "
                           "'finishedSuccessfully'.")
        return obj.finished_jobs if hasattr(obj, 'finished_jobs') else 0

    def get_errored_jobs(self, obj) -> int:
        """
        Overriden to get the number of plugin instances in 'finishedWithError' status.
        """
        if 'finishedWithError' not in [status[0] for status in STATUS_CHOICES]:
            raise KeyError("Undefined plugin instance execution status: "
                           "'finishedWithError'.")
        return obj.errored_jobs if hasattr(obj, 'errored_jobs') else 0

    def get_cancelled_jobs(self, obj) -> int:
        """
        Overriden to get the number of plugin instances in 'cancelled' status.
        """
        if 'cancelled' not in [status[0] for status in STATUS_CHOICES]:
            raise KeyError("Undefined plugin instance execution status: 'cancelled'.")
        return obj.cancelled_jobs if hasattr(obj, 'cancelled_jobs') else 0


class FeedGroupPermissionSerializer(serializers.HyperlinkedModelSerializer):
    grp_name = serializers.CharField(write_only=True)
    feed_id = serializers.ReadOnlyField(source='feed.id')
    feed_name = serializers.ReadOnlyField(source='feed.name')
    group_id = serializers.ReadOnlyField(source='group.id')
    group_name = serializers.ReadOnlyField(source='group.name')
    feed = serializers.HyperlinkedRelatedField(view_name='feed-detail', read_only=True)
    group = serializers.HyperlinkedRelatedField(view_name='group-detail', read_only=True)

    class Meta:
        model = FeedGroupPermission
        fields = ('url', 'id', 'feed_id', 'feed_name', 'group_id', 'group_name',
                  'feed', 'group', 'grp_name')

    def create(self, validated_data):
        """
        Overriden to handle the error when trying to grant access permission to a group
        that already has the permission granted. Also a link file in the SHARED folder
        pointing to the feed's folder is created if it doesn't exist.
        """
        feed = validated_data['feed']
        group = validated_data['group']

        try:
            feed_perm = super(FeedGroupPermissionSerializer, self).create(validated_data)
        except IntegrityError:
            raise serializers.ValidationError(
                {'non_field_errors':
                     [f"Group '{group.name}' already has permission to access feed "
                      f"with id {feed.id}"]})

        lf = feed.folder.create_shared_link()
        lf.grant_group_permission(group, 'r')
        return feed_perm

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

class FeedUserPermissionSerializer(serializers.HyperlinkedModelSerializer):
    username = serializers.CharField(write_only=True, min_length=4, max_length=32)
    feed_id = serializers.ReadOnlyField(source='feed.id')
    feed_name = serializers.ReadOnlyField(source='feed.name')
    user_id = serializers.ReadOnlyField(source='user.id')
    user_username = serializers.ReadOnlyField(source='user.username')
    feed = serializers.HyperlinkedRelatedField(view_name='feed-detail', read_only=True)
    user = serializers.HyperlinkedRelatedField(view_name='user-detail', read_only=True)

    class Meta:
        model = FeedUserPermission
        fields = ('url', 'id', 'feed_id', 'feed_name', 'user_id','user_username',
                  'feed', 'user', 'username')

    def create(self, validated_data):
        """
        Overriden to handle the error when trying to grant access permission to a user
        that already has the permission granted. Also a link file in the SHARED folder
        pointing to the feed's folder is created if it doesn't exist.
        """
        feed = validated_data['feed']
        user = validated_data['user']

        try:
            feed_perm = super(FeedUserPermissionSerializer, self).create(validated_data)
        except IntegrityError:
            raise serializers.ValidationError(
                {'non_field_errors':
                     [f"User '{user.username}' already has permission to access feed "
                      f"with id {feed.id}"]})

        lf = feed.folder.create_shared_link()
        lf.grant_user_permission(user, 'r')
        return feed_perm

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


class CommentSerializer(serializers.HyperlinkedModelSerializer):
    owner_username = serializers.ReadOnlyField(source='owner.username')
    feed = serializers.HyperlinkedRelatedField(view_name='feed-detail', read_only=True)

    class Meta:
        model = Comment
        fields = ('url', 'id', 'title', 'owner_username', 'content', 'feed')
