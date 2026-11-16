"""
Celery queue-depth sampling against the Redis-protocol broker (Dragonfly).

Queue depth is the earliest place backpressure shows: a growing ``status`` list means
status checks are produced faster than the worker slots consume them, long before
CPU graphs make that obvious. Celery queues are plain Redis lists named after the
queue so a depth sample is one ``LLEN`` per queue.

Implemented over a raw socket (the RESP protocol is three lines of framing) to avoid
adding a redis dependency to the harness. Degrades to unavailable — never raises out
of ``depths()`` or ``oldest_wait()`` — so the rest of the harness runs without a broker,
same as ``DockerClient`` without a socket.
"""

from __future__ import annotations

import json
import socket
from typing import Optional


#: CUBE's Celery queues (chris_backend/core/celery.py QUEUES)
CELERY_QUEUES = ("submit", "status", "register", "register_large", "delete", "cleanup",
                 "pacs", "periodic")

#: the queue of the status checks, at most one pending per running instance
STATUS_QUEUE = "status"

#: the header CUBE stamps each message with when it publishes it (core/celery.py
#: PUBLISHED_AT_HEADER)
PUBLISHED_AT_HEADER = "cube_published_at"


def encode_command(*args: str) -> bytes:
    """
    RESP array of bulk strings, e.g. LLEN status.
    """
    out = f"*{len(args)}\r\n".encode()
    for arg in args:
        data = arg.encode()
        out += b"$" + str(len(data)).encode() + b"\r\n" + data + b"\r\n"
    return out


def read_reply(stream) -> "int | str | bytes | None":
    """
    Read one reply from ``stream``: a single-line one (``parse_reply``) or a bulk string,
    its bytes (``None`` for a nil one, which LINDEX gives past the end of a list).
    """
    line = stream.readline()

    if not line.startswith(b"$"):
        return parse_reply(line)

    length = int(line[1:].strip())
    if length < 0:
        return None

    data = stream.read(length + 2)
    if len(data) != length + 2:
        raise ValueError("truncated RESP bulk string")
    return data[:-2]


def parse_reply(line: bytes) -> "int | str":
    """
    Parse a single-line RESP reply (integer or simple string).
    """
    line = line.strip()

    if line.startswith(b":"):
        return int(line[1:])
    
    if line.startswith(b"+"):
        return line[1:].decode()
    raise ValueError(f"unexpected RESP reply: {line!r}")


class BrokerClient:
    """
    Best-effort LLEN sampler for the Celery queues.
    """

    def __init__(self, host: str = "chris-broker", port: int = 6379, *,
                 queues: tuple[str, ...] = CELERY_QUEUES, timeout: float = 2.0):
        self.host = host
        self.port = port
        self.queues = queues
        self.timeout = timeout
        self.unavailable_reason: Optional[str] = None

        try:
            if self._command("PING") != "PONG":
                self.unavailable_reason = "unexpected PING reply"
        except OSError as exc:
            self.unavailable_reason = str(exc)

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None

    def depths(self) -> dict[str, int]:
        """
        Current depth per queue, the LLENs sent together on one connection; {} when the
        broker is unreachable.
        """
        if not self.available:
            return {}
        try:
            replies = self._pipeline([("LLEN", q) for q in self.queues])
            return {q: int(reply) for q, reply in zip(self.queues, replies)}
        except (OSError, ValueError):
            return {}

    def oldest_wait(self, queue: str, now: float) -> Optional[float]:
        """
        How long the message a worker would take next from ``queue`` (the end of its list
        that BRPOP pops, the oldest) has waited at the epoch time ``now``, from the stamp
        CUBE puts in its headers; 0.0 when the queue is empty (a worker took its last
        message since its depth was read: nothing waits), ``None`` when the message has no
        stamp (oxidicom's) or the broker cannot tell.
        """
        if not self.available:
            return None
        try:
            raw = self._command("LINDEX", queue, "-1")
            if raw is None:
                return 0.0
            if not isinstance(raw, bytes):
                return None
            published = json.loads(raw)["headers"][PUBLISHED_AT_HEADER]
            return round(now - float(published), 1)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _command(self, *args: str):
        return self._pipeline([args])[0]

    def _pipeline(self, commands: list[tuple[str, ...]]) -> list:
        """
        Send ``commands`` at once on one connection and read their replies.
        """
        with socket.create_connection((self.host, self.port),
                                      timeout=self.timeout) as sock:
            sock.sendall(b"".join(encode_command(*args) for args in commands))
            stream = sock.makefile("rb")
            return [read_reply(stream) for _ in commands]
