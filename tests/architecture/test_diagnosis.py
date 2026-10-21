"""Production wiring stays on the null assist adapter."""

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "flakepilot" / "interface"


def test_interface_wires_null_assist_and_not_the_fake() -> None:
    source = "\n".join(path.read_text() for path in SRC.rglob("*.py"))
    assert "NullAiAssist" in source
    assert "FakeAiAssist" not in source
