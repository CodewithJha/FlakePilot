"""Loopback TCP with mutual TLS.

Certificates exist only in the test temporary directory. This is not a
deployed execution host, not a public listener, and not production isolation.
"""

import hashlib
import socket
import ssl
import subprocess
import time
from pathlib import Path

import pytest

from flakepilot.execution.application.host import HostRejected, MemoryLedger, seal
from flakepilot.execution.application.reference_host import ReferenceHost
from flakepilot.execution.infrastructure.host_channel import (
    LoopbackHostChannel,
    LoopbackHostClient,
)

EXEC = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
NOW = 1_700_000_000
CID = "ab" * 6
KEY = b"k" * 32
_ROOT = Path(__file__).resolve().parents[3]


def _openssl(directory: Path, *args: str) -> None:
    subprocess.run(
        ["openssl", *args],
        cwd=directory,
        check=True,
        capture_output=True,
    )


def _fingerprint(path: Path) -> str:
    der = subprocess.run(
        ["openssl", "x509", "-in", str(path), "-outform", "der"],
        check=True,
        capture_output=True,
    ).stdout
    return hashlib.sha256(der).hexdigest()


def _material(tmp: Path) -> dict[str, Path | str]:
    _openssl(
        tmp,
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        "ca.key",
        "-out",
        "ca.crt",
        "-days",
        "1",
        "-subj",
        "/CN=fp-test-ca",
    )
    for name, ext, subject in (
        (
            "server",
            "subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\n",
            "/CN=fp-host",
        ),
        ("client", "extendedKeyUsage=clientAuth\n", "/CN=fp-control"),
        ("other", "extendedKeyUsage=clientAuth\n", "/CN=fp-other"),
    ):
        _openssl(
            tmp,
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            f"{name}.key",
            "-out",
            f"{name}.csr",
            "-subj",
            subject,
        )
        (tmp / f"{name}.ext").write_text(ext, encoding="ascii")
        _openssl(
            tmp,
            "x509",
            "-req",
            "-in",
            f"{name}.csr",
            "-CA",
            "ca.crt",
            "-CAkey",
            "ca.key",
            "-CAcreateserial",
            "-out",
            f"{name}.crt",
            "-days",
            "1",
            "-extfile",
            f"{name}.ext",
        )
    return {
        "ca": tmp / "ca.crt",
        "server_crt": tmp / "server.crt",
        "server_key": tmp / "server.key",
        "client_crt": tmp / "client.crt",
        "client_key": tmp / "client.key",
        "other_crt": tmp / "other.crt",
        "other_key": tmp / "other.key",
        "server_fp": _fingerprint(tmp / "server.crt"),
        "client_fp": _fingerprint(tmp / "client.crt"),
    }


def _host(*, runtime: bool = True) -> tuple[ReferenceHost, MemoryLedger]:
    ledger = MemoryLedger()
    return (
        ReferenceHost(ledger, KEY, now=NOW, runtime_available=runtime),
        ledger,
    )


def test_the_listener_refuses_an_open_bind_and_stays_off_the_worker() -> None:
    text = (
        _ROOT / "src/flakepilot/execution/infrastructure/host_channel.py"
    ).read_text(encoding="utf-8")
    for name in ("0.0.0.0", "CERT_NONE", "docker.sock", "privileged"):
        assert name not in text
    worker = (_ROOT / "src/flakepilot/interface/composition_root/worker.py").read_text(
        encoding="utf-8"
    )
    assert "LoopbackHostChannel" not in worker
    assert "host_channel" not in worker
    with pytest.raises(HostRejected):
        LoopbackHostChannel(
            ReferenceHost(MemoryLedger(), KEY, now=NOW),
            certificate=Path("/no/such/cert"),
            private_key=Path("/no/such/key"),
            client_ca=Path("/no/such/ca"),
            client_fingerprints=frozenset({("ab" * 32)}),
        )


