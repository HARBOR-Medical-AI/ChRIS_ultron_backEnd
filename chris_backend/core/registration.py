"""
Registration of a tree of storage objects as rows: its folders (``ChrisFolder``) and its
files (``ChrisFile`` or a proxy of it), inserted in batches whatever the size of the tree.

``FolderIndex`` maps the folders of one tree to their ids and creates the missing ones
level by level; ``FileRegistrar`` takes the files one at a time, as a storage walk yields
them, and inserts them a batch at a time, each batch committed on its own; ``BatchSizer``
sizes the batches by the time the last insert took. They know nothing of plugin
instances: the registration of a plugin instance's output uses them, and so can any
other registration of a tree of objects.
"""

import time
from collections import defaultdict
from typing import Callable, Iterable, Optional

from django.db import IntegrityError, transaction
from django.db.models import Q

from core.models import ChrisFile, ChrisFolder


class BatchSizer:
    """
    How many rows the next insert takes (``size``): as many as one statement inserts in
    ``target`` seconds at the rate of the last insert of ``least`` rows or more
    (``took``), between ``least`` and ``most`` rows, and at most twice the last size. The
    time of a smaller insert (a batch's few new folders) is mostly round trips to the
    database, which says little about the rate of a large one.

    Why by time: PostgreSQL logs every statement slower than ``log_min_duration_statement``
    (1 s in ``postgres/cube.conf``), and Django binds a bulk insert's values in the client,
    so a slow insert lands in the log with every path in it. Inserts run at very different
    rates (37 µs a row into a small table, 200 µs into a large one under load), so a
    fixed number of rows is either too many for one or too few for the other.
    """

    def __init__(self, first: int = 1000, *, target: float = 0.25, least: int = 250,
                 most: int = 5000):
        self.size = first
        self.target, self.least, self.most = target, least, most

    def took(self, rows: int, seconds: float) -> None:
        """
        Size the next batch after an insert of ``rows`` rows that took ``seconds``.
        """
        if rows < self.least:
            return
        rate = rows / max(seconds, 1e-6)
        self.size = int(max(self.least, min(self.most, 2 * self.size, rate * self.target)))


def insert_rows(model, rows: list, sizer: BatchSizer) -> None:
    """
    Insert ``rows`` of ``model`` in one statement, timed for ``sizer``. Rows that are
    there already (another run inserted them) are left as they are: the plain INSERT
    fails on them (once the other run's transaction ends, if it has not) and is made
    again skipping them. The plain INSERT comes first because one that skips conflicting
    rows checks every row before writing it, which makes the insert of a large batch
    about three-quarters longer.
    """
    started = time.monotonic()
    try:
        with transaction.atomic():
            model.objects.bulk_create(rows)
    except IntegrityError:
        model.objects.bulk_create(rows, ignore_conflicts=True)
    sizer.took(len(rows), time.monotonic() - started)


def parent_path(path: str) -> str:
    """
    The path of the folder of the file or folder at ``path``.
    """
    return path.rpartition('/')[0]


