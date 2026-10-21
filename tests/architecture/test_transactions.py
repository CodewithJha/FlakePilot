import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"


def test_no_code_commits_or_rolls_back_explicitly() -> None:
    """Transactions end only through `transaction()` (session.begin()), never by hand.

    Repositories and use cases must not call `.commit()` or `.rollback()`.
    """
    calls = [
        f"{path.relative_to(SRC)}:{node.lineno}"
        for path in SRC.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"commit", "rollback"}
    ]
    assert calls == []
