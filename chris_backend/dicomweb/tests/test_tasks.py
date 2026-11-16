"""
Tests for ``dicomweb.tasks``.

Two groups:
  - Helper-function unit tests (no DB, no storage).
  - Task body tests: real DB objects + mocked storage, exercising
    ``_find_series_for_file``, ``_backfill_series_tags``, and the full
    ``index_pacs_instance`` execution path.
"""

import io
import logging
import types
from datetime import date, time
from unittest.mock import MagicMock, patch

import pydicom
from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.models import ChrisFolder
from pacsfiles.models import PACS, PACSFile, PACSSeries

from dicomweb.models import PACSInstance, PACSStudy
from dicomweb.tasks import (
    _as_int,
    _backfill_series_tags,
    _backfill_study_tags,
    _find_series_for_file,
    _parse_dicom_date,
    _parse_dicom_time,
    _transfer_syntax_uid,
    index_pacs_instance,
)


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

def _make_dicom_bytes(**kwargs) -> bytes:
    """Return bytes of a minimal valid DICOM P10 file with compliant UIDs."""
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    sop_uid = kwargs.get('SOPInstanceUID', generate_uid())
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = '1.2.840.10008.5.1.4.1.1.2'
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = Dataset()
    ds.file_meta = file_meta

    ds.SOPClassUID = '1.2.840.10008.5.1.4.1.1.2'
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = kwargs.get('StudyInstanceUID', '1.2.3.4.1')
    ds.SeriesInstanceUID = kwargs.get('SeriesInstanceUID', '1.2.3.4.2')
    ds.PatientID = 'TESTPAT'
    ds.PatientName = 'Test^Patient'
    ds.StudyDate = '20230101'
    ds.StudyTime = '120000'
    ds.SeriesNumber = 1
    ds.Modality = 'CT'
    ds.Manufacturer = 'TestMfr'
    ds.BodyPartExamined = 'HEAD'
    ds.PerformedProcedureStepStartDate = '20230101'
    ds.PerformedProcedureStepStartTime = '120000'
    ds.InstanceNumber = kwargs.get('InstanceNumber', 1)
    ds.Rows = 4
    ds.Columns = 4
    ds.BitsAllocated = 16
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = 'MONOCHROME2'
    ds.PixelData = b'\x00' * 32

    buf = io.BytesIO()
    pydicom.dcmwrite(buf, ds)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Helper-parse tests (no DB)
# ---------------------------------------------------------------------------

