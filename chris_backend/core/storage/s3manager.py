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
from pathlib import Path
from typing import Dict, List, AnyStr, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from core.storage.storagemanager import StorageManager

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
        l_ls = []
        if not path:
            return l_ls
        client = self.__get_client()
        paging = {} if limit is None else {
            'PaginationConfig': {'MaxItems': limit, 'PageSize': min(limit, 1000)}}
        for i in range(5):
            l_ls = []  # a try again lists from the start
            try:
                paginator = client.get_paginator('list_objects_v2')
                pages = paginator.paginate(Bucket=self.bucket_name, Prefix=path, **paging)
                for page in pages:
                    for obj in page.get('Contents', []):
                        l_ls.append(obj['Key'])
            except ClientError as e:
                logger.error(str(e))
                if i == 4:
                    raise
                time.sleep(0.4)
            else:
                break
        return l_ls

    def path_exists(self, path: str) -> bool:
        """
        Return True if any objects exist under the given path prefix.
        """
        client = self.__get_client()
        for i in range(5):
            try:
                resp = client.list_objects_v2(
                    Bucket=self.bucket_name, Prefix=path, MaxKeys=1
                )
                return resp.get('KeyCount', 0) > 0
            except ClientError as e:
                logger.error(str(e))
                if i == 4:
                    raise
                time.sleep(0.4)

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
        paginator = client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=self.bucket_name, Prefix=path)

        for page in pages:
            contents = page.get('Contents', [])
            if not contents:
                continue
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

    def sanitize_obj_names(self, path: str) -> Dict[str, str]:
        """
        Removes commas from the paths of all objects that start with the specified
        input path/prefix.
        Handles special cases:
            - Objects with names that only contain commas and white spaces are deleted.
            - "Folders" with names that only contain commas and white spaces are removed
            after moving their contents to the parent folder.

        Returns a dictionary that only contains modified object paths. Keys are the
        original object paths and values are the new object paths. Deleted objects have
        the empty string as the value.
        """
        new_obj_paths = {}
        l_ls = self.ls(path)

        if len(l_ls) != 1 or l_ls[0] != path:  # path is a prefix
            p = Path(path)

            for obj_path in l_ls:
                p_obj = Path(obj_path)

                if p_obj.name.replace(',', '').strip() == '':
                    self.delete_obj(obj_path)
                    new_obj_paths[obj_path] = ''
                else:
                    new_parts = []
                    for part in p_obj.relative_to(p).parts:
                        new_part = part.replace(',', '')
                        if new_part.strip() != '':
                            new_parts.append(new_part)

                    new_p_obj = p / Path(*new_parts)

                    if new_p_obj != p_obj:
                        new_obj_path = str(new_p_obj)
                        self.copy_obj(obj_path, new_obj_path)
                        self.delete_obj(obj_path)
                        new_obj_paths[obj_path] = new_obj_path
        return new_obj_paths
