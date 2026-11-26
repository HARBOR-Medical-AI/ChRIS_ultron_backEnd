
from itertools import islice
import os
from pathlib import Path, PurePosixPath
import shutil
from typing import Union, List, Dict, AnyStr, Iterator, Optional, Tuple

from core.storage.storagemanager import (StorageManager, StorageObject, sanitized_name,
                                         sanitized_path)


def _raise_unless_missing(function, path, error):
    """
    The ``onexc`` handler of ``shutil.rmtree``: an error that says that what it deletes
    is gone already (another task deleted it) is ignored, any other is raised.
    """
    if not isinstance(error, FileNotFoundError):
        raise error


class FilesystemManager(StorageManager):
    """
    The simplest manager, something everyone has, the one you can trust...

    ``FilesystemManager`` is for storing files on disk as-is, no magic involved.
    More technically, ``FilesystemManager`` methods adapt method calls of ``pathlib`` to the ``StoreManager`` interface.

    This code can be used as a reference for how to implement ``StorageManager``
    for other file storage services.
    """

    def __init__(self, base: Union[str, Path]):
        self.__base = Path(base)

    def create_container(self) -> None:
        self.__base.mkdir(exist_ok=True, parents=True)

    def ls(self, path: str, limit: Optional[int] = None) -> List[str]:
        named = self.__named(path)
        if named is None:
            return []
        if named.is_file():
            return [path.strip('/')]
        # the files that rglob finds: symbolic links to files, but not the trees of links
        # to directories
        files = (name for name, entry in self.__entries(path.strip('/'))
                 if entry.is_file())
        return list(islice(files, limit))

    def walk(self, folder: str) -> Iterator[StorageObject]:
        for path, entry in self.__entries(folder):
            if entry.is_file(follow_symlinks=False):
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except FileNotFoundError:  # deleted since its folder was read
                    continue
                yield StorageObject(path, size)

    def __entries(self, folder: str) -> Iterator[Tuple[str, os.DirEntry]]:
        """
        The directory entries under ``folder`` that are not directories (files, and
        symbolic links whatever they point to), with their paths relative to the base,
        read lazily one directory at a time: unlike ``rglob``, which reads each directory
        whole, it stops reading once its caller has enough. Their types come with the
        entries, at no cost. Like ``rglob`` it skips a directory it may not read.
        """
        top = self.__resolve(folder)
        relative = top.relative_to(self.__base).as_posix()
        prefix = '' if relative == '.' else relative + '/'
        start = len(str(top)) + 1  # an entry's path is its directory's, a slash, its name
        dirs = [str(top)]

        while dirs:
            try:
                entries = os.scandir(dirs.pop())
            except (FileNotFoundError, NotADirectoryError, PermissionError):
                continue
            with entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        dirs.append(entry.path)
                    else:
                        yield prefix + entry.path[start:], entry

    def __resolve(self, path: str) -> Path:
        """
        ``path`` under the base. One that would leave the base, absolute or with a '..'
        name in it, raises ValueError.
        """
        relative = PurePosixPath(path)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError(f"The path '{path}' leaves the storage.")
        return self.__base / relative

    def __named(self, path: str) -> Optional[Path]:
        """
        What ``path`` names under the base, as the operations on paths take a path (see
        ``StorageManager``): None, nothing, for the empty path and for a path that ends
        with a slash but is not a directory.
        """
        relative = path.strip('/')
        if not relative:
            return None
        named = self.__resolve(relative)
        if path.endswith('/') and not named.is_dir():
            return None
        return named

    def path_exists(self, path: str) -> bool:
        named = self.__named(path)
        return named is not None and named.exists()

    def obj_exists(self, file_path: str) -> bool:
        return self.__resolve(file_path).is_file()

    def upload_obj(self, file_path: str, contents: AnyStr, content_type: Optional[str] = None):
        dst = self.__resolve(file_path)
        dst.parent.mkdir(exist_ok=True, parents=True)

        if self.__is_textual(content_type):
            dst.write_text(contents)
        else:
            dst.write_bytes(contents)

    @staticmethod
    def __is_textual(media_type: Optional[str]) -> bool:
        """
        :returns: True if given media type is a text-based media type.
        """
        return media_type is not None and media_type.split('/', maxsplit=1)[0] == 'text'

    def download_obj(self, file_path: str) -> bytes:
        return self.__resolve(file_path).read_bytes()

    def copy_obj(self, src: str, dst: str) -> None:
        """
        A hard link at ``dst`` to ``src``; one there already is the copy (another task
        made it), anything else at ``dst`` raises ``FileExistsError``.
        """
        src_path = self.__resolve(src)
        dst_path = self.__resolve(dst)
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            dst_path.hardlink_to(src_path)
        except FileExistsError:
            if not os.path.samestat(dst_path.lstat(), src_path.stat()):
                raise

    def delete_obj(self, file_path: str) -> None:
        self.__resolve(file_path).unlink(missing_ok=True)

    def copy_path(self, src: str, dst: str) -> None:
        src_path = self.__named(src)
        if src_path is None or not src_path.exists():
            return
        dst_path = self.__resolve(dst.strip('/'))

        if src_path.is_dir():
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src_path, dst_path)
        else:
            self.copy_obj(src.strip('/'), dst.strip('/'))

    def move_path(self, src: str, dst: str) -> None:
        src_path = self.__named(src)
        if src_path is None or not src_path.exists():
            return
        dst_path = self.__resolve(dst.strip('/'))

        if src_path.is_dir():
            if src_path.parent == dst_path.parent:  # just a renaming
                src_path.rename(dst_path)
            else:
                dst_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(src_path, dst_path)
        else:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            src_path.rename(dst_path)

    def delete_path(self, path: str) -> None:
        named = self.__named(path)
        if named is None:
            return  # not the whole storage, nor a file named as a folder

        if named.is_dir():
            shutil.rmtree(named, onexc=_raise_unless_missing)
        else:
            named.unlink(missing_ok=True)

    def sanitize_obj_names(self, path: str) -> Dict[str, str]:
        """
        Removes commas from the names of all files and folders under the specified
        input folder path.
        Handles special cases:
            - Files with names that only contain commas and white spaces are deleted.
            - Folders with names that only contain commas and white spaces are removed
            after moving their contents to the parent folder.

        Returns a dictionary that only contains modified file paths. Keys are the
        original file paths and values are the new file paths. Deleted files have
        the empty string as the value.
        """
        new_file_paths = {}
        p = self.__resolve(path)
        p_rel = Path(path)

        if p.is_dir():
            for item in sorted(p.rglob('*'), key=lambda i: len(i.parts), reverse=True):
                # Sorted by depth (deepest paths first) to handle files and subfolders
                # before their parent folders
                new_name = sanitized_name(item.name)

                if item.is_file():
                    item_rel_path = str(item.relative_to(self.__base))
                    new_rel_path = sanitized_path(str(item.relative_to(p)))

                    if not new_rel_path:
                        item.unlink()  # Delete the file
                        new_file_paths[item_rel_path] = ''
                    else:
                        if item.name != new_name:
                            new_path = item.with_name(new_name)
                            item.rename(new_path)  # Filesystem op

                        new_item_rel_path = str(p_rel / new_rel_path)
                        if new_item_rel_path != item_rel_path:  # Final file path changes
                            new_file_paths[item_rel_path] = new_item_rel_path

                elif item.is_dir():
                    if not new_name:
                        # Move contents to the parent folder
                        parent = item.parent
                        for subitem in item.iterdir():
                            shutil.move(str(subitem), str(parent))
                        item.rmdir()  # Remove the now-empty folder
                    else:
                        if item.name != new_name:
                            new_path = item.with_name(new_name)
                            item.rename(new_path)  # Filesystem op
        return new_file_paths
