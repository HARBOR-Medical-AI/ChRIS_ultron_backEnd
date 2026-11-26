
import logging
import io

from django.contrib.auth.models import User
from django.test import TestCase, tag
from django.conf import settings
from unittest import mock
from rest_framework import serializers

from core.models import ChrisFolder
from core.storage import connect_storage
from pacsfiles.models import PACS, PACSQuery
from pacsfiles.serializers import (PACSQuerySerializer, PACSRetrieveSerializer,
                                   PACSSeriesSerializer)
from dicomweb.models import PACSStudy


CHRIS_SUPERUSER_PASSWORD = settings.CHRIS_SUPERUSER_PASSWORD


class SerializerTests(TestCase):
    def setUp(self):
        # avoid cluttered console output (for instance logging all the http requests)
        logging.disable(logging.WARNING)

        # superuser chris (owner of root folders)
        self.chris_username = 'chris'
        chris_user = User.objects.get(username=self.chris_username)

        # create normal user
        self.username = 'foo'
        self.password = 'bar'
        User.objects.create_user(username=self.username, password=self.password)

        # create a PACS
        self.pacs_name = 'myPACS'
        folder_path = f'SERVICES/PACS/{self.pacs_name}'
        (pacs_folder, tf) = ChrisFolder.objects.get_or_create(path=folder_path,
                                                              owner=chris_user)
        PACS.objects.get_or_create(folder=pacs_folder, identifier=self.pacs_name)

    def tearDown(self):
        # re-enable logging
        logging.disable(logging.NOTSET)


class PACSQuerySerializerTests(SerializerTests):

    def test_create_success(self):
        """
        Test whether overriden 'create' method successfully creates a new PACS query.
        """
        user = User.objects.get(username=self.username)
        pacs = PACS.objects.get(identifier=self.pacs_name)
        query = {'SeriesInstanceUID': '2.3.15.2.1057'}
        data = {'title': 'query1', 'query': query, 'owner': user, 'pacs': pacs}

        pacs_query_serializer = PACSQuerySerializer(data=data)
        pacs_query = pacs_query_serializer.create(data)
        self.assertEqual(pacs_query.status, 'created')


    def test_create_failure_pacs_user_title_combination_already_exists(self):
        """
        Test whether overriden 'create' method raises a ValidationError when a user has
        already registered a PACS query with the same title and pacs.
        """
        user = User.objects.get(username=self.username)
        pacs = PACS.objects.get(identifier=self.pacs_name)
        query = {'SeriesInstanceUID': '1.3.12.2.1107'}
        data = {'title': 'query2', 'query': query, 'owner': user, 'pacs': pacs}

        PACSQuery.objects.get_or_create(title='query2', query=query, owner=user, pacs=pacs)

        pacs_query_serializer = PACSQuerySerializer(data=data)
        with self.assertRaises(serializers.ValidationError):
            pacs_query_serializer.create(data)

    def test_update_success(self):
        """
        Test whether overriden 'update' method successfully updates an existing PACS query.
        """
        user = User.objects.get(username=self.username)
        pacs = PACS.objects.get(identifier=self.pacs_name)
        query = {'SeriesInstanceUID': '2.3.15.2.1057'}

        pacs_query, _ = PACSQuery.objects.get_or_create(title='query2', query=query,
                                                        owner=user, pacs=pacs)

        data = {'title': 'query4'}
        pacs_query_serializer = PACSQuerySerializer(pacs_query, data)
        pacs_query = pacs_query_serializer.update(pacs_query, data)
        self.assertEqual(pacs_query.title, 'query4')

    def test_update_failure_pacs_user_title_combination_already_exists(self):
        """
        Test whether overriden 'update' method raises a ValidationError when a user has
        already registered a PACS query with the same title and pacs.
        """
        user = User.objects.get(username=self.username)
        pacs = PACS.objects.get(identifier=self.pacs_name)
        query = {'SeriesInstanceUID': '1.3.12.2.1107'}

        pacs_query, _ = PACSQuery.objects.get_or_create(title='query2', query=query,
                                                        owner=user, pacs=pacs)
        PACSQuery.objects.get_or_create(title='query3', query=query, owner=user, pacs=pacs)

        data = {'title': 'query3'}
        pacs_query_serializer = PACSQuerySerializer(pacs_query, data)
        with self.assertRaises(serializers.ValidationError):
            pacs_query_serializer.update(pacs_query, data)

    def test_validate_execute(self):
        """
        Test whether overriden validate_execute method raises a serializers.ValidationError
        when the execute field changes from true to false.
        """
        user = User.objects.get(username=self.username)
        pacs = PACS.objects.get(identifier=self.pacs_name)
        query = {'SeriesInstanceUID': '1.3.12.2.1107'}

        pacs_query, _ = PACSQuery.objects.get_or_create(title='query5', query=query,
                                                        owner=user, pacs=pacs)
        pacs_query_serializer = PACSQuerySerializer(pacs_query)

        with self.assertRaises(serializers.ValidationError):
            pacs_query_serializer.validate_execute(False)


    def test_validate_validates_required_query_field_on_create(self):
        """
        Test whether overriden validate method validates that the query field must
        be provided when creating a new PACS query.
        """
        user = User.objects.get(username=self.username)
        pacs = PACS.objects.get(identifier=self.pacs_name)

        data = {'title': 'query6', 'owner': user, 'pacs': pacs}
        pacs_query_serializer = PACSQuerySerializer(data=data)

        with self.assertRaises(serializers.ValidationError):
            pacs_query_serializer.validate(data)