def test_loopback_mtls_registers_idempotently_and_rejects_a_bad_peer(
    tmp_path: Path,
) -> None:
    material = _material(tmp_path)
    host, ledger = _host()
    channel = LoopbackHostChannel(
        host,
        certificate=material["server_crt"],  # type: ignore[arg-type]
        private_key=material["server_key"],  # type: ignore[arg-type]
        client_ca=material["ca"],  # type: ignore[arg-type]
        client_fingerprints=frozenset({str(material["client_fp"])}),
        inventory=((CID, EXEC),),
        timeout=2.0,
    )
    port = channel.start()
    try:
        client = LoopbackHostClient(
            port,
            certificate=material["client_crt"],  # type: ignore[arg-type]
            private_key=material["client_key"],  # type: ignore[arg-type]
            server_ca=material["ca"],  # type: ignore[arg-type]
            server_fingerprint=str(material["server_fp"]),
            timeout=2.0,
        )
        message = f"register {EXEC} {NOW + 30}".encode()
        assert client.call("register", message, seal(message, KEY), read=False) == ""
        assert client.call("register", message, seal(message, KEY)) == "ok"
        assert ledger.phase(EXEC) == "open"
        assert ledger.lookup(EXEC) == (True, False, NOW + 30)
        conflict = f"register {EXEC} {NOW + 90}".encode()
        assert client.call("register", conflict, seal(conflict, KEY)) == "rejected"
        assert ledger.lookup(EXEC)[2] == NOW + 30
        other = LoopbackHostClient(
            port,
            certificate=material["other_crt"],  # type: ignore[arg-type]
            private_key=material["other_key"],  # type: ignore[arg-type]
            server_ca=material["ca"],  # type: ignore[arg-type]
            server_fingerprint=str(material["server_fp"]),
            timeout=2.0,
        )
        cancel = f"cancel {EXEC}".encode()
        assert other.call("cancel", cancel, seal(cancel, KEY)) == "rejected"
        assert ledger.phase(EXEC) == "open"
        pinned = LoopbackHostClient(
            port,
            certificate=material["client_crt"],  # type: ignore[arg-type]
            private_key=material["client_key"],  # type: ignore[arg-type]
            server_ca=material["ca"],  # type: ignore[arg-type]
            server_fingerprint="cd" * 32,
            timeout=2.0,
        )
        with pytest.raises(HostRejected):
            pinned.call("health", b"health", seal(b"health", KEY))
        assert ledger.phase(EXEC) == "open"
        assert KEY.hex() not in repr(channel)
        assert repr(channel) == "LoopbackHostChannel()"
    finally:
        channel.close()


def test_cleanup_uses_the_host_inventory_and_a_stall_writes_nothing(
    tmp_path: Path,
) -> None:
    material = _material(tmp_path)
    host, ledger = _host()
    channel = LoopbackHostChannel(
        host,
        certificate=material["server_crt"],  # type: ignore[arg-type]
        private_key=material["server_key"],  # type: ignore[arg-type]
        client_ca=material["ca"],  # type: ignore[arg-type]
        client_fingerprints=frozenset({str(material["client_fp"])}),
        inventory=((CID, EXEC),),
        timeout=0.4,
    )
    port = channel.start()
    try:
        client = LoopbackHostClient(
            port,
            certificate=material["client_crt"],  # type: ignore[arg-type]
            private_key=material["client_key"],  # type: ignore[arg-type]
            server_ca=material["ca"],  # type: ignore[arg-type]
            server_fingerprint=str(material["server_fp"]),
            timeout=2.0,
        )
        cancel = f"cancel {EXEC}".encode()
        assert client.call("cancel", cancel, seal(cancel, KEY)) == "ok"
        mac = seal(b"cleanup", KEY)
        assert client.call("cleanup", b"cleanup", mac) == "ok"
        assert client.call("cleanup", b"cleanup", mac) == "ok"
        assert ledger.phase(EXEC) == "cancelled"
        stalled = socket.create_connection(("127.0.0.1", port), timeout=2)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(material["client_crt"], material["client_key"])
        context.load_verify_locations(material["ca"])
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        tls = context.wrap_socket(stalled, server_hostname="127.0.0.1")
        try:
            tls.sendall((16).to_bytes(4, "big"))
            time.sleep(0.6)
        finally:
            tls.close()
        assert ledger.phase(EXEC) == "cancelled"
        assert ledger.rows() == ((EXEC, 0, 1),)
    finally:
        channel.close()
