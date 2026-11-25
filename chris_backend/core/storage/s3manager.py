"""
S3-compatible storage manager module.

Supports any S3-compatible backend: AWS S3, MinIO, Ceph RGW, SeaweedFS, or
Azure Blob Storage via S3Proxy gateway.

NOTE on boto3 >= 1.36.0 (January 2025): AWS enabled automatic CRC checksums on
PutObject and checksum validation on GetObject by default. This breaks all
S3-compatible services (MinIO, Ceph, S3Proxy, etc.) because their ETags are not
MD5 hashes. We disable this via botocore.config.Config below.
"""

import logging
import time
from typing import AnyStr, Iterator, List, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from core.storage.storagemanager import StorageManager, StorageObject

logger = logging.getLogger(__name__)

# boto3 config for S3-compatible backends:
# - path addressing: required for non-AWS endpoints (no virtual-hosted bucket DNS)
# - checksum disabled: boto3 >= 1.36.0 auto-CRC breaks non-AWS S3 implementations
_S3_CLIENT_CONFIG = Config(
    s3={'addressing_style': 'path'},
    request_checksum_calculation='when_required',
    response_checksum_validation='when_required',
)


class S3Manager(StorageManager):

    #: the most keys S3 returns in one listing request
    PAGE_SIZE = 1000

    def __init__(self, bucket_name: str, conn_params: dict):
        self.bucket_name = bucket_name
        self.conn_params = conn_params
        self._client = None

    def __get_client(self):
        """
        Connect to S3-compatible storage and return the client object.
        """
        if self._client is not None:
            return self._client
        for i in range(5):
            try:
                self._client = boto3.client(
                    's3',
                    endpoint_url=self.conn_params.get('endpoint_url'),
                    aws_access_key_id=self.conn_params.get('access_key'),
                    aws_secret_access_key=self.conn_params.get('secret_key'),
                    region_name=self.conn_params.get('region_name', 'us-east-1'),
                    config=_S3_CLIENT_CONFIG,
                )
            except ClientError as e:
                logger.error(str(e))
                if i == 4:
                    raise
                time.sleep(0.4)
            else:
                return self._client

    def create_container(self) -> None:
        """
        Create the S3 bucket if it does not already exist.
        """
        client = self.__get_client()
        try:
            client.head_bucket(Bucket=self.bucket_name)
        except ClientError as e:
            error_code = int(e.response['Error']['Code'])
            if error_code == 404:
                create_kwargs = {'Bucket': self.bucket_name}
                region = self.conn_params.get('region_name')
                if region and region != 'us-east-1':
                    create_kwargs['CreateBucketConfiguration'] = {
                        'LocationConstraint': region
                    }
                client.create_bucket(**create_kwargs)
            else:
                logger.error(str(e))
                raise

    def ls(self, path: str, limit: Optional[int] = None) -> List[str]:
        """
        Return a list of object keys in the bucket with the given path as prefix, of
        ``limit`` keys at most when given: a request per 1,000 of them.
        """
        if not path:
            return []
        return [obj['Key'] for page in self._pages(path, limit) for obj in page]

    def walk(self, folder: str) -> Iterator[StorageObject]:
        for page in self._pages(folder.rstrip('/') + '/'):
            for obj in page:
                if not obj['Key'].endswith('/'):  # not the marker of a folder
                    yield StorageObject(obj['Key'], obj['Size'])

    def _pages(self, prefix: str, limit: Optional[int] = None) -> Iterator[List[dict]]:
        """
        The objects whose keys start with ``prefix`` (``Key``, ``Size``...), a page of a
        listing request at a time, ``limit`` objects in all at most when given. A request
        that fails is made again on its own, up to 5 tries in all, so nothing is listed
        twice.
        """
        client = self.__get_client()
        request: dict = {'Bucket': self.bucket_name, 'Prefix': prefix}
        left = limit

        while left is None or left > 0:
            request['MaxKeys'] = self.PAGE_SIZE if left is None else min(left, self.PAGE_SIZE)
            resp = self._list(client, request)
            page = resp.get('Contents', [])
            if left is not None:
                left -= len(page)
            if page:
                yield page
            if not resp.get('IsTruncated'):
                return
            if not resp.get('NextContinuationToken'):
                raise ValueError(f'The listing of {prefix} in bucket {self.bucket_name} '
                                 f'is cut short without a continuation token')
            request['ContinuationToken'] = resp['NextContinuationToken']

    @staticmethod
    def _list(client, request: dict) -> dict:
        """
        The response to one listing request, made again on a failure, up to 5 tries.
        """
        attempt = 1
        while True:
            try:
                return client.list_objects_v2(**request)
            except ClientError as e:
                logger.error(str(e))
                if attempt == 5:
                    raise
                attempt += 1
                time.sleep(0.4)

    def path_exists(self, path: str) -> bool:
        """
        Return True if any objects exist under the given path prefix.
        """
        return any(True for _ in self._pages(path, limit=1))

    def obj_exists(self, file_path: str) -> bool:
        """
        Return True if an object exists at the exact key.
        """
        client = self.__get_client()
        for i in range(5):
            try:
                client.head_object(Bucket=self.bucket_name, Key=file_path)
            except ClientError as e:
                if e.response['Error']['Code'] == '404':
                    return False
                logger.error(str(e))
                if i == 4:
                    raise
                time.sleep(0.4)
            else:
                return True

    def upload_obj(self, file_path: str, contents: AnyStr,
                   content_type: Optional[str] = None) -> None:
        """
        Upload data to S3 at the given key.
        """
        if isinstance(contents, str):
            contents = contents.encode('utf-8')
        client = self.__get_client()
        put_kwargs = {
            'Bucket': self.bucket_name,
            'Key': file_path,
            'Body': contents,
        }
        if content_type:
            put_kwargs['ContentType'] = content_type
        for i in range(5):
            try:
                client.put_object(**put_kwargs)
            except ClientError as e:
                logger.error(str(e))
                if i == 4:
                    raise
                time.sleep(0.4)
            else:
                break

    def download_obj(self, file_path: str) -> bytes:
        """
        Download object data from S3.
        """
        client = self.__get_client()
        for i in range(5):
            try:
                resp = client.get_object(Bucket=self.bucket_name, Key=file_path)
                return resp['Body'].read()
            except ClientError as e:
                logger.error(str(e))
                if i == 4:
                    raise
                time.sleep(0.4)

    def copy_obj(self, src: str, dst: str) -> None:
        """
        Copy an object within the same bucket.
        """
        client = self.__get_client()
        copy_source = {'Bucket': self.bucket_name, 'Key': src}
        for i in range(5):
            try:
                client.copy_object(
                    Bucket=self.bucket_name,
                    Key=dst,
                    CopySource=copy_source,
                )
            except ClientError as e:
                logger.error(str(e))
                if i == 4:
                    raise
                time.sleep(0.4)
            else:
                break

    def delete_obj(self, file_path: str) -> None:
        """
        Delete an object from S3.
        """
        client = self.__get_client()
        for i in range(5):
            try:
                client.delete_object(Bucket=self.bucket_name, Key=file_path)
            except ClientError as e:
                logger.error(str(e))
                if i == 4:
                    raise
                time.sleep(0.4)
            else:
                break

    def is_missing(self, error: Exception) -> bool:
        return (isinstance(error, ClientError)
                and error.response.get('Error', {}).get('Code') in ('404', 'NoSuchKey'))

    def copy_path(self, src: str, dst: str) -> None:
        """
        Copy all objects under src prefix to dst prefix.
        """
        l_ls = self.ls(src)
        for obj_key in l_ls:
            new_key = obj_key.replace(src, dst, 1)
            self.copy_obj(obj_key, new_key)

    def move_path(self, src: str, dst: str) -> None:
        """
        Move all objects under src prefix to dst prefix (copy + delete).
        """
        l_ls = self.ls(src)
        for obj_key in l_ls:
            new_key = obj_key.replace(src, dst, 1)
            self.copy_obj(obj_key, new_key)
            self.delete_obj(obj_key)

    def delete_path(self, path: str) -> None:
        """
        Delete all objects under a given prefix.

        Uses batch delete for efficiency (up to 1000 objects per request).
        """
        client = self.__get_client()

        for contents in self._pages(path):
            delete_keys = [{'Key': obj['Key']} for obj in contents]
            for i in range(5):
                try:
                    client.delete_objects(
                        Bucket=self.bucket_name,
                        Delete={'Objects': delete_keys, 'Quiet': True},
                    )
                except ClientError as e:
                    logger.error(str(e))
                    if i == 4:
                        raise
                    time.sleep(0.4)
                else:
                    break
