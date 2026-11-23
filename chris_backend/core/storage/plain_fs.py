
from itertools import islice
import os
from pathlib import Path
import shutil
from typing import Union, List, Dict, AnyStr, Optional

from core.storage.storagemanager import StorageManager


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

    def ls(self, path_prefix: str, limit: Optional[int] = None) -> List[str]:
        if self.obj_exists(path_prefix):
            return [path_prefix]
        if limit is not None:
            return list(islice(self.__files_under(self.__base / path_prefix), limit))
        all_paths = (self.__base / path_prefix).rglob('*')
        return [str(p.relative_to(self.__base)) for p in all_paths if p.is_file()]

    def __files_under(self, top: Path):
        """
        The files under ``top`` that ``rglob`` finds (symbolic links to files, but not
        the trees of links to directories), relative to the base, read lazily one
        directory at a time: unlike ``rglob``, which reads each directory whole, it stops
        reading once its caller has enough.
        """
        dirs = [top]
        while dirs:
            try:
                entries = os.scandir(dirs.pop())
            except (FileNotFoundError, NotADirectoryError):
                continue
            with entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        dirs.append(entry.path)
                    elif entry.is_file():
                        yield str(Path(entry.path).relative_to(self.__base))

    def path_exists(self, path: str) -> bool:
        return (self.__base / path).exists()

    def obj_exists(self, file_path: str) -> bool:
        return (self.__base / file_path).is_file()

    def upload_obj(self, file_path: str, contents: AnyStr, content_type: Optional[str] = None):
        dst = (self.__base / file_path)
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

    def download_obj(self, file_path: str) -> AnyStr:
        return (self.__base / file_path).read_bytes()

    def copy_obj(self, src: str, dst: str) -> None:
        src_path = self.__base / src
        dst_path = self.__base / dst
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        dst_path.hardlink_to(src_path)

    def delete_obj(self, file_path: str) -> None:
        (self.__base / file_path).unlink()

    def copy_path(self, src: str, dst: str) -> None:
        src_path = self.__base / src
        dst_path = self.__base / dst

        if not src_path.exists():
            raise FileNotFoundError(
                f"The source path '{src_path}' does not exist.")

        if src_path.is_dir():
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src_path, dst_path)
        else:
            self.copy_obj(src, dst)

    def move_path(self, src: str, dst: str) -> None:
        src_path = self.__base / src
        dst_path = self.__base / dst

        if not src_path.exists():
            raise FileNotFoundError(
                f"The source path '{src_path}' does not exist.")

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
        p = self.__base / path

        if not p.exists():
            raise FileNotFoundError(
                f"The source path '{p}' does not exist.")

        if p.is_dir():
            shutil.rmtree(p)
        else:
            self.delete_obj(path)


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
        p = self.__base / path
        p_rel = Path(path)

        if p.is_dir():
            for item in sorted(p.rglob('*'), key=lambda i: len(i.parts), reverse=True):
                # Sorted by depth (deepest paths first) to handle files and subfolders
                # before their parent folders
                new_name = item.name.replace(',', '')

                if item.is_file():
                    item_rel_path = item.relative_to(self.__base)

                    if new_name.strip() == '':
                        item.unlink()  # Delete the file
                        new_file_paths[str(item_rel_path)] = ''
                    else:
                        if item.name != new_name:
                            new_path = item.with_name(new_name)
                            item.rename(new_path)  # Filesystem op

                        new_parts = []
                        for part in item.relative_to(p).parts:
                            new_part = part.replace(',', '')
                            if new_part.strip() != '':
                                new_parts.append(new_part)

                        new_item_rel_path = p_rel / Path(*new_parts)
                        if new_item_rel_path != item_rel_path:  # Final file path changes
                            new_file_paths[str(item_rel_path)] = str(new_item_rel_path)

                elif item.is_dir():
                    if new_name.strip() == '':
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
