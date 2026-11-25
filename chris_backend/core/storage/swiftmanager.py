"""
Swift storage manager module.
"""

import logging
import os
import time
from typing import Iterator, List, Optional

from swiftclient import Connection
from swiftclient.exceptions import ClientException

from core.storage.storagemanager import StorageManager, StorageObject

logger = logging.getLogger(__name__)


class SwiftManager(StorageManager):

    #: Swift's default listing limit: the most objects a listing request may ask for
    PAGE_SIZE = 10000

    def __init__(self, container_name, conn_params):
        self.container_name = container_name
        # swift storage connection parameters dictionary
        self.conn_params = conn_params
        # swift storage connection object
        self._conn = None

    def __get_connection(self):
        """
        Connect to swift storage and return the connection object.
        """
        if self._conn is not None:
            return self._conn
        for i in range(5):  # 5 retries at most
            try:
                self._conn = Connection(**self.conn_params)
            except ClientException as e:
                logger.error(str(e))
                if i == 4:
                    raise  # give up
                time.sleep(0.4)
            else:
                return self._conn

    def create_container(self):
        """
        Create the storage container.
        """
        conn = self.__get_connection()
        try:
            conn.put_container(self.container_name)
        except ClientException as e:
            logger.error(str(e))
            raise

    def ls(self, path, limit=None):
        """
        Return a list of objects in the swift storage with the provided path
        as a prefix, of ``limit`` objects at most when given: in one request when
        ``limit`` is at most 10,000 (Swift's default listing limit).
        """
        if not path:
            return []
        return [obj['name'] for page in self._pages(path, limit) for obj in page]

    def walk(self, folder: str) -> Iterator[StorageObject]:
        for page in self._pages(folder.rstrip('/') + '/'):
            for obj in page:
                if not obj['name'].endswith('/'):  # not the marker of a folder
                    yield StorageObject(obj['name'], obj['bytes'])

    def _pages(self, prefix: str, limit: Optional[int] = None) -> Iterator[List[dict]]:
        """
        The objects whose names start with ``prefix`` (``name``, ``bytes``...), a page of
        a listing request at a time, each after the last one's last name, ``limit``
        objects in all at most when given.
            - Without ``limit`` a request takes a page of Swift's own size (its listing
              limit), and the listing ends with an empty page.
            - With ``limit`` a request asks for what is left of it, 10,000 at most
              (Swift's default listing limit: it refuses more than its own with a 412),
              and the listing ends with a page shorter than asked.
        A request that fails is made again on its own, up to 5 tries in all, so nothing
        is listed twice.
        """
        conn = self.__get_connection()
        marker, left = '', limit

        while left is None or left > 0:
            asked = self.PAGE_SIZE if left is None else min(left, self.PAGE_SIZE)
            page = self._list(conn, prefix, marker, None if left is None else asked)
            if not page:
                return
            yield page
            if left is not None:
                if len(page) < asked:
                    return
                left -= len(page)
            marker = page[-1]['name']

    def _list(self, conn, prefix: str, marker: str, limit: Optional[int]) -> List[dict]:
        """
        One page of a listing, the request made again on a failure, up to 5 tries.
        """
        attempt = 1
        while True:
            try:
                return conn.get_container(self.container_name, prefix=prefix,
                                          marker=marker, limit=limit)[1]
            except ClientException as e:
                logger.error(str(e))
                if attempt == 5:
                    raise
                attempt += 1
                time.sleep(0.4)

    def path_exists(self, path):
        """
        Return True/False if passed path exists in swift storage.
        """
        return bool(path) and any(True for _ in self._pages(path, limit=1))

    def obj_exists(self, obj_path):
        """
        Return True/False if passed object exists in swift storage.
        """
        conn = self.__get_connection()
        for i in range(5):
            try:
                conn.head_object(self.container_name, obj_path)
            except ClientException as e:
                if e.http_status == 404:
                    return False
                else:
                    logger.error(str(e))
                    if i == 4:
                        raise
                    time.sleep(0.4)
            else:
                return True

    def upload_obj(self, swift_path, contents, content_type=None):
        """
        Upload an object (a file contents) into swift storage.
        """
        conn = self.__get_connection()
        for i in range(5):
            try:
                conn.put_object(self.container_name,
                                swift_path,
                                contents=contents,
                                content_type=content_type)
            except ClientException as e:
                logger.error(str(e))
                if i == 4:
                    raise
                time.sleep(0.4)
            else:
                break

    def download_obj(self, obj_path):
        """
        Download an object from swift storage.
        """
        conn = self.__get_connection()
        for i in range(5):
            try:
                resp_headers, obj_contents = conn.get_object(self.container_name, obj_path)
            except ClientException as e:
                logger.error(str(e))
                if i == 4:
                    raise
                time.sleep(0.4)
            else:
                return obj_contents

    def copy_obj(self, obj_path, dest_path):
        """
        Copy an object to a new destination in swift storage.
        """
        conn = self.__get_connection()
        dest = os.path.join('/' + self.container_name, dest_path.lstrip('/'))
        for i in range(5):
            try:
                conn.copy_object(self.container_name, obj_path, dest)
            except ClientException as e:
                logger.error(str(e))
                if i == 4:
                    raise
                time.sleep(0.4)
            else:
                break

    def delete_obj(self, obj_path):
        """
        Delete an object from swift storage.
        """
        conn = self.__get_connection()
        for i in range(5):
            try:
                conn.delete_object(self.container_name, obj_path)
            except ClientException as e:
                logger.error(str(e))
                if i == 4:
                    raise
                time.sleep(0.4)
            else:
                break

    def is_missing(self, error: Exception) -> bool:
        return isinstance(error, ClientException) and error.http_status == 404

    def copy_path(self, src: str, dst: str) -> None:
        l_ls = self.ls(src)
        for obj_path in l_ls:
            new_obj_path = obj_path.replace(src, dst, 1)
            self.copy_obj(obj_path, new_obj_path)

    def move_path(self, src: str, dst: str) -> None:
        l_ls = self.ls(src)
        for obj_path in l_ls:
            new_obj_path = obj_path.replace(src, dst, 1)
            self.copy_obj(obj_path, new_obj_path)
            self.delete_obj(obj_path)

    def delete_path(self, path: str) -> None:
        l_ls = self.ls(path)
        for obj_path in l_ls:
            self.delete_obj(obj_path)
