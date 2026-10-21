import ast
from pathlib import Path

from flakepilot.identity.infrastructure.tables import metadata as iam_metadata
from flakepilot.integrations.infrastructure.secret_store import (
    metadata as secret_store_metadata,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "flakepilot"


def _modules(root: Path) -> list[tuple[Path, ast.AST]]:
    return [(path, ast.parse(path.read_text())) for path in root.rglob("*.py")]


def test_interface_never_builds_access_from_request_input() -> None:
    """`OrgAccess` comes from IAM after verified authentication, never from the interface."""
    calls = [
        f"{path.name}:{node.lineno}"
        for path, tree in _modules(SRC / "interface")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", None)) == "OrgAccess"
    ]
    assert calls == []


def test_outbound_http_lives_only_in_integration_adapters() -> None:
    """urllib.request (outbound HTTP) only in INT infrastructure (ADR-003)."""
    allowed = SRC / "integrations" / "infrastructure"
    imports = [
        f"{path.relative_to(SRC)}:{node.lineno}"
        for path, tree in _modules(SRC)
        if allowed not in path.parents
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Import)
            and any(a.name.startswith("urllib.request") for a in node.names)
        )
        or (isinstance(node, ast.ImportFrom) and node.module == "urllib.request")
    ]
    assert imports == []


def test_every_context_uses_the_same_constraint_naming_convention() -> None:
    assert iam_metadata.naming_convention == secret_store_metadata.naming_convention
