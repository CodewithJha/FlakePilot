"""Frame codec checks. No socket is opened and no host is contacted."""

import pytest

from flakepilot.execution.application.frame import (
    FrameRejected,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)

EXEC = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
MESSAGE = f"register {EXEC} 1700000030".encode()
MAC = "ab" * 32


def test_a_request_round_trips_without_extra_fields() -> None:
    frame = encode_request("register", MESSAGE, MAC)
    assert decode_request(frame) == ("register", MESSAGE, MAC)
    assert decode_response(encode_response("ok")) == "ok"
    assert decode_response(encode_response("rejected")) == "rejected"


def test_bounds_and_mismatches_are_rejected() -> None:
    frame = encode_request("register", MESSAGE, MAC)
    with pytest.raises(FrameRejected):
        decode_request(frame + b"x")
    with pytest.raises(FrameRejected):
        decode_request(frame[:-1])
    with pytest.raises(FrameRejected):
        decode_request((9999).to_bytes(4, "big") + b"x")
    with pytest.raises(FrameRejected):
        encode_request("register", b"cancel " + EXEC.encode(), MAC)
    with pytest.raises(FrameRejected):
        encode_request("cleanup", b"cleanup " + EXEC.encode(), MAC)
    with pytest.raises(FrameRejected):
        encode_request("rm", b"rm -rf /", MAC)
    with pytest.raises(FrameRejected):
        encode_response("secret")
    assert str(FrameRejected()) == "rejected"
