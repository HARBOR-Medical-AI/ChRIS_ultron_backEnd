
import abc
from typing import Dict, AnyStr, Iterator, List, NamedTuple, Optional


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

    def ls(self, path_prefix: str, limit: Optional[int] = None) -> List[str]:
        """
        :returns: a list of all files under a given path prefix, or of ``limit`` of them
                  at most (1 or more, in no given order): the listing stops there, however
                  many there are.
        """
        ...

    @abc.abstractmethod
    def walk(self, folder: str) -> Iterator[StorageObject]:
        """
        Every object under ``folder`` with its size, in no given order, as the listing
        goes: it holds a page of the listing at a time (on a filesystem, the list of the
        folders it has still to read), not every object. Unlike ``ls`` on Swift and S3, a
        folder is a
        folder: objects of a sibling whose name starts with the folder's (``a/data_2``
        beside ``a/data``) are not under it, and neither is an object at ``folder``
        itself. Swift and S3 leave out the markers of empty folders (keys ending in
        ``/``), and a filesystem leaves out symbolic links.
        """
        ...

    def path_exists(self, path: str) -> bool:
        """
        :returns: True if path exists (whether it be a directory OR file)
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
        Delete file data from the given path.
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
        Copy all the data under a src path to a new dst path.
        """
        ...

    def move_path(self, src: str, dst: str) -> None:
        """
        Move all the data under a src path to a new dst path.
        """
        ...

    def delete_path(self, path: str) -> None:
        """
        Delete all the data under a path.
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