class HelperParseTests(TestCase):

    def test_parse_dicom_date(self):
        cases = [
            ('20231201', date(2023, 12, 1)),
            ('', None),
            (None, None),
            ('not-a-date', None),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(_parse_dicom_date(value), expected)

    def test_parse_dicom_time(self):
        cases = [
            ('143005',     time(14, 30, 5)),   # full HHMMSS
            ('143005.123', time(14, 30, 5)),   # fractional seconds stripped
            ('1430',       time(14, 30, 0)),   # truncated HHMM
            ('14',         time(14,  0, 0)),   # truncated HH
            ('9999',       None),              # valid length, invalid value
            ('',           None),
            (None,         None),
            ('not-a-time', None),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(_parse_dicom_time(value), expected)

    def test_as_int(self):
        cases = [
            (42,    42),
            ('17',  17),
            ('',    None),
            (None,  None),
            ('xyz', None),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(_as_int(value), expected)


class TransferSyntaxUIDTests(TestCase):

    def test_returns_uid_when_file_meta_present(self):
        from pydicom.uid import ExplicitVRLittleEndian
        ds = pydicom.dcmread(io.BytesIO(_make_dicom_bytes()), stop_before_pixels=True, force=True)
        self.assertEqual(_transfer_syntax_uid(ds), str(ExplicitVRLittleEndian))

    def test_returns_empty_string_on_attribute_error(self):
        class _NoFileMeta:
            @property
            def file_meta(self):
                raise AttributeError
        self.assertEqual(_transfer_syntax_uid(_NoFileMeta()), '')

    def test_returns_empty_string_when_file_meta_is_none(self):
        ds = pydicom.dataset.Dataset()
        self.assertEqual(_transfer_syntax_uid(ds), '')


# ---------------------------------------------------------------------------
# Import / routing smoke tests (no DB)
# ---------------------------------------------------------------------------

class TaskImportSmokeTests(TestCase):
    def test_celery_task_is_importable(self):
        from dicomweb.tasks import index_pacs_instance as t
        self.assertTrue(callable(t))
        self.assertEqual(t.name, 'dicomweb.tasks.index_pacs_instance')

    def test_task_routed_to_the_pacs_queue(self):
        # the routing table itself: the test runner switches the app's routes off
        from core.celery import task_routes
        entry = task_routes.get('dicomweb.tasks.index_pacs_instance')
        self.assertIsNotNone(entry)
        self.assertEqual(entry.get('queue'), 'pacs')


# ---------------------------------------------------------------------------
# Shared DB fixture
# ---------------------------------------------------------------------------

class DicomwebTaskTestBase(TestCase):
    """
    Creates a minimal PACS → ChrisFolder → PACSSeries → PACSFile fixture.
    Sub-folders are used in some tests to verify the folder-walk logic.
    """

    PACS_NAME = 'TASKPACS'
    SERIES_PATH = 'SERVICES/PACS/TASKPACS/P001/1.2.3.study/1.2.3.series'

    def setUp(self):
        logging.disable(logging.CRITICAL)  # suppress expected task error/warning logs
        owner = User.objects.get(username='chris')
        pacs_grp, _ = Group.objects.get_or_create(name='pacs_users')

        pacs_folder, _ = ChrisFolder.objects.get_or_create(
            path=f'SERVICES/PACS/{self.PACS_NAME}', owner=owner)
        self.pacs = PACS(folder=pacs_folder, identifier=self.PACS_NAME)
        self.pacs.save()

        self.series_folder, _ = ChrisFolder.objects.get_or_create(
            path=self.SERIES_PATH, owner=owner)

        self.series, _ = PACSSeries.objects.get_or_create(
            PatientID='P001',
            PatientName='Test^Patient',
            PatientSex='O',
            StudyDate='2023-01-01',
            StudyInstanceUID='1.2.3.study',
            SeriesInstanceUID='1.2.3.series',
            pacs=self.pacs,
            folder=self.series_folder,
        )

        # A PACSFile whose parent_folder IS the series folder (direct child)
        self.pacs_file = PACSFile(owner=owner, parent_folder=self.series_folder)
        self.pacs_file.fname.name = self.SERIES_PATH + '/0001.dcm'
        self.pacs_file.save()

        self.owner = owner

    def tearDown(self):
        logging.disable(logging.NOTSET)


# ---------------------------------------------------------------------------
# _find_series_for_file tests
# ---------------------------------------------------------------------------

class FindSeriesForFileTests(DicomwebTaskTestBase):

    def test_direct_parent_is_series_folder(self):
        # PACSFile is directly under the series folder
        result = _find_series_for_file(self.pacs_file)
        self.assertEqual(result, self.series)

    def test_file_in_sub_folder_walks_up(self):
        # Some ingest paths create a sub-folder under the series folder
        sub_folder, _ = ChrisFolder.objects.get_or_create(
            path=self.SERIES_PATH + '/sub', owner=self.owner)
        sub_folder.parent = self.series_folder
        sub_folder.save()

        nested_file = PACSFile(owner=self.owner, parent_folder=sub_folder)
        nested_file.fname.name = self.SERIES_PATH + '/sub/0002.dcm'
        nested_file.save()

        result = _find_series_for_file(nested_file)
        self.assertEqual(result, self.series)

    def test_file_with_no_series_ancestor_returns_none(self):
        # A folder that has no PACSSeries attached anywhere up the chain
        orphan_folder, _ = ChrisFolder.objects.get_or_create(
            path='SERVICES/PACS/TASKPACS/orphan', owner=self.owner)

        orphan_file = PACSFile(owner=self.owner, parent_folder=orphan_folder)
        orphan_file.fname.name = 'SERVICES/PACS/TASKPACS/orphan/x.dcm'
        orphan_file.save()

        result = _find_series_for_file(orphan_file)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# _backfill_series_tags tests
# ---------------------------------------------------------------------------

class BackfillSeriesTagsTests(DicomwebTaskTestBase):

    def _make_ds(self, **attrs):
        # Use SimpleNamespace to avoid pydicom VR validation warnings when
        # setting test values that intentionally exceed field length limits.
        return types.SimpleNamespace(**attrs)

    def test_fills_all_empty_fields(self):
        ds = self._make_ds(
            StudyTime='143000',
            SeriesNumber=5,
            Manufacturer='Siemens',
            BodyPartExamined='BRAIN',
            PerformedProcedureStepStartDate='20230101',
            PerformedProcedureStepStartTime='143000',
        )
        _backfill_series_tags(self.series, ds)
        self.series.refresh_from_db()

        self.assertEqual(self.series.StudyTime, time(14, 30, 0))
        self.assertEqual(self.series.SeriesNumber, 5)
        self.assertEqual(self.series.Manufacturer, 'Siemens')
        self.assertEqual(self.series.BodyPartExamined, 'BRAIN')
        self.assertEqual(self.series.PerformedProcedureStepStartDate, date(2023, 1, 1))
        self.assertEqual(self.series.PerformedProcedureStepStartTime, time(14, 30, 0))

    def test_does_not_overwrite_existing_values(self):
        # Pre-populate some fields
        PACSSeries.objects.filter(pk=self.series.pk).update(
            SeriesNumber=99,
            Manufacturer='Original',
        )
        self.series.refresh_from_db()

        ds = self._make_ds(SeriesNumber=1, Manufacturer='NewMfr')
        _backfill_series_tags(self.series, ds)
        self.series.refresh_from_db()

        # Should remain unchanged
        self.assertEqual(self.series.SeriesNumber, 99)
        self.assertEqual(self.series.Manufacturer, 'Original')

    def test_no_update_when_ds_has_no_relevant_tags(self):
        ds = self._make_ds()  # empty dataset
        _backfill_series_tags(self.series, ds)
        self.series.refresh_from_db()

        self.assertIsNone(self.series.StudyTime)
        self.assertIsNone(self.series.SeriesNumber)
        self.assertEqual(self.series.Manufacturer, '')

    def test_manufacturer_truncated_to_64_chars(self):
        ds = self._make_ds(Manufacturer='X' * 100)
        _backfill_series_tags(self.series, ds)
        self.series.refresh_from_db()
        self.assertEqual(len(self.series.Manufacturer), 64)

    def test_body_part_truncated_to_16_chars(self):
        ds = self._make_ds(BodyPartExamined='Y' * 30)
        _backfill_series_tags(self.series, ds)
        self.series.refresh_from_db()
        self.assertEqual(len(self.series.BodyPartExamined), 16)


# ---------------------------------------------------------------------------
# _backfill_study_tags tests
# ---------------------------------------------------------------------------

class BackfillStudyTagsTests(DicomwebTaskTestBase):

    def _make_ds(self, **attrs):
        return types.SimpleNamespace(**attrs)

    def _make_study(self, **kwargs):
        kwargs.setdefault('StudyInstanceUID', '1.2.3.study')
        return PACSStudy.objects.create(pacs=self.pacs, **kwargs)

    def test_fills_empty_studytime_from_header(self):
        study = self._make_study()  # StudyTime is NULL
        _backfill_study_tags(study, self._make_ds(StudyTime='143000'))
        study.refresh_from_db()
        self.assertEqual(study.StudyTime, time(14, 30, 0))

    def test_does_not_overwrite_existing_studytime(self):
        study = self._make_study(StudyTime=time(9, 0, 0))
        _backfill_study_tags(study, self._make_ds(StudyTime='143000'))
        study.refresh_from_db()
        self.assertEqual(study.StudyTime, time(9, 0, 0))

    def test_no_update_when_ds_has_no_studytime(self):
        study = self._make_study()
        _backfill_study_tags(study, self._make_ds())
        study.refresh_from_db()
        self.assertIsNone(study.StudyTime)


# ---------------------------------------------------------------------------
# index_pacs_instance task body tests (mocked storage)
# ---------------------------------------------------------------------------

class IndexPacsInstanceTaskTests(DicomwebTaskTestBase):

    def _mock_storage(self, dicom_bytes):
        mock_mgr = MagicMock()
        mock_mgr.download_obj.return_value = dicom_bytes
        return mock_mgr

    def test_creates_pacs_instance_from_valid_dicom(self):
        dicom_bytes = _make_dicom_bytes(
            SOPInstanceUID='1.2.3.4.5001',
            InstanceNumber=1,
        )
        with patch('dicomweb.tasks.connect_storage',
                   return_value=self._mock_storage(dicom_bytes)):
            index_pacs_instance.apply(args=[self.pacs_file.pk])

        self.assertEqual(
            PACSInstance.objects.filter(
                series=self.series,
                SOPInstanceUID='1.2.3.4.5001',
            ).count(),
            1,
        )

    def test_created_instance_has_correct_geometry(self):
        dicom_bytes = _make_dicom_bytes(
            SOPInstanceUID='1.2.3.4.5002',
            InstanceNumber=7,
        )
        with patch('dicomweb.tasks.connect_storage',
                   return_value=self._mock_storage(dicom_bytes)):
            index_pacs_instance.apply(args=[self.pacs_file.pk])

        inst = PACSInstance.objects.get(SOPInstanceUID='1.2.3.4.5002')
        self.assertEqual(inst.Rows, 4)
        self.assertEqual(inst.Columns, 4)
        self.assertEqual(inst.BitsAllocated, 16)
        self.assertEqual(inst.InstanceNumber, 7)
        self.assertEqual(inst.SOPClassUID, '1.2.840.10008.5.1.4.1.1.2')

    def test_task_is_idempotent(self):
        dicom_bytes = _make_dicom_bytes(SOPInstanceUID='1.2.3.4.5003')
        mock_mgr = self._mock_storage(dicom_bytes)
        with patch('dicomweb.tasks.connect_storage', return_value=mock_mgr):
            index_pacs_instance.apply(args=[self.pacs_file.pk])
            index_pacs_instance.apply(args=[self.pacs_file.pk])

        self.assertEqual(
            PACSInstance.objects.filter(SOPInstanceUID='1.2.3.4.5003').count(),
            1,
        )

    def test_backfill_runs_during_task(self):
        dicom_bytes = _make_dicom_bytes(SOPInstanceUID='1.2.3.4.5004')
        with patch('dicomweb.tasks.connect_storage',
                   return_value=self._mock_storage(dicom_bytes)):
            index_pacs_instance.apply(args=[self.pacs_file.pk])

        self.series.refresh_from_db()
        self.assertEqual(self.series.SeriesNumber, 1)
        self.assertEqual(self.series.Manufacturer, 'TestMfr')

    def test_non_dcm_file_is_skipped(self):
        self.pacs_file.fname.name = self.SERIES_PATH + '/manifest.json'
        self.pacs_file.save()

        index_pacs_instance.apply(args=[self.pacs_file.pk])

        self.assertEqual(PACSInstance.objects.filter(series=self.series).count(), 0)

    def test_missing_pacs_file_id_returns_gracefully(self):
        # Should log a warning and return without raising
        index_pacs_instance.apply(args=[999999])
        self.assertEqual(PACSInstance.objects.count(), 0)

    def test_no_series_for_file_returns_gracefully(self):
        orphan_folder, _ = ChrisFolder.objects.get_or_create(
            path='SERVICES/PACS/TASKPACS/orphan2', owner=self.owner)
        orphan_file = PACSFile(owner=self.owner, parent_folder=orphan_folder)
        orphan_file.fname.name = 'SERVICES/PACS/TASKPACS/orphan2/x.dcm'
        orphan_file.save()

        dicom_bytes = _make_dicom_bytes(SOPInstanceUID='1.2.3.4.5005')
        with patch('dicomweb.tasks.connect_storage',
                   return_value=self._mock_storage(dicom_bytes)):
            index_pacs_instance.apply(args=[orphan_file.pk])

        self.assertEqual(PACSInstance.objects.count(), 0)

    def test_storage_error_raises_for_retry(self):
        mock_mgr = MagicMock()
        mock_mgr.download_obj.side_effect = OSError('storage unavailable')
        with patch('dicomweb.tasks.connect_storage', return_value=mock_mgr):
            result = index_pacs_instance.apply(args=[self.pacs_file.pk])
        # Task raises Retry internally; apply() captures it as a failure
        self.assertTrue(result.failed())

    def test_pydicom_exception_returns_gracefully(self):
        # pydicom.dcmread raises (e.g. memory error, bad decompression) —
        # exercises the except Exception branch at lines 115-117.
        mock_mgr = self._mock_storage(b'irrelevant')
        with patch('dicomweb.tasks.connect_storage', return_value=mock_mgr), \
             patch('dicomweb.tasks.pydicom.dcmread', side_effect=RuntimeError('boom')):
            index_pacs_instance.apply(args=[self.pacs_file.pk])
        self.assertEqual(PACSInstance.objects.filter(series=self.series).count(), 0)

    def test_missing_sop_instance_uid_returns_gracefully(self):
        # DICOM with no SOPInstanceUID — exercises the `if not sop_instance_uid` branch.
        dicom_bytes = _make_dicom_bytes(SOPInstanceUID='1.2.3.4.5006')
        mock_mgr = self._mock_storage(dicom_bytes)
        mock_ds = MagicMock()
        mock_ds.SOPInstanceUID = None
        mock_ds.file_meta = None
        with patch('dicomweb.tasks.connect_storage', return_value=mock_mgr), \
             patch('dicomweb.tasks.pydicom.dcmread', return_value=mock_ds):
            index_pacs_instance.apply(args=[self.pacs_file.pk])
        self.assertEqual(PACSInstance.objects.filter(series=self.series).count(), 0)

    def test_file_meta_attribute_error_handled(self):
        # ds.file_meta raises AttributeError — exercises the except AttributeError
        # branch. MagicMock.__getattr__ intercepts PropertyMock descriptors, so we
        # use a plain class whose file_meta property raises the error reliably.
        class _NoFileMeta:
            # All attributes the task accesses via getattr(ds, ..., default) are
            # set here; file_meta is omitted entirely so Python raises AttributeError
            # naturally without __getattr__ intercepting it.
            SOPInstanceUID = '1.2.3.4.5007'
            SOPClassUID = '1.2.840.10008.5.1.4.1.1.2'
            InstanceNumber = 1
            Rows = 4
            Columns = 4
            BitsAllocated = 16
            NumberOfFrames = 1
            Manufacturer = None
            BodyPartExamined = None
            StudyTime = None
            SeriesNumber = None
            PerformedProcedureStepStartDate = None
            PerformedProcedureStepStartTime = None

        mock_mgr = self._mock_storage(b'irrelevant')
        with patch('dicomweb.tasks.connect_storage', return_value=mock_mgr), \
             patch('dicomweb.tasks.pydicom.dcmread', return_value=_NoFileMeta()):
            index_pacs_instance.apply(args=[self.pacs_file.pk])
        self.assertEqual(
            PACSInstance.objects.filter(SOPInstanceUID='1.2.3.4.5007').count(), 1
        )

    def test_backfill_study_runs_when_series_linked_to_study(self):
        # When the series is linked to a PACSStudy, the task also backfills the
        # study's study-level tags (StudyTime) from the header — the call site
        # that is skipped when series.study is None. The task backfills the same
        # tag onto both series and study, so assert they agree afterwards rather
        # than hard-coding the fixture's StudyTime.
        study = PACSStudy.objects.create(
            pacs=self.pacs, StudyInstanceUID='1.2.3.study')
        PACSSeries.objects.filter(pk=self.series.pk).update(study=study)

        dicom_bytes = _make_dicom_bytes(SOPInstanceUID='1.2.3.4.5008')
        with patch('dicomweb.tasks.connect_storage',
                   return_value=self._mock_storage(dicom_bytes)):
            index_pacs_instance.apply(args=[self.pacs_file.pk])

        self.series.refresh_from_db()
        study.refresh_from_db()
        # guard: the fixture must carry a StudyTime for this test to exercise the path
        self.assertIsNotNone(self.series.StudyTime)
        self.assertEqual(study.StudyTime, self.series.StudyTime)
