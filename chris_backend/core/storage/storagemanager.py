
import abc
from typing import Dict, AnyStr, Iterator, List, NamedTuple, Optional, Tuple


class StorageObject(NamedTuple):
    """
    An object in storage: its path and its size in bytes.
    """
    path: str
    size: int


def sanitized_name(name: str) -> str:
    """
    A file or folder name as ChRIS names it: without its commas, or the empty string
    when nothing but white space is left.
    """
    name = name.replace(',', '')
    return name if name.strip() else ''


def in_tree(path: str, folder: str) -> bool:
    """
    Whether ``path`` is the folder ``folder`` or under it: ``a/data/x`` is in the tree of
    ``a/data``, ``a/data_2/x`` is not.
    """
    folder = folder.rstrip('/')
    return path.rstrip('/') == folder or path.startswith(folder + '/')


def sanitized_path(path: str) -> str:
    """
    ``path`` (relative to the folder whose objects are sanitized) as ChRIS names it: each
    name sanitized (``sanitized_name``), and the folders whose names come out empty left
    out. The empty string when the object's own name comes out empty: that object is
    deleted. ``StorageManager.sanitize_obj_names`` renames objects to these paths.
    """
    names = [sanitized_name(name) for name in path.split('/')]

    if not names[-1]:
        return ''
    return '/'.join(name for name in names if name)


class StorageManager(abc.ABC):
    """
    ``StorageManager`` provides an interface between ChRIS and its file storage backend.

    ``StorageManager`` methods implement helper functions for browsing stored files and retrieving
    file data. These functions are analogous to ``ls``, ``stat``, and ``cat`` commands.
    """

    @abc.abstractmethod
    def create_container(self) -> None:
        """
        Create the container where all ChRIS file data is to be stored.

        For Swift, a container is... a container. For S3, a container is a bucket.

        For a plain filesystem, a container is simply the top-level/parent directory.
        """
        ...

    # A path names an object or a folder on every backend, as on a filesystem: the
    # operations on paths (ls, path_exists, copy_path, move_path, delete_path) reach the
    # object at the path and the objects under the folder it names, never the objects of
    # a sibling whose name only starts with it (``a/data_2`` beside ``a/data``). A path
    # that ends with a slash names a folder only, and the empty path names nothing.

    @abc.abstractmethod
    def ls(self, path: str, limit: Optional[int] = None) -> List[str]:
        """
        :returns: the object at ``path`` and the objects under the folder ``path``, or
                  ``limit`` of them at most (1 or more, in no given order): the listing
                  stops there, however many there are.
        """
        ...

    @abc.abstractmethod
    def walk(self, folder: str) -> Iterator[StorageObject]:
        """
        Every object under ``folder`` with its size, in no given order, as the listing
        goes: it holds a page of the listing at a time (on a filesystem, the list of the
        folders it has still to read), not every object. Unlike ``ls``, it leaves out an
        object at ``folder`` itself. Swift and S3 leave out the markers of empty folders
        (keys ending in ``/``), and a filesystem leaves out symbolic links.
        """
        ...

    def path_exists(self, path: str) -> bool:
        """
        :returns: True if there is an object at ``path`` or under the folder ``path``
        """
        ...

    def obj_exists(self, file_path: str) -> bool:
        """
        :returns: True if given path is an existing file
        """
        ...

    def upload_obj(self, file_path: str, contents: AnyStr, content_type: Optional[str] = None):
        """
        Upload file data to the storage service.

        :param file_path: file path to upload to
        :param contents: file data
        :param content_type: optional media type, e.g. "text/plain"
        """
        ...

    def download_obj(self, file_path: str) -> AnyStr:
        """
        Download file data from the storage service.
        """
        ...

    def copy_obj(self, src: str, dst: str) -> None:
        """
        Copy file data to a new path.

        Instead of a copy, implementations may create links or shallow copies for efficiency.
        """
        ...

    def delete_obj(self, file_path: str) -> None:
        """
        Delete the object at ``file_path``. One that is not there counts as deleted (by
        another task, say): no error, and no request made again for it.
        """
        ...

    def is_missing(self, error: Exception) -> bool:
        """
        Whether ``error``, raised by a call of this manager on an object, says that the
        object is not there (a filesystem's ``FileNotFoundError``; Swift and S3 say it
        their own way).
        """
        return isinstance(error, FileNotFoundError)

    def copy_path(self, src: str, dst: str) -> None:
        """
        Copy the object at ``src``, or the folder ``src``, to ``dst``. What is not there
        is nothing to copy: no error.
        """
        ...

    def move_path(self, src: str, dst: str) -> None:
        """
        Move the object at ``src``, or the folder ``src``, to ``dst``. What is not there
        (a folder no object was put in, say) is nothing to move: no error.
        """
        ...

    def delete_path(self, path: str) -> None:
        """
        Delete the object at ``path`` and every object under the folder ``path``. What is
        not there counts as deleted: no error.
        """
        ...

    def sanitize_obj_names(self, path: str) -> Dict[str, str]:
        """
        Removes commas from the names of all files and folders under the input path.
        Handles special cases:
            - Files with names that only contain commas and white spaces are deleted.
            - Folders with names that only contain commas and white spaces are removed
            after moving their contents to the parent folder.

        Returns a dictionary that only contains the modified file paths. Keys are the
        original file paths and values are the new file paths. Deleted files have
        the empty string as their value.

        Objects have no folders to rename: each object whose path changes
        (``sanitized_path``) is copied to its new path and deleted. An object that a
        listing shows but that is not there (``is_missing``: renamed by another task, or
        deleted a moment before the listing was made) is taken as renamed already.
        """
        folder = path.rstrip('/')
        changes = {}

        for obj in self.walk(folder):
            relative = obj.path[len(folder) + 1:]
            new = sanitized_path(relative)

            if new != relative:
                changes[obj.path] = f'{folder}/{new}' if new else ''

        for old, new in changes.items():  # once listed: a listing may meet a new path
            try:
                if new:
                    self.copy_obj(old, new)
                self.delete_obj(old)
            except Exception as error:
                if not self.is_missing(error):
                    raise
        return changes


