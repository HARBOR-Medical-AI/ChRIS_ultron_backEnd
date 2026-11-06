
import jwt

from channels.db import database_sync_to_async
from django.conf import settings
from django.contrib.auth.models import User, Group

# note: use TransactionTestCase instead of TestCase for async tests that speak to DB.
# See https://stackoverflow.com/a/71763849
from django.test import TransactionTestCase, tag

from channels.testing import WebsocketCommunicator
from django.utils import timezone

from core.models import FileDownloadToken
from core.utils import download_token_signing_key
from core.websockets.auth import TokenQsAuthMiddleware

from pacsfiles.lonk import (
    SubscriptionRequest,
    Lonk,
    LonkWsSubscription,
    LonkProgress,
    LonkDone,
    LonkError,
    UnsubscriptionRequest,
)
from pacsfiles.consumers import PACSFileProgress
from pacsfiles.tests.mocks import Mockidicom


class PACSFileProgressTests(TransactionTestCase):
    def setUp(self):
        self.username = 'PintoGideon'
        self.password = 'gideon1234'
        self.email = 'gideon@example.org'
        self.user = User.objects.create_user(
            username=self.username, email=self.email, password=self.password
        )
        pacs_grp, _ = Group.objects.get_or_create(name='pacs_users')
        self.user.groups.set([pacs_grp])
        self.user.save()

    @tag('integration')
    async def test_lonk_ws(self):
        communicator, oxidicom = await self.connect()

        series1 = {'pacs_name': 'MyPACS', 'SeriesInstanceUID': '1.234.567890'}
        subscription_request = SubscriptionRequest(
            action='subscribe', **series1
        )
        await communicator.send_json_to(subscription_request)
        self.assertEqual(
            await communicator.receive_json_from(),
            Lonk(
                message=LonkWsSubscription(subscribed=True),
                **series1,
            ),
        )
        series2 = {'pacs_name': 'MyPACS', 'SeriesInstanceUID': '5.678.90123'}
        subscription_request = SubscriptionRequest(
            action='subscribe', **series2
        )
        await communicator.send_json_to(subscription_request)
        self.assertEqual(
            await communicator.receive_json_from(),
            Lonk(
                message=LonkWsSubscription(subscribed=True),
                **series2,
            ),
        )

        await oxidicom.send_progress(ndicom=1, **series1)
        self.assertEqual(
            await communicator.receive_json_from(),
            Lonk(message=LonkProgress(ndicom=1), **series1),
        )
        await oxidicom.send_progress(ndicom=115, **series1)
        self.assertEqual(
            await communicator.receive_json_from(),
            Lonk(message=LonkProgress(ndicom=115), **series1),
        )

        await oxidicom.send_error(error='stuck in chimney', **series2)
        expected = 'oxidicom reported an error, check logs for details.'
        # the oxidicom error is decoded in pacsfiles.lonk and logs at ERROR;
        # capture/suppress with assertLogs and verify the expected message fired
        with self.assertLogs('pacsfiles.lonk', level='ERROR') as cm:
            self.assertEqual(
                await communicator.receive_json_from(),
                Lonk(message=LonkError(error=expected), **series2),
            )
        self.assertTrue(any('stuck in chimney' in msg for msg in cm.output))

        await oxidicom.send_progress(ndicom=192, **series1)
        self.assertEqual(
            await communicator.receive_json_from(),
            Lonk(message=LonkProgress(ndicom=192), **series1),
        )
        await oxidicom.send_done(**series1)
        self.assertEqual(
            await communicator.receive_json_from(),
            Lonk(message=LonkDone(done=True), **series1),
        )

    @tag('integration')
    async def test_unsubscribe(self):
        """
        https://chrisproject.org/docs/oxidicom/lonk-ws#unsubscribe
        """
        communicator, oxidicom = await self.connect()

        series1 = {
            'pacs_name': 'MyPACSUnsub',
            'SeriesInstanceUID': '1.234.567890',
        }
        subscription_request = SubscriptionRequest(
            action='subscribe', **series1
        )
        await communicator.send_json_to(subscription_request)
        self.assertEqual(
            await communicator.receive_json_from(),
            Lonk(
                message=LonkWsSubscription(subscribed=True),
                **series1,
            ),
        )

        unsubscription_request = UnsubscriptionRequest(action='unsubscribe')
        await communicator.send_json_to(unsubscription_request)
        self.assertEqual(
            await communicator.receive_json_from(),
            {'message': {'subscribed': False}},
        )

        series2 = {
            'pacs_name': 'MyPACSUnsub',
            'SeriesInstanceUID': '5.678.90123',
        }
        subscription_request = SubscriptionRequest(
            action='subscribe', **series2
        )
        await communicator.send_json_to(subscription_request)
        self.assertEqual(
            await communicator.receive_json_from(),
            Lonk(
                message=LonkWsSubscription(subscribed=True),
                **series2,
            ),
        )

        await oxidicom.send_progress(ndicom=1, **series1)
        await oxidicom.send_progress(ndicom=2, **series2)
        self.assertEqual(
            await communicator.receive_json_from(),
            Lonk(
                message=LonkProgress(ndicom=2),
                **series2,  # unsubscribed from series1, should not be a message for it
            ),
        )

    async def connect(self) -> tuple[WebsocketCommunicator, Mockidicom]:
        token = await self._get_download_token()
        app = TokenQsAuthMiddleware(PACSFileProgress.as_asgi())
        communicator = WebsocketCommunicator(
            app, f'v1/pacs/ws/?token={token.token}'
        )
        connected, subprotocol = await communicator.connect()
        assert connected

        oxidicom: Mockidicom = await Mockidicom.connect(settings.NATS_ADDRESS)
        return communicator, oxidicom

    async def test_unauthenticated_not_connected(self):
        app = TokenQsAuthMiddleware(PACSFileProgress.as_asgi())
        communicator = WebsocketCommunicator(app, 'v1/pacs/ws/')  # no token
        connected, subprotocol = await communicator.connect()
        assert not connected

    @database_sync_to_async
    def _get_download_token(self) -> FileDownloadToken:
        """
        Copy-pasted from
        https://github.com/HARBOR-Medical-AI/ChRIS_ultron_backEnd/blob/7bcccc2031386955875ef4e9758025577f5ee067/chris_backend/userfiles/tests/test_views.py#L210-L213
        """
        dt = timezone.now() + timezone.timedelta(minutes=10)
        token = jwt.encode(
            {'user': self.user.username, 'exp': dt},
            download_token_signing_key(),
            algorithm='HS512',
        )
        return FileDownloadToken.objects.create(token=token, owner=self.user)
