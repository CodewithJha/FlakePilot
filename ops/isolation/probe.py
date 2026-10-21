"""Benign probes that run inside the digest-pinned container.

The harness feeds this file on stdin. It does not read the repository, the
network, or a secret. A mode that cannot do what it attempts prints a RESULT
line and exits. The memory mode is expected to die when the cgroup kills it.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import socket
import sys
import time

SELECTOR = "tests.login.TestLogin.test_ok"
_HOST_MARKER = "flakepilot-host-only"
_CLONE_NEWNS = 0x20000
_CLONE_NEWUSER = 0x10000000


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    modes = {
        "network": network,
        "metadata": metadata,
        "mounts": mounts,
        "creds": creds,
        "privs": privs,
        "disk": disk,
        "pids": pids,
        "cpu": cpu,
        "memory": memory,
        "emit-bad": emit_bad,
        "emit-big": emit_big,
        "emit-malformed": emit_malformed,
        "emit-ok": emit_ok,
        "tenant-write": tenant_write,
        "tenant-read": tenant_read,
    }
    chosen = modes.get(mode)
    if chosen is None:
        print(f"RESULT unknown_mode={mode or 'missing'}")
        sys.exit(2)
    chosen()


def _connect(host: str, port: int) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        sock.connect((host, port))
    except TimeoutError:
        return "blocked errno=timeout"
    except OSError as exc:
        return f"blocked errno={exc.errno}"
    else:
        return "open errno=0"
    finally:
        sock.close()


def network() -> None:
    print("RESULT net_1_1_1_1=" + _connect("1.1.1.1", 443))
    print("RESULT net_8_8_8_8=" + _connect("8.8.8.8", 53))
    print("RESULT net_lo=" + _connect("127.0.0.1", 9))


def metadata() -> None:
    print("RESULT meta_imds=" + _connect("169.254.169.254", 80))
    print("RESULT meta_azure=" + _connect("168.63.129.16", 80))
    print("RESULT control_plane=absent")


def _flag(path: str) -> str:
    return "1" if os.path.exists(path) else "0"


def mounts() -> None:
    marker = "/tmp/fp-host-marker"
    content = "absent"
    if os.path.exists(marker):
        try:
            with open(marker, encoding="utf-8") as handle:
                content = handle.read().strip()
        except OSError as exc:
            content = f"errno-{exc.errno}"
    if content == _HOST_MARKER:
        content = "HOST"
    print(f"RESULT marker_exists={_flag(marker)} marker_content={content}")
    print("RESULT sock_docker=" + _flag("/var/run/docker.sock"))
    print("RESULT sock_run=" + _flag("/run/docker.sock"))
    print("RESULT home_runner=" + _flag("/home/runner"))
    print("RESULT runner_file=" + _flag("/home/runner/fp-host-secret"))
    print("RESULT docker_dir=" + _flag("/var/lib/docker"))
    with open("/proc/self/mountinfo", encoding="utf-8") as handle:
        text = handle.read()
    graph = "1" if ("/var/lib/docker" in text or "docker.sock" in text) else "0"
    print(f"RESULT graph_path={graph}")


def creds() -> None:
    words = (
        "GITHUB",
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "DATABASE",
        "AWS_",
        "AZURE",
        "PRIVATE_KEY",
    )
    bad = [key for key in os.environ if any(word in key.upper() for word in words)]
    leaked = [
        key
        for key, value in os.environ.items()
        if "ghs_" in value or "ghp_" in value or "github_pat_" in value
    ]
    keys = ",".join(sorted(os.environ))
    print("RESULT bad=" + (",".join(bad) if bad else "none"))
    print("RESULT leak=" + (",".join(leaked) if leaked else "none"))
    print(f"RESULT keys={keys}")
    print("RESULT sock_docker=" + _flag("/var/run/docker.sock"))
    print("RESULT sock_containerd=" + _flag("/run/containerd/containerd.sock"))


def _status(name: str) -> str:
    prefix = name + ":"
    with open("/proc/self/status", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith(prefix):
                return line.split(":", 1)[1].strip()
    return "missing"


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def _mount() -> str:
    libc = _libc()
    libc.mount.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    libc.mount.restype = ctypes.c_int
    ctypes.set_errno(0)
    if libc.mount(b"none", b"/tmp", b"tmpfs", 0, None) == 0:
        return "mounted"
    return f"errno={ctypes.get_errno()}"


def _unshare(flags: int) -> str:
    libc = _libc()
    libc.unshare.argtypes = [ctypes.c_int]
    libc.unshare.restype = ctypes.c_int
    ctypes.set_errno(0)
    if libc.unshare(flags) == 0:
        return "created"
    return f"errno={ctypes.get_errno()}"


def privs() -> None:
    print(f"RESULT uid={os.getuid()}")
    print(f"RESULT gid={os.getgid()}")
    print("RESULT capeff=" + _status("CapEff"))
    print("RESULT nonewprivs=" + _status("NoNewPrivs"))
    try:
        os.setuid(0)
    except OSError as exc:
        print(f"RESULT setuid_errno={exc.errno}")
    else:
        print("RESULT setuid_errno=0")
    print("RESULT mount=" + _mount())
    print("RESULT mntns=" + _unshare(_CLONE_NEWNS))
    print("RESULT userns=" + _unshare(_CLONE_NEWUSER))


def disk() -> None:
    """Write 2 MB in 64 KB pieces. A short write is not success.

    `FileIO.write` can return fewer bytes than requested without raising.
    The previous probe treated that as `disk_errno=0`. This one keeps going
    until 2 MB is stored or the filesystem refuses the next piece.
    """
    print("RESULT mount=" + _tmp_mount())
    chunk = b"x" * (64 * 1024)
    target = 2 * 1024 * 1024
    written = 0
    errno = 0
    try:
        with open("/tmp/fp-disk", "wb", buffering=0) as handle:
            while written < target:
                count = handle.write(chunk)
                if not count:
                    errno = 28
                    break
                written += count
            try:
                os.fsync(handle.fileno())
            except OSError as exc:
                if errno == 0:
                    errno = exc.errno or 0
    except OSError as exc:
        errno = exc.errno or 0
    try:
        size = os.path.getsize("/tmp/fp-disk")
    except OSError:
        size = -1
    print(f"RESULT disk_errno={errno} written={written} size={size}")


def _tmp_mount() -> str:
    try:
        with open("/proc/mounts", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return "unreadable"
    for line in lines:
        parts = line.split()
        if len(parts) >= 4 and parts[1] == "/tmp":
            options = parts[3].replace(",", "_")
            return f"{parts[2]}:{options}"
    return "missing"


def pids() -> None:
    live: list[int] = []
    stop = "none"
    for _ in range(48):
        try:
            pid = os.fork()
        except OSError as exc:
            stop = str(exc.errno)
            break
        if pid == 0:
            time.sleep(120)
            os._exit(0)
        live.append(pid)
    print(f"RESULT children={len(live)} stop_errno={stop}")
    for pid in live:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass


def cpu() -> None:
    path = "/sys/fs/cgroup/cpu.max"
    if not os.path.exists(path):
        for root, _dirs, files in os.walk("/sys/fs/cgroup"):
            if "cpu.max" in files:
                path = os.path.join(root, "cpu.max")
                break
        else:
            path = ""
    if path:
        with open(path, encoding="utf-8") as handle:
            value = handle.read().strip().replace(" ", "_")
        print(f"RESULT cpu_max={value}")
    else:
        print("RESULT cpu_max=missing")
    quota = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
    if os.path.exists(quota):
        with open(quota, encoding="utf-8") as handle:
            value = handle.read().strip()
        print(f"RESULT cpu_quota={value}")


def memory() -> None:
    chunks: list[bytearray] = []
    for index in range(1, 257):
        block = bytearray(1024 * 1024)
        block[0] = 1
        block[-1] = 1
        chunks.append(block)
        if index % 16 == 0:
            print(f"ALLOC {index}", flush=True)
    print("ALLOC_DONE", flush=True)


def emit_bad() -> None:
    sys.stdout.write(
        '{"status":"passed","selector":"' + SELECTOR + '","verified":true}'
    )


def emit_big() -> None:
    sys.stdout.write(
        '{"status":"passed","selector":"' + SELECTOR + '","pad":"' + ("a" * 9000) + '"}'
    )


def emit_malformed() -> None:
    sys.stdout.write("{not-json")


def emit_ok() -> None:
    sys.stdout.write('{"status":"passed","selector":"' + SELECTOR + '"}')


def tenant_write() -> None:
    payload = b"tenant-a-marker\n"
    with open("/data/marker", "wb") as handle:
        handle.write(payload)
    os.chmod("/data/marker", 0o644)
    digest = hashlib.sha256(payload).hexdigest()
    print(f"RESULT wrote=1 sha256={digest}")


def tenant_read() -> None:
    for label, path in (
        ("own", "/data/marker"),
        ("other", "/tmp/fp-tenant-a/marker"),
        ("host", "/host-a/marker"),
    ):
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            print(f"RESULT {label}=errno_{exc.errno}")
        else:
            print(f"RESULT {label}=bytes_{len(data)}")


if __name__ == "__main__":
    main()
