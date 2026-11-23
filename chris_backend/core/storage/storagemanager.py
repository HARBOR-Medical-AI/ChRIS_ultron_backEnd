
import abc
from typing import List, Dict, AnyStr, Optional


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
        """
        ...
