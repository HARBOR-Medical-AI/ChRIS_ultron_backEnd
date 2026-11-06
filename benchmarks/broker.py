"""
Celery queue-depth sampling against the Redis-protocol broker (Dragonfly).

Queue depth is the earliest place backpressure shows: a growing ``main2`` list means
status checks are produced faster than the worker slots consume them, long before
CPU graphs make that obvious. Celery queues are plain Redis lists named after the
queue so a depth sample is one ``LLEN`` per queue.

Implemented over a raw socket (the RESP protocol is three lines of framing) to avoid
adding a redis dependency to the harness. Degrades to unavailable — never raises out
of ``depths()`` — so the rest of the harness runs without a broker, same as
``DockerClient`` without a socket.
"""

from __future__ import annotations

import socket
from typing import Optional


CELERY_QUEUES = ("main1", "main2", "periodic")


def encode_command(*args: str) -> bytes:
    """
    RESP array of bulk strings, e.g. LLEN main1.
    """
    out = f"*{len(args)}\r\n".encode()
    for arg in args:
        data = arg.encode()
        out += b"$" + str(len(data)).encode() + b"\r\n" + data + b"\r\n"
    return out


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

    def _command(self, *args: str):
        return self._pipeline([args])[0]

    def _pipeline(self, commands: list[tuple[str, ...]]) -> list:
        """
        Send ``commands`` at once on one connection and read their single-line replies.
        """
        with socket.create_connection((self.host, self.port),
                                      timeout=self.timeout) as sock:
            sock.sendall(b"".join(encode_command(*args) for args in commands))
            stream = sock.makefile("rb")
            return [parse_reply(stream.readline()) for _ in commands]
