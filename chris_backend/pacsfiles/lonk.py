"""
Implementation of the "Light Oxidicom NotifiKations Encoding"

See https://chrisproject.org/docs/oxidicom/lonk
"""
import asyncio
import enum
import logging
from typing import (
    Self,
    Callable,
    TypedDict,
    Literal,
    TypeGuard,
    Any,
    Awaitable,
)

import nats
from nats import NATS
from nats.aio.subscription import Subscription
from nats.aio.msg import Msg

logger = logging.getLogger(__name__)


class SubscriptionRequest(TypedDict):
    """
    A request to subscribe to LONK notifications about a DICOM series.
    """

    pacs_name: str
    SeriesInstanceUID: str
    action: Literal['subscribe']


class UnsubscriptionRequest(TypedDict):
    """
    A request to unsubscribe from *all* series notifications.
    """

    action: Literal['unsubscribe']


def validate_subscription(data: Any) -> TypeGuard[SubscriptionRequest]:
    if not isinstance(data, dict):
        return False
    return (
        data.get('action', None) == 'subscribe'
        and isinstance(data.get('SeriesInstanceUID', None), str)
        and isinstance(data.get('pacs_name', None), str)
    )


def validate_unsubscription(data: Any) -> TypeGuard[UnsubscriptionRequest]:
    if not isinstance(data, dict):
        return False
    return data.get('action', None) == 'unsubscribe'


class LonkProgress(TypedDict):
    """
    LONK "done" message.

    https://chrisproject.org/docs/oxidicom/lonk#lonk-message-encoding
    """

    ndicom: int


class LonkError(TypedDict):
    """
    LONK "error" message.

    https://chrisproject.org/docs/oxidicom/lonk#lonk-message-encoding
    """

    error: str


class LonkDone(TypedDict):
    """
    LONK "done" message.

    https://chrisproject.org/docs/oxidicom/lonk#lonk-message-encoding
    """

    done: bool


class LonkWsSubscription(TypedDict):
    """
    LONK-WS "subscribed" message.

    https://chrisproject.org/docs/oxidicom/lonk-ws#lonk-ws-subscription
    """

    subscribed: bool


LonkMessageData = LonkProgress | LonkError | LonkDone | LonkWsSubscription
"""
Lonk message data.

https://chrisproject.org/docs/oxidicom/lonk-ws#messages
"""


class Lonk(TypedDict):
    """
    Serialized LONK message about a DICOM series.

    https://chrisproject.org/docs/oxidicom#lonk-message-encoding
    """

    SeriesInstanceUID: str
    pacs_name: str
    message: LonkMessageData


class LonkClient:
    """
    "Light Oxidicom NotifiKations Encoding" client:
    A client for the messages sent by *oxidicom* over NATS.

    https://chrisproject.org/docs/oxidicom/lonk
    """

    def __init__(self, nc: NATS):
        self._nc = nc
        self._subscriptions: dict[str, Subscription] = {}

    @classmethod
    async def connect(cls, servers: str | list[str]) -> Self:
        return cls(await nats.connect(servers))

    async def subscribe(
        self,
        pacs_name: str,
        series_instance_uid: str,
        cb: Callable[[Lonk], Awaitable[None]],
    ):
        subject = subject_of(pacs_name, series_instance_uid)
        if (
            subscription := self._subscriptions.get(subject, None)
        ) is not None:
            return subscription  # already subscribed
        cb = _curry_message2json(pacs_name, series_instance_uid, cb)
        subscription = await self._nc.subscribe(subject, cb=cb)
        self._subscriptions[subscription.subject] = subscription
        return subscription

    async def unsubscribe_all(self):
        await asyncio.gather(
            *(s.unsubscribe() for s in self._subscriptions.values())
        )
        self._subscriptions = {}

    async def close(self):
        await self.unsubscribe_all()
        await self._nc.close()


def subject_of(pacs_name: str, series_instance_uid: str) -> str:
    """
    Get the NATS subject for a series.

    Equivalent to https://github.com/HARBOR-Medical-AI/oxidicom/blob/33838f22a5431a349b3b83a313035b8e22d16bb1/src/lonk.rs#L36-L48
    """
    return f'oxidicom.{_sanitize_topic_part(pacs_name)}.{_sanitize_topic_part(series_instance_uid)}'


def _sanitize_topic_part(s: str) -> str:
    return (
        s.replace('\0', '')
        .replace(' ', '_')
        .replace('.', '_')
        .replace('*', '_')
        .replace('>', '_')
    )


def _message2json(
    pacs_name: str, series_instance_uid: str, message: Msg
) -> Lonk:
    return Lonk(
        pacs_name=pacs_name,
        SeriesInstanceUID=series_instance_uid,
        message=_serialize_to_lonkws(message.data),
    )


def _curry_message2json(
    pacs_name: str,
    series_instance_uid: str,
    cb: Callable[[Lonk], Awaitable[None]],
):
    async def nats_callback(message: Msg):
        lonk = _message2json(pacs_name, series_instance_uid, message)
        await cb(lonk)

    return nats_callback


@enum.unique
class LonkMagicByte(enum.IntEnum):
    """
    LONK message first magic byte.
    """

    DONE = 0x00
    PROGRESS = 0x01
    ERROR = 0x02


def _serialize_to_lonkws(payload: bytes) -> LonkMessageData:
    """
    Translate LONK binary encoding to LONK-WS JSON.
    """
    if len(payload) == 0:
        raise ValueError('Empty message')
    data = payload[1:]

    match payload[0]:
        case LonkMagicByte.DONE.value:
            return LonkDone(done=True)
        case LonkMagicByte.PROGRESS.value:
            ndicom = int.from_bytes(data, 'little', signed=False)
            return LonkProgress(ndicom=ndicom)
        case LonkMagicByte.ERROR.value:
            msg = data.decode(encoding='utf-8')
            logger.error(f'Error from oxidicom: {msg}')
            error = 'oxidicom reported an error, check logs for details.'
            return LonkError(error=error)
        case _:
            hexstr = ' '.join(hex(b) for b in payload)
            raise ValueError(f'Unrecognized message: {hexstr}')
