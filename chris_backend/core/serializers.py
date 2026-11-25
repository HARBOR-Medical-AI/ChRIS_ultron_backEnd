
from contextlib import contextmanager

from rest_framework import serializers
from rest_framework.utils import model_meta

from drf_spectacular.utils import OpenApiTypes, extend_schema_field

from collectionjson.fields import ItemLinkField
from .models import ChrisInstance, FileDownloadToken, ChrisFile, ChrisLinkFile, path_in_use
from .utils import get_file_resource_link


def validate_path_not_in_use(path, instance=None):
    """
    Raise a ValidationError when ``path``, where a file or link file is to be uploaded
    or moved (``instance``, when it is there already) or a folder made, is in use
    (``path_in_use``): a file, a link file or a folder has it, or a file or a link file
    has a path above it. ``instance``'s own path is allowed, as a move to it does
    nothing.
    """
    if (instance is None or path != instance.fname.name) and path_in_use(path):
        raise serializers.ValidationError(
            [f"Invalid path. A file, link file or folder already has path '{path}'."])


@contextmanager
def public_link_follows(instance, public):
    """
    Around a move of ``instance`` (a folder, a file or a link file), when ``public``:
    take its public link away, and make it again for the path it has after the move,
    the old one when the move failed.
    """
    if public:
        instance.remove_public_link()
    try:
        yield
    finally:
        if public:
            instance.create_public_link()


def save_validated_fields(instance, validated_data):
    """
    Update ``instance`` as ``ModelSerializer.update`` does, but save only the fields in
    ``validated_data``: a whole save would write back what another request or a task
    changed since this request read the instance (the state of its deletion, say).
    Returns the instance.
    """
    relations = model_meta.get_field_info(instance).relations
    to_many = {}

    for attr, value in validated_data.items():
        if attr in relations and relations[attr].to_many:
            to_many[attr] = value
        else:
            setattr(instance, attr, value)

    fields = [attr for attr in validated_data if attr not in to_many]
    if fields:
        instance.save(update_fields=fields)

    for attr, value in to_many.items():
        getattr(instance, attr).set(value)
    return instance


class ChrisInstanceSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = ChrisInstance
        fields = ('url', 'id', 'creation_date', 'name', 'uuid', 'job_id_prefix',
                  'description')


class FileDownloadTokenSerializer(serializers.HyperlinkedModelSerializer):
    token = serializers.CharField(required=False)
    owner_username = serializers.ReadOnlyField(source='owner.username')
    owner = serializers.HyperlinkedRelatedField(view_name='user-detail', read_only=True)

    class Meta:
        model = FileDownloadToken
        fields = ('url', 'id', 'creation_date', 'token', 'owner_username', 'owner')


class _ChrisFileSerializerMetaclass(serializers.SerializerMetaclass):
    """
    A metaclass to require that the Meta inner class' model attribute is ChrisFile.
    """
    def __new__(cls, name, bases, dct):
        if set(bases) == {serializers.HyperlinkedModelSerializer}:
            # skip validation of ChrisFileSerializer, which is a
            # direct subclass of HyperlinkedModelSerializer
            return super().__new__(cls, name, bases, dct)
        if (Meta := dct.get('Meta', None)) is None:
            raise TypeError(f'{name} must have an inner class called Meta')
        if not issubclass(getattr(Meta, 'model', None), (ChrisFile, ChrisLinkFile)):
            raise TypeError(f'{name}.Meta.model must be ChrisFile or ChrisLinkFile')
        return super().__new__(cls, name, bases, dct)


class ChrisFileSerializer(serializers.HyperlinkedModelSerializer,
                          metaclass=_ChrisFileSerializerMetaclass):
    """
    A superclass for serializers of ``ChrisFile`` or similar.
    """
    fname = serializers.FileField(use_url=False, allow_empty_file=True, required=False)
    fsize = serializers.SerializerMethodField()
    file_resource = ItemLinkField('get_file_link')
    owner_username = serializers.ReadOnlyField(source='owner.username')
    parent_folder = serializers.HyperlinkedRelatedField(view_name='chrisfolder-detail',
                                                        read_only=True)
    owner = serializers.HyperlinkedRelatedField(view_name='user-detail', read_only=True)

    def get_fsize(self, obj) -> int:
        """
        Get the size of the file in bytes.
        """
        return obj.fname.size

    @extend_schema_field(OpenApiTypes.URI)
    def get_file_link(self, obj):
        """
        Custom method to get the hyperlink to the actual file resource.
        """
        return get_file_resource_link(self, obj)
