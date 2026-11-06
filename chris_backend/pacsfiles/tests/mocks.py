from typing import Self

import nats
from nats import NATS

from pacsfiles import lonk
from pacsfiles.lonk import LonkMagicByte


class Mockidicom:
    """
    A mock *oxidicom* which sends LONK messages to NATS.

    Somewhat similar to https://github.com/HARBOR-Medical-AI/oxidicom/blob/e6bb83d1ea2fbaf5bb4af7dbf518a4b1a2957f2d/src/lonk.rs
    """

    def __init__(self, nc: NATS):
        self._nc = nc

    @classmethod
    async def connect(cls, servers: str | list[str]) -> Self:
        nc = await nats.connect(servers)
        return cls(nc)

    async def send_progress(
        self, pacs_name: str, SeriesInstanceUID: str, ndicom: int
    ):
        subject = lonk.subject_of(pacs_name, SeriesInstanceUID)
        u32 = ndicom.to_bytes(length=4, byteorder='little', signed=False)
        data = LonkMagicByte.PROGRESS.value.to_bytes() + u32
        await self._nc.publish(subject, data)

    async def send_done(self, pacs_name: str, SeriesInstanceUID: str):
        subject = lonk.subject_of(pacs_name, SeriesInstanceUID)
        await self._nc.publish(subject, LonkMagicByte.DONE.value.to_bytes())

    async def send_error(
        self, pacs_name: str, SeriesInstanceUID: str, error: str
    ):
        subject = lonk.subject_of(pacs_name, SeriesInstanceUID)
        data = LonkMagicByte.ERROR.value.to_bytes() + error.encode(
            encoding='utf-8'
        )
        await self._nc.publish(subject, data)

    async def close(self):
        self._nc.close()
