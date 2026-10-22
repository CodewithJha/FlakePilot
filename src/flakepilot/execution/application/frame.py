"""Length-prefixed frames for host protocol version 1.

This module does not open a socket. A request body is
`1\\n<operation>\\n<message>\\n<mac>\\n`. The mac is not checked here. The
message stays the version 1 grammar. A frame cannot name a path, a program,
or a container id.
"""

from flakepilot.execution.application.reference_host import OPERATIONS

__all__ = [
    "MAX_FRAME",
    "FrameRejected",
    "decode_request",
    "decode_response",
    "encode_request",
    "encode_response",
]

MAX_FRAME = 256


class FrameRejected(Exception):
    """The frame was refused. The text does not echo the bytes."""

    def __init__(self) -> None:
        super().__init__("rejected")


def encode_request(operation: str, message: bytes, mac_hex: str) -> bytes:
    body = _request_body(operation, message, mac_hex)
    return len(body).to_bytes(4, "big") + body


def decode_request(frame: bytes) -> tuple[str, bytes, str]:
    body = _body(frame)
    lines = body.split(b"\n")
    if len(lines) != 5 or lines[4] != b"" or lines[0] != b"1":
        raise FrameRejected
    try:
        operation = lines[1].decode("ascii")
        mac_hex = lines[3].decode("ascii")
    except UnicodeError:
        raise FrameRejected from None
    message = lines[2]
    _request_body(operation, message, mac_hex)
    return operation, message, mac_hex


def encode_response(status: str) -> bytes:
    if status not in {"ok", "rejected"}:
        raise FrameRejected
    body = f"1\n{status}\n".encode("ascii")
    return len(body).to_bytes(4, "big") + body


def decode_response(frame: bytes) -> str:
    body = _body(frame)
    lines = body.split(b"\n")
    if len(lines) != 3 or lines[2] != b"" or lines[0] != b"1":
        raise FrameRejected
    try:
        status = lines[1].decode("ascii")
    except UnicodeError:
        raise FrameRejected from None
    if status not in {"ok", "rejected"}:
        raise FrameRejected
    return status


def _body(frame: bytes) -> bytes:
    if not isinstance(frame, bytes) or len(frame) < 4:
        raise FrameRejected
    length = int.from_bytes(frame[:4], "big")
    if length < 1 or length > MAX_FRAME or len(frame) != 4 + length:
        raise FrameRejected
    return frame[4:]


def _request_body(operation: str, message: bytes, mac_hex: str) -> bytes:
    if (
        operation not in OPERATIONS
        or not isinstance(message, bytes)
        or not isinstance(mac_hex, str)
        or len(mac_hex) != 64
        or any(character not in "0123456789abcdef" for character in mac_hex)
        or len(message) > 80
        or not message.isascii()
        or any(character in message for character in b"\n\r\x00")
        or message.split(b" ")[0] != operation.encode("ascii")
    ):
        raise FrameRejected
    if operation == "cleanup" and message != b"cleanup":
        raise FrameRejected
    if operation == "health" and message != b"health":
        raise FrameRejected
    body = b"1\n" + operation.encode("ascii") + b"\n" + message + b"\n"
    body += mac_hex.encode("ascii") + b"\n"
    if len(body) > MAX_FRAME:
        raise FrameRejected
    return body