class FolderIndex:
    """
    The ids of the folders of the tree at ``root`` by path, read with one query. Its
    ``ensure`` creates the folders that are missing, owned by ``owner``, a level of the
    tree at a time in statements that ``sizer`` sizes, whereas ``ChrisFolder.save``
    creates the missing ancestors of one folder one at a time.
    """

    def __init__(self, root: str, owner, sizer: Optional[BatchSizer] = None):
        self.root, self.owner = root, owner
        self.sizer = sizer if sizer is not None else BatchSizer()
        self._prefix = root + '/'
        self._ids = dict(ChrisFolder.objects.filter(
            Q(path=root) | Q(path__startswith=self._prefix)).order_by().values_list(
            'path', 'id'))

        if root not in self._ids:  # creates its missing ancestors too
            (folder, _) = ChrisFolder.objects.get_or_create(path=root,
                                                            defaults={'owner': owner})
            self._ids[root] = folder.id

    def __len__(self) -> int:
        return len(self._ids)

    def id(self, path: str) -> int:
        """
        The id of the folder at ``path``, which the index has (``ensure``).
        """
        return self._ids[path]

    def ensure(self, paths: Iterable[str]) -> None:
        """
        Create the folders at ``paths`` that are missing and their missing ancestors,
        level by level from the root, each level's in statements of their own, sorted by
        path. Folders another run created meanwhile are taken as they are.
        """
        missing = set()

        for path in paths:
            while path not in self._ids and path not in missing:
                if not path.startswith(self._prefix):
                    raise ValueError(f'{path} is not under {self.root}')
                missing.add(path)
                path = parent_path(path)

        levels = defaultdict(list)
        for path in missing:
            levels[path.count('/')].append(path)

        for depth in sorted(levels):
            level = sorted(levels[depth])
            start = 0
            while start < len(level):  # the sizer sizes each statement after the last
                end = start + self.sizer.size
                self._create(level[start:end])
                start = end

    def _create(self, paths: list) -> None:
        rows = [ChrisFolder(path=path, owner=self.owner,
                            parent_id=self._ids[parent_path(path)]) for path in paths]
        insert_rows(ChrisFolder, rows, self.sizer)

        if all(row.id is not None for row in rows):
            self._ids.update((row.path, row.id) for row in rows)
        else:  # some were there already: the rows skipped have no id
            self._ids.update(ChrisFolder.objects.filter(path__in=paths).order_by()
                             .values_list('path', 'id'))


class Stopped(Exception):
    """
    Raised to stop a registration that must write nothing more: by ``FileRegistrar``
    when its ``should_stop`` says so before a batch.
    """


class FileRegistrar:
    """
    Registers files of the tree of ``folders`` as rows of ``model`` (``ChrisFile`` or a
    proxy of it) owned by ``owner``: given one at a time with ``add``, as a storage walk
    yields them, and inserted a batch at a time, the last one by ``close``. A batch is
    ``folders.sizer``'s size unless another ``sizer`` is given, and each is one statement
    committed on its own, its files sorted by path (two registrations of one tree at once
    lock their rows in the same order), after its folders (``FolderIndex.ensure``).

    ``should_stop`` is asked before each batch, which raises ``Stopped`` instead when it
    answers true, so the registration writes nothing more. With ``resume`` each batch
    leaves out the files that have a row already, which an earlier registration of the
    tree that did not finish inserted.

    ``files`` and ``bytes`` count the files of the batches written, registered by this
    registration or an earlier one.
    """

    def __init__(self, model, owner, folders: FolderIndex, *, resume: bool = False,
                 should_stop: Optional[Callable[[], bool]] = None,
                 sizer: Optional[BatchSizer] = None):
        self.model, self.owner, self.folders = model, owner, folders
        self.resume, self.should_stop = resume, should_stop
        self.sizer = sizer if sizer is not None else folders.sizer
        self.files = self.bytes = 0
        self._batch: list = []

    def add(self, path: str, size: int) -> None:
        """
        Register the file at ``path`` of ``size`` bytes, with the batch it fills.
        """
        self._batch.append((path, size))

        if len(self._batch) >= self.sizer.size:
            self.flush()

    def close(self) -> None:
        """
        Register the files of the batch still open.
        """
        self.flush()

    def flush(self) -> None:
        """
        Register the files of the batch so far, if any.
        """
        if not self._batch:
            return

        batch, self._batch = sorted(self._batch), []
        if self.should_stop is not None and self.should_stop():
            raise Stopped()

        self.folders.ensure({parent_path(path) for path, _ in batch})
        paths = [path for path, _ in batch]

        if self.resume:
            registered = set(ChrisFile.objects.filter(fname__in=paths).order_by()
                             .values_list('fname', flat=True))
            paths = [path for path in paths if path not in registered]

        if paths:
            rows = [self.model(fname=path, owner=self.owner,
                               parent_folder_id=self.folders.id(parent_path(path)))
                    for path in paths]
            insert_rows(self.model, rows, self.sizer)

        self.files += len(batch)
        self.bytes += sum(size for _, size in batch)
