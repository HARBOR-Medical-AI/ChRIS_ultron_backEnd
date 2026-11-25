"""
A module for interfacing with file storage backends.

File storage backends are "services" which store arbitrary data identified by path-like strings.
Examples include OpenStack Swift object storage, AWS S3, Azure Blob Storage (via S3-compatible
endpoint), MinIO, Ceph RGW, or of course, a literal UNIX-y filesystem.

ChRIS files are immutable, so file storage services can be optimized for WORM
(write-once, read-many) workloads.

Note to developers: historically, *ChRIS* was tightly-coupled to OpenStack Swift, hence
variable and function names use Swift terminology.
"""
from typing import Dict

from .storagemanager import StorageManager, StorageObject, in_tree, sanitized_path
from .swiftmanager import SwiftManager
from .s3manager import S3Manager
from .plain_fs import FilesystemManager
from .helpers import connect_storage, verify_storage_connection


__all__ = ['StorageManager', 'StorageObject', 'in_tree', 'sanitized_path', 'SwiftManager',
           'S3Manager', 'FilesystemManager', 'connect_storage', 'verify_storage_connection']
