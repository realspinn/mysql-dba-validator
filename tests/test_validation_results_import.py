"""The connector builds results without importing the web app (backend.main).

backend.main runs the web app's startup when imported: load_dotenv(), the
MDV_PUBLIC_DEMO / RENDER / CORS checks (which raise), the FastAPI app, its middleware
and the static mount. The shared result construction lives in
backend.validation_results so company validation does not depend on any of that.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_connector_code_never_imports_the_web_app():
    for path in sorted((ROOT / "connector").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [f"{node.module}.{alias.name}" for alias in node.names]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                continue
            assert all(name != "backend.main" for name in names), f"{path.name}:{node.lineno}"


def test_shared_results_import_without_the_web_app_or_its_settings():
    # A fresh interpreter, with a setting that makes backend.main refuse to import.
    code = (
        "import sys\n"
        "import connector.server\n"
        "from backend.validation_results import batch_result, build_statement_result, script_error\n"
        "assert 'backend.main' not in sys.modules\n"
        "try:\n"
        "    import backend.main\n"
        "except RuntimeError:\n"
        "    print('web app refused')\n"
    )
    env = {**os.environ, "MDV_PUBLIC_DEMO": "invalid"}
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "web app refused"
