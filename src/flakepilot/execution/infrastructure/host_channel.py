"""Loopback mutual TLS for one protocol session.

The listener binds 127.0.0.1 and requires TLS 1.2 or newer with a client
certificate. There is no switch that turns verification off. The certificate
fingerprint allowlist is the peer authorization. The HMAC inside the frame
stays the application integrity check. A frame cannot choose a path, a
program, or a container id.

This is a real TCP listener for tests of that boundary. It is not a deployed
execution host and it is not a public endpoint. The worker does not start it.
"""

from __future__ import annotations

import hashlib
import socket
import ssl
import threading
from pathlib import Path

from flakepilot.execution.application.frame import (
    MAX_FRAME,
    FrameRejected,
    decode_request,
    encode_request,
    encode_response,
)
from flakepilot.execution.application.frame import (
    decode_response as decode_status,
)
from flakepilot.execution.application.host import HostRejected, LedgerError
from flakepilot.execution.application.reference_host import ReferenceHost

__all__ = ["LoopbackHostChannel", "LoopbackHostClient"]

_BIND = "127.0.0.1"


class LoopbackHostChannel:
    """One loopback listener. `close` stops it. Cleanup uses `inventory`."""

    def __init__(
        self,
        host: ReferenceHost,
        *,
        certificate: Path,
        private_key: Path,
        client_ca: Path,
        client_fingerprints: frozenset[str],
        inventory: tuple[tuple[str, str], ...] = (),
        timeout: float = 2.0,
    ) -> None:
        if (
            not isinstance(host, ReferenceHost)
            or not _files(certificate, private_key, client_ca)
            or not _fingerprints(client_fingerprints)
            or not isinstance(inventory, tuple)
            or not _timeout(timeout)
        ):
            raise HostRejected
        self._host = host
        self._certificate = certificate
        self._private_key = private_key
        self._client_ca = client_ca
        self._fingerprints = client_fingerprints
        self._inventory = inventory
        self._timeout = timeout
        self._closed = False
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None

    def __repr__(self) -> str:
        return "LoopbackHostChannel()"

    def start(self) -> int:
        """Bind 127.0.0.1 on an ephemeral port and accept until `close`."""
        if self._socket is not None:
            raise HostRejected
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind((_BIND, 0))
            listener.listen(8)
            listener.settimeout(0.2)
        except OSError:
            listener.close()
            raise HostRejected from None
        self._socket = listener
        port = listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, name="host-channel")
        self._thread.start()
        return int(port)

    def close(self) -> None:
        self._closed = True
        if self._socket is not None:
            self._socket.close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _serve(self) -> None:
        listener = self._socket
        if listener is None:
            return
        while not self._closed:
            try:
                connection, address = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if address[0] != _BIND:
                connection.close()
                continue
            try:
                self._handle(connection)
            except (OSError, ssl.SSLError, HostRejected, ValueError):
                pass
            finally:
                connection.close()

    def _handle(self, connection: socket.socket) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self._certificate, self._private_key)
        context.load_verify_locations(self._client_ca)
        context.verify_mode = ssl.CERT_REQUIRED
        tls = context.wrap_socket(connection, server_side=True)
        try:
            frame = _read_frame(tls, self._timeout)
            peer = tls.getpeercert(binary_form=True)
            if (
                not isinstance(peer, bytes)
                or _fingerprint(peer) not in self._fingerprints
            ):
                _send(tls, encode_response("rejected"), self._timeout)
                return
            operation, message, mac_hex = decode_request(frame)
            self._dispatch(operation, message, mac_hex)
            _send(tls, encode_response("ok"), self._timeout)
        except (
            HostRejected,
            FrameRejected,
            LedgerError,
            OSError,
            ssl.SSLError,
            ValueError,
        ):
            try:
                _send(tls, encode_response("rejected"), self._timeout)
            except (OSError, ssl.SSLError, HostRejected):
                return
        finally:
            tls.close()

    def _dispatch(self, operation: str, message: bytes, mac_hex: str) -> None:
        if operation == "health":
            self._host.health(message, mac_hex)
            return
        if operation == "cleanup":
            status, _selected = self._host.authenticated_cleanup(
                message, mac_hex, self._inventory
            )
            if status != "ok":
                raise HostRejected
            return
        self._host.accept(operation, message, mac_hex)


class LoopbackHostClient:
    """One request per connection. The server certificate fingerprint is pinned."""

    def __init__(
        self,
        port: int,
        *,
        certificate: Path,
        private_key: Path,
        server_ca: Path,
        server_fingerprint: str,
        timeout: float = 2.0,
    ) -> None:
        if (
            not isinstance(port, int)
            or isinstance(port, bool)
            or not 0 < port <= 65535
            or not _files(certificate, private_key, server_ca)
            or not _fingerprint_ok(server_fingerprint)
            or not _timeout(timeout)
        ):
            raise HostRejected
        self._port = port
        self._certificate = certificate
        self._private_key = private_key
        self._server_ca = server_ca
        self._server_fingerprint = server_fingerprint
        self._timeout = timeout

    def __repr__(self) -> str:
        return "LoopbackHostClient()"

    def call(
        self, operation: str, message: bytes, mac_hex: str, *, read: bool = True
    ) -> str:
        """Send one frame. `read=False` closes before the response is read."""
        if not isinstance(read, bool):
            raise HostRejected
        raw = socket.create_connection((_BIND, self._port), timeout=self._timeout)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self._certificate, self._private_key)
        context.load_verify_locations(self._server_ca)
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        tls = context.wrap_socket(raw, server_hostname=_BIND)
        try:
            peer = tls.getpeercert(binary_form=True)
            if (
                not isinstance(peer, bytes)
                or _fingerprint(peer) != self._server_fingerprint
            ):
                raise HostRejected
            _send(tls, encode_request(operation, message, mac_hex), self._timeout)
            if not read:
                return ""
            return decode_status(_read_frame(tls, self._timeout))
        finally:
            tls.close()


def _files(*paths: Path) -> bool:
    return all(isinstance(path, Path) and path.is_file() for path in paths)


def _timeout(value: float) -> bool:
    return (
        isinstance(value, float)
        and not isinstance(value, bool)
        and 0.05 <= value <= 30.0
    )


def _fingerprints(values: frozenset[str]) -> bool:
    return (
        isinstance(values, frozenset)
        and len(values) > 0
        and all(_fingerprint_ok(item) for item in values)
    )


def _fingerprint_ok(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _fingerprint(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def _send(tls: ssl.SSLSocket, payload: bytes, timeout: float) -> None:
    tls.settimeout(timeout)
    view = memoryview(payload)
    sent = 0
    while sent < len(view):
        count = tls.send(view[sent:])
        if count <= 0:
            raise HostRejected
        sent += count


def _read_frame(tls: ssl.SSLSocket, timeout: float) -> bytes:
    prefix = _read_exact(tls, 4, timeout)
    length = int.from_bytes(prefix, "big")
    if length < 1 or length > MAX_FRAME:
        raise HostRejected
    return prefix + _read_exact(tls, length, timeout)


def _read_exact(tls: ssl.SSLSocket, count: int, timeout: float) -> bytes:
    tls.settimeout(timeout)
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = tls.recv(remaining)
        if not chunk:
            raise HostRejected
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
