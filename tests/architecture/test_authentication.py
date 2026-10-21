import ast
from pathlib import Path

INTERFACE = Path(__file__).resolve().parents[2] / "src" / "flakepilot" / "interface"


def test_interface_never_builds_access_from_request_input() -> None:
    """`OrgAccess` comes from IAM after verified authentication, never from the interface.

    Until authentication exists, no interface module may construct it at all.
    """
    calls = [
        f"{path.name}:{node.lineno}"
        for path in INTERFACE.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", None)) == "OrgAccess"
    ]
    assert calls == []