class ObjectStorageManager(StorageManager):
    """
    What Swift and S3 do alike. They list objects by a raw prefix of their names, so the
    listing of ``a/data`` holds the objects of ``a/data_2`` too: the operations on paths
    list a path's objects with ``_tree``, which keeps to the path.
    """

    @abc.abstractmethod
    def _objects(self, prefix: str, limit: Optional[int] = None) -> Iterator[StorageObject]:
        """
        Every object whose name starts with ``prefix``, with its size, in the order of
        their names, ``limit`` of them at most when given, a page of a listing at a time.
        """
        ...

    def _tree(self, path: str, limit: Optional[int] = None) -> Iterator[StorageObject]:
        """
        The object at ``path``, then the objects under the folder ``path``, ``limit`` of
        them at most. Swift and S3 list names in their binary order, where a name comes
        before every longer name that starts with it: a listing of one object by the
        prefix ``path`` holds the object at ``path`` when there is one. A ``path`` that
        ends with a slash names the folder only.
        """
        folder = path.rstrip('/')
        if not folder:
            return
        if path == folder:
            first = next(self._objects(folder, 1), None)
            if first is not None and first.path == folder:
                yield first
                if limit is not None:
                    limit -= 1
        if limit is None or limit > 0:
            yield from self._objects(folder + '/', limit)

    def ls(self, path: str, limit: Optional[int] = None) -> List[str]:
        return [obj.path for obj in self._tree(path, limit)]

    def walk(self, folder: str) -> Iterator[StorageObject]:
        for obj in self._objects(folder.rstrip('/') + '/'):
            if not obj.path.endswith('/'):  # not the marker of a folder
                yield obj

    def path_exists(self, path: str) -> bool:
        return next(self._tree(path, 1), None) is not None

    def copy_path(self, src: str, dst: str) -> None:
        for path, new_path in self._renamed(src, dst):
            self.copy_obj(path, new_path)

    def move_path(self, src: str, dst: str) -> None:
        for path, new_path in self._renamed(src, dst):
            self.copy_obj(path, new_path)
            self.delete_obj(path)

    def delete_path(self, path: str) -> None:
        for obj in self._tree(path):
            self.delete_obj(obj.path)

    def _renamed(self, src: str, dst: str) -> List[Tuple[str, str]]:
        """
        The path of each object of ``src`` and its path under ``dst``, all of them listed
        before the first is copied: a copy may land where the listing has yet to go.
        """
        old, new = src.rstrip('/'), dst.rstrip('/')
        return [(obj.path, new + obj.path[len(old):]) for obj in self._tree(src)]
