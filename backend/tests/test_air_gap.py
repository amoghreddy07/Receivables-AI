"""AIR GAP: the diagnosis (AI) package must not be able to reach execution.

Two independent proofs:
  1. STATIC — an AST scan of every source file under app/diagnosis asserts no
     import of app.execution, execution, razorpay, or any HTTP client.
  2. RUNTIME — importing the whole diagnosis package must not pull
     app.execution or razorpay into sys.modules.
"""
from __future__ import annotations

import ast
import importlib
import pkgutil
import sys
from pathlib import Path

import app.diagnosis as diagnosis_pkg
import app.execution

BACKEND = Path(app.execution.__file__).resolve().parents[2]  # .../backend (repo root dir of the app package)
FORBIDDEN = {
    "app.execution",
    "app.execution.",
    "execution",
    "razorpay",
    "requests",
    "httpx",
    "urllib",
    "socket",
    "http.client",
    "aiohttp",
}


def _iter_py_files(pkg) -> list[Path]:
    root = Path(pkg.__file__).resolve().parent
    files = []
    for m in pkgutil.walk_packages([str(root)], prefix=f"{pkg.__name__}."):
        spec = importlib.util.find_spec(m.name)
        if spec and spec.origin and spec.origin.endswith(".py"):
            files.append(Path(spec.origin))
    return files


def _imported_names(file: Path) -> list[str]:
    tree = ast.parse(file.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
            if node.level:  # relative import from within app.diagnosis
                names.append("app." + node.module if node.level == 1 else node.module)
    return names


def test_static_air_gap_import_scan():
    files = _iter_py_files(diagnosis_pkg)
    assert files, "no diagnosis source files found"
    violations = []
    for f in files:
        for name in _imported_names(f):
            for bad in FORBIDDEN:
                if name == bad or name.startswith(bad):
                    violations.append((str(f), name))
    assert not violations, f"diagnosis imports execution/SDK: {violations}"


def _clean_subprocess(imports: str, bad_prefix: tuple[str, ...]) -> str:
    """Run the given imports in a fresh interpreter; return modules matching
    the forbidden prefixes that got pulled in."""
    import os
    import subprocess

    code = (
        "import sys; "
        f"{imports}; "
        f"bad = [m for m in sys.modules if m.startswith({bad_prefix!r})]; "
        "print('|'.join(bad))"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND)
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(BACKEND),
        env=env,
    )
    if result.returncode != 0:  # pragma: no cover - diagnostic
        print(f"DBG backend={BACKEND!r} cwd={os.getcwd()!r} py={sys.executable!r} err={result.stderr!r}")
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_runtime_import_does_not_pull_execution():
    """In a clean interpreter, importing the full diagnosis package must not
    load app.execution or the razorpay SDK (proves the layering at runtime)."""
    pulled = _clean_subprocess(
        "import app.diagnosis.rules_classifier, app.diagnosis.rules_map, "
        "app.diagnosis.fusion, app.diagnosis.llm_diagnoser, app.diagnosis.context_builder",
        ("app.execution", "razorpay"),
    )
    assert pulled == "", f"diagnosis pulled execution/SDK into sys.modules: {pulled}"


def test_positive_control_execution_imports_diagnosis_not_required():
    """Positive control: importing execution must not pull in diagnosis."""
    pulled = _clean_subprocess(
        "import app.execution.executors, app.execution.sandbox, app.execution.razorpay_client",
        ("app.diagnosis",),
    )
    assert pulled == "", f"execution pulled diagnosis into sys.modules: {pulled}"
