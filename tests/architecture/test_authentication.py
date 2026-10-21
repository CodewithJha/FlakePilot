import ast
from pathlib import Path

from flakepilot.audit.infrastructure.tables import metadata as aud_metadata
from flakepilot.diagnosis.infrastructure.tables import metadata as diag_metadata
from flakepilot.execution.infrastructure.tables import metadata as exec_metadata
from flakepilot.identity.infrastructure.tables import metadata as iam_metadata
from flakepilot.integrations.infrastructure.secret_store import (
    metadata as secret_store_metadata,
)
from flakepilot.orchestration.infrastructure.tables import metadata as orch_metadata
from flakepilot.repository.infrastructure.tables import metadata as repo_metadata
from flakepilot.test_intelligence.infrastructure.tables import metadata as ti_metadata

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


def test_interface_never_supplies_code_host_access() -> None:
    """Code-host permission comes from REPO (`CodeHostAccessVerifier`, DEPENDENCY-RULES §4),
    never from a value the interface or composition root chooses, such as a literal READ."""
    uses = [
        f"{path.name}:{node.lineno}"
        for path, tree in _modules(SRC / "interface")
        for node in ast.walk(tree)
        if (isinstance(node, ast.Name) and node.id == "CodeHostAccess")
        or (isinstance(node, ast.Attribute) and node.attr == "CodeHostAccess")
        or (
            isinstance(node, ast.ImportFrom)
            and any(alias.name == "CodeHostAccess" for alias in node.names)
        )
    ]
    assert uses == []


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
    assert (
        iam_metadata.naming_convention
        == secret_store_metadata.naming_convention
        == ti_metadata.naming_convention
        == diag_metadata.naming_convention
        == orch_metadata.naming_convention
        == aud_metadata.naming_convention
        == repo_metadata.naming_convention
        == exec_metadata.naming_convention
    )


def test_xml_parsing_lives_only_in_integration_adapters() -> None:
    """Untrusted XML is parsed only by the hardened INT report adapter (ADR-009)."""
    allowed = SRC / "integrations" / "infrastructure"
    imports = [
        f"{path.relative_to(SRC)}:{node.lineno}"
        for path, tree in _modules(SRC)
        if allowed not in path.parents
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Import)
            and any(a.name.split(".")[0] == "xml" for a in node.names)
        )
        or (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").split(".")[0] == "xml"
        )
    ]
    assert imports == []
