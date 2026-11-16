"""
Swift storage manager module.
"""

import logging
import os
import time
from pathlib import Path
from typing import Dict

from swiftclient import Connection
from swiftclient.exceptions import ClientException

from core.storage.storagemanager import StorageManager

logger = logging.getLogger(__name__)


class SwiftManager(StorageManager):

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
        as a prefix, of ``limit`` objects at most when given, in one request: Swift
        refuses a limit above its listing limit (10,000 by default) with a 412.
        """
        return self._ls(path, b_full_listing=limit is None, limit=limit)

    def _ls(self, path, b_full_listing: bool, limit=None):
        """
        Note to developers: the body of ``_ls`` was originally the body of ``self.ls``,
        though it's been renamed to ``_ls`` so that ``self.ls``'s signature could be
        changed. ``self.ls`` originally accepted ``**kwargs`` but that is no longer the case.
        """
        l_ls = []  # listing of names to return
        if path:
            conn = self.__get_connection()
            for i in range(5):
                try:
                    # the objects in Swift storage with the given prefix: all of them,
                    # or the first page, of ``limit`` objects at most when given
                    ld_obj = conn.get_container(self.container_name,
                                                prefix=path,
                                                limit=limit,
                                                full_listing=b_full_listing)[1]
                except ClientException as e:
                    logger.error(str(e))
                    if i == 4:
                        raise
                    time.sleep(0.4)
                else:
                    l_ls = [d_obj['name'] for d_obj in ld_obj]
                    break
        return l_ls

    def path_exists(self, path):
        """
        Return True/False if passed path exists in swift storage.
        """
        return len(self._ls(path, b_full_listing=False)) > 0

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

        if len(l_ls) != 1 or l_ls[0] != path:  # Path is a prefix
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

                    if new_p_obj != p_obj:  # Final file path is different
                        new_obj_path = str(new_p_obj)
                        self.copy_obj(obj_path, new_obj_path)
                        self.delete_obj(obj_path)
                        new_obj_paths[obj_path] = new_obj_path
        return new_obj_paths
