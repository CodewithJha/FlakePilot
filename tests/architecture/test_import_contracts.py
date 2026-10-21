import ast
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "pyproject.toml"
PACKAGES = {
    "INT": "integrations",
    "IAM": "identity",
    "REPO": "repository",
    "AUD": "audit",
    "EXEC": "execution",
    "TI": "test_intelligence",
    "DIAG": "diagnosis",
    "PATCH": "patches",
    "VER": "verification",
    "APPR": "approval",
    "ORCH": "orchestration",
}


def lint_imports(src: Path, cwd: Path) -> str:
    result = subprocess.run(
        [
            Path(sys.executable).with_name("lint-imports"),
            "--config",
            CONFIG,
            "--no-cache",
        ],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(src)},
        capture_output=True,
        text=True,
        check=False,
    )
    output = " ".join((result.stdout + result.stderr).split())
    return f"exit={result.returncode} {output}"


def test_contracts_are_kept() -> None:
    output = lint_imports(ROOT / "src", ROOT)
    assert output.startswith("exit=0 "), output
    assert "0 broken" in output, output


def test_forbidden_import_is_caught_directly_and_indirectly(tmp_path: Path) -> None:
    src = tmp_path / "src"
    shutil.copytree(ROOT / "src", src, ignore=shutil.ignore_patterns("__pycache__"))
    pkg = src / "flakepilot"
    # TI -> EXEC is a permitted edge; EXEC -> DIAG is not.
    (pkg / "test_intelligence/application/probe.py").write_text(
        "import flakepilot.execution.application.probe\n"
    )
    (pkg / "execution/application/probe.py").write_text(
        "import flakepilot.diagnosis.application\n"
    )

    output = lint_imports(src, tmp_path)

    assert output.startswith("exit=1 "), output
    assert "Dependency matrix: execution BROKEN" in output, output
    assert "Dependency matrix: test_intelligence KEPT" in output, output
    assert "Diagnosis isolation" in output and "2 broken" in output, output
    assert (
        "flakepilot.test_intelligence.application.probe -> "
        "flakepilot.execution.application.probe (l.1) "
        "flakepilot.execution.application.probe -> flakepilot.diagnosis.application (l.1)"
    ) in output, output


def test_matrix_contracts_match_dependency_rules() -> None:
    doc = (ROOT / "docs/architecture/DEPENDENCY-RULES.md").read_text()
    rows = [
        line.strip("|").split("|") for line in doc.splitlines() if line.startswith("|")
    ]
    header = next(row for row in rows if "depends on" in row[0])
    columns = [cell.strip() for cell in header[1:]]
    allowed: dict[str, set[str]] = {}
    for row in rows:
        match = re.fullmatch(r"\*\*(\w+)\*\*", row[0].strip())
        if match and len(row) == len(columns) + 1:
            allowed[PACKAGES[match[1]]] = {
                PACKAGES[code]
                for code, cell in zip(columns, row[1:], strict=True)
                if "✓" in cell
            }

    contracts = tomllib.loads(CONFIG.read_text())["tool"]["importlinter"]["contracts"]
    forbidden = {
        c["source_modules"][0].removeprefix("flakepilot."): {
            m.removeprefix("flakepilot.") for m in c["forbidden_modules"]
        }
        for c in contracts
        if c["name"].startswith("Dependency matrix: ")
    }

    assert set(allowed) == set(forbidden) == set(PACKAGES.values())
    assert sum(map(len, allowed.values())) == 36
    for context, deps in allowed.items():
        assert forbidden[context] == set(PACKAGES.values()) - deps - {context}, context
    assert sum(map(len, forbidden.values())) == 74


def test_shared_kernel_imports_only_the_standard_library() -> None:
    """DEPENDENCY-RULES §6: no business rules, no I/O, no vendor types (ADR-012)."""
    kernel = ROOT / "src" / "flakepilot" / "shared_kernel"
    imported = {
        name.split(".")[0]
        for path in kernel.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        for name in (
            [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
            if isinstance(node, ast.ImportFrom) and node.level == 0
            else []
        )
    }
    io = {"asyncio", "http", "io", "os", "shutil", "socket", "subprocess", "urllib"}
    assert imported, "the shared kernel has modules"
    assert imported <= sys.stdlib_module_names - io