class PACSSeriesSerializerTests(SerializerTests):

    def test_ensure_study_creates_and_reuses_one_row_per_study(self):
        """
        _ensure_study creates exactly one PACSStudy per (pacs, StudyInstanceUID),
        denormalizes the study tags from the first series, and reuses the row for
        subsequent series of the same study.
        """
        pacs = PACS.objects.get(identifier=self.pacs_name)
        data = {'StudyInstanceUID': '1.study.A', 'PatientID': 'P9',
                'PatientName': 'Doe^Jane', 'StudyDate': '2026-02-02',
                'AccessionNumber': 'ACC1', 'StudyDescription': 'head'}

        study = PACSSeriesSerializer._ensure_study(pacs, data)
        self.assertEqual(PACSStudy.objects.filter(pacs=pacs).count(), 1)
        self.assertEqual(study.PatientName, 'Doe^Jane')
        self.assertEqual(study.AccessionNumber, 'ACC1')
        self.assertEqual(study.StudyDescription, 'head')

        # a second series of the same study reuses the row (no new PACSStudy)
        again = PACSSeriesSerializer._ensure_study(
            pacs, dict(data, SeriesInstanceUID='whatever'))
        self.assertEqual(again.pk, study.pk)
        self.assertEqual(PACSStudy.objects.filter(pacs=pacs).count(), 1)

    def test_ensure_study_warns_and_keeps_name_on_patient_name_drift(self):
        """
        A later series reporting a different, non-empty PatientName logs a
        WARNING and the stored value is kept (never overwritten).
        """
        pacs = PACS.objects.get(identifier=self.pacs_name)
        base = {'StudyInstanceUID': '1.study.B', 'PatientID': 'P9',
                'PatientName': 'Smith^John', 'StudyDate': '2026-02-02'}
        PACSSeriesSerializer._ensure_study(pacs, base)

        drift = dict(base, PatientName='Smith')
        logging.disable(logging.NOTSET)  # base setUp disables WARNING logs
        with self.assertLogs('pacsfiles.serializers', level='WARNING') as cm:
            study = PACSSeriesSerializer._ensure_study(pacs, drift)

        self.assertEqual(study.PatientName, 'Smith^John')  # not overwritten
        self.assertTrue(any('PatientName mismatch' in line for line in cm.output))

    @tag('integration')
    def test_create_links_study_and_increments_counter(self):
        """
        End-to-end: ingest finds-or-creates the study, links the series to it, and
        the series counter reflects the new row.
        """
        chris_user = User.objects.get(username=self.chris_username)
        path = f'SERVICES/PACS/{self.pacs_name}/777-jane/brain_study'
        data = {'PatientID': '777', 'PatientName': 'jane',
                'StudyDate': '2021-08-16',
                'StudyInstanceUID': '1.1.study.counter',
                'StudyDescription': 'brain_study',
                'SeriesDescription': 'AX T2',
                'SeriesInstanceUID': '2.2.series.counter',
                'owner': chris_user,
                'pacs_name': self.pacs_name, 'path': path}

        storage_manager = connect_storage(settings)
        with io.StringIO('Test file') as f:
            storage_manager.upload_obj(path + '/AXT2/a.dcm', f.read(),
                                       content_type='text/plain')
        data['files_in_storage'] = [path + '/AXT2/a.dcm']

        series = PACSSeriesSerializer(data=data).create(data)
        self.assertIsNotNone(series.study)
        self.assertEqual(series.study.StudyInstanceUID, '1.1.study.counter')
        series.study.refresh_from_db()
        self.assertEqual(series.study.NumberOfStudyRelatedSeries, 1)

        storage_manager.delete_path(path)

    @tag('integration')
    def test_create_success(self):
        """
        Test whether overriden 'create' method successfully creates a new PACS series.
        """
        chris_user = User.objects.get(username=self.chris_username)
        path = f'SERVICES/PACS/{self.pacs_name}/123456-crazy/brain_crazy_study'
        data = {'PatientID': '123456', 'PatientName': 'crazy',
                'StudyDate': '2020-07-15',
                'StudyInstanceUID': '1.1.3432.54.6545674765.765434',
                'StudyDescription': 'brain_crazy_study',
                'SeriesDescription': 'SAG T1 MPRAGE',
                'SeriesInstanceUID': '2.4.3432.54.845674765.763345',
                'owner': chris_user,
                'pacs_name': f'{self.pacs_name}', 'path': path}

        storage_manager = connect_storage(settings)

        # upload two files to the storage PACS's space
        with io.StringIO('Test file') as f:
            storage_manager.upload_obj(path + '/, ,/SAG,T1,MPRAGE/tes,t1.dcm', f.read(),
                                       content_type='text/plain')
            f.seek(0)
            storage_manager.upload_obj(path + '/, ,/SAG,T1,MPRAGE/test2.dcm', f.read(),
                                       content_type='text/plain')

        data['files_in_storage'] = [path + '/, ,/SAG,T1,MPRAGE/tes,t1.dcm',
                                    path + '/, ,/SAG,T1,MPRAGE/test2.dcm']
        pacs_series_serializer = PACSSeriesSerializer(data=data)
        pacs_series = pacs_series_serializer.create(data)

        self.assertEqual(str(pacs_series.folder.path), path)

        folders = pacs_series.folder.children.all()
        self.assertEqual(len(folders), 1)

        folder = folders.first()
        fnames = [f.fname.name for f in folder.chris_files.all()]

        self.assertEqual(len(fnames), 2)
        self.assertIn(path + '/SAGT1MPRAGE/test1.dcm', fnames)
        self.assertIn(path + '/SAGT1MPRAGE/test2.dcm', fnames)

        # delete files from storage
        storage_manager.delete_path(path)


    def test_validate_pacs_name_failure_contains_commas_or_slashes(self):
        """
        Test whether overriden validate_pacs_name method validates that the provided
        PACS name does not contain commas or forward slashes.
        """
        pacs_series_serializer = PACSSeriesSerializer()

        with self.assertRaises(serializers.ValidationError):
            pacs_series_serializer.validate_pacs_name('My/pacs')
        with self.assertRaises(serializers.ValidationError):
            pacs_series_serializer.validate_pacs_name('My,pacs')

    def test_validate_ndicom_failure_not_positive(self):
        """
        Test whether overriden validate_ndicom method validates submitted ndicom must
        be a positive integer.
        """
        pacs_series_serializer = PACSSeriesSerializer()
        with self.assertRaises(serializers.ValidationError):
            pacs_series_serializer.validate_ndicom(0)

    def test_validate_ndicom_success(self):
        """
        Test whether overriden validate_ndicom method properly validates submitted ndicom.
        """
        pacs_series_serializer = PACSSeriesSerializer()
        self.assertEqual(pacs_series_serializer.validate_ndicom(1), 1)

    def test_validate_path_failure_contains_commas(self):
        """
        Test whether overriden validate_path method validates that the provided
        path does not contain commas or forward slashes.
        """
        pacs_series_serializer = PACSSeriesSerializer()

        with self.assertRaises(serializers.ValidationError):
            path = 'SERVICES/PACS/MyPACS/123456-crazy/brain,crazy,study/SAG_T1_MPRAGE'
            pacs_series_serializer.validate_path(path)

    def test_validate_path_failure_does_not_start_with_SERVICES_PACS(self):
        """
        Test whether overriden validate_path method validates submitted path must start
        with the 'SERVICES/PACS/' string.
        """
        pacs_series_serializer = PACSSeriesSerializer()
        path = 'cube/123456-Jorge/brain/brain_mri'
        with self.assertRaises(serializers.ValidationError):
            pacs_series_serializer.validate_path(path)
        path = 'SERVICES/123456-Jorge/brain/brain_mri'
        with self.assertRaises(serializers.ValidationError):
            pacs_series_serializer.validate_path(path)


    def test_validate_path_failure_path_not_plain(self):
        """
        Test whether overriden validate_path method validates that no name in the
        submitted path is empty, '.' or '..'.
        """
        pacs_series_serializer = PACSSeriesSerializer()

        for path in ('SERVICES/PACS/MyPACS/123456-crazy//SAG_T1_MPRAGE',
                     'SERVICES/PACS/MyPACS/./SAG_T1_MPRAGE',
                     'SERVICES/PACS/MyPACS/../../home/cube'):
            with self.subTest(path=path), self.assertRaises(serializers.ValidationError):
                pacs_series_serializer.validate_path(path)

    def test_validate_path_success(self):
        """
        Test whether overriden validate_path method validates submitted path.
        """
        pacs_series_serializer = PACSSeriesSerializer()
        path = 'SERVICES/PACS/MyPACS/123456-crazy/brain_crazy_study/SAG_T1_MPRAGE'
        self.assertEqual(pacs_series_serializer.validate_path(path), path)

    def test_validate_failure_path_does_not_start_with_SERVICES_PACS_pacs_name(self):
        """
        Test whether overriden validate method validates that submitted path must start
        with the 'SERVICES/PACS/pacs_name' string.
        """
        path = 'SERVICES/PACS/MyPACS/123456-crazy/brain_crazy_study/SAG_T1_MPRAGE'
        data = {'PatientID': '123456', 'PatientName': 'crazy',
                'StudyDate': '2020-07-15',
                'StudyInstanceUID': '1.1.3432.54.6545674765.765434',
                'StudyDescription': 'brain_crazy_study',
                'SeriesDescription': 'SAG T1 MPRAGE',
                'SeriesInstanceUID': '2.4.3432.54.845674765.763345',
                'pacs_name': 'MyPACS1', 'path': path, 'ndicom': 1}

        with self.assertRaises(serializers.ValidationError):
            pacs_series_serializer = PACSSeriesSerializer()
            pacs_series_serializer.validate(data)

    def test_validate_failure_ndicom_in_storage_different_from_request_data(self):
        """
        Test whether overriden validate method validates that submitted path must start
        with the 'SERVICES/PACS/pacs_name' string.
        """
        path = 'SERVICES/PACS/MyPACS/123456-crazy/brain_crazy_study/SAG_T1_MPRAGE'
        data = {'PatientID': '123456', 'PatientName': 'crazy',
                'StudyDate': '2020-07-15',
                'StudyInstanceUID': '1.1.3432.54.6545674765.765434',
                'StudyDescription': 'brain_crazy_study',
                'SeriesDescription': 'SAG T1 MPRAGE',
                'SeriesInstanceUID': '2.4.3432.54.845674765.763345',
                'pacs_name': 'MyPACS', 'path': path, 'ndicom': 2}


        storage_manager_mock = mock.Mock()
        storage_manager_mock.ls = mock.Mock(return_value=['file1'])

        with mock.patch('pacsfiles.serializers.connect_storage') as connect_storage_mock:
            connect_storage_mock.return_value = storage_manager_mock

            with self.assertRaises(serializers.ValidationError):
                pacs_series_serializer = PACSSeriesSerializer()
                pacs_series_serializer.validate(data)

    def test_validate_success(self):
        """
        Test whether overriden validate method correctly validates data.
        """
        path = 'SERVICES/PACS/MyPACS/123456-crazy/brain_crazy_study/SAG_T1_MPRAGE'
        data = {'PatientID': '123456', 'PatientName': 'crazy',
                'StudyDate': '2020-07-15',
                'StudyInstanceUID': '1.1.3432.54.6545674765.765434',
                'StudyDescription': 'brain_crazy_study',
                'SeriesDescription': 'SAG T1 MPRAGE',
                'SeriesInstanceUID': '2.4.3432.54.845674765.763345',
                'pacs_name': 'MyPACS', 'path': path, 'ndicom': 1}


        storage_manager_mock = mock.Mock()
        storage_manager_mock.ls = mock.Mock(return_value=[
            'SERVICES/PACS/MyPACS/123456-crazy/brain_crazy_study/SAG_T1_MPRAGE/file1.dcm'])

        with mock.patch('pacsfiles.serializers.connect_storage') as connect_storage_mock:
            connect_storage_mock.return_value = storage_manager_mock
            pacs_series_serializer = PACSSeriesSerializer()
            pacs_series_serializer.validate(data)
            storage_manager_mock.ls.assert_called_with(path)
