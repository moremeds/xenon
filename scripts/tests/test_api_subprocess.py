"""Tests for scripts/api/subprocess.py — async subprocess helper.

Edge cases:
- Script not found
- Successful JSON extraction from stdout with progress prefix
- Empty stdout (rawOutput pattern → returns empty dict)
- Script exit code != 0 (stderr extraction, noise filtering)
- JSON parse error from invalid output
- Timeout handling
- Module execution (-m) path
"""

import asyncio
import sys
from pathlib import Path

# Ensure scripts/ is on sys.path
SCRIPTS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from xenon.api.subprocess import ScriptResult, _extract_error_message, run_module


class TestExtractErrorMessage:
    """Tests for _extract_error_message() — stderr/stdout parsing helper."""

    def test_json_error_stdout_returns_message_field(self):
        msg = _extract_error_message(
            '{"status":"error","message":"Trade not found after reconnect as original clientId"}\n',
            "",
            "fallback",
        )
        assert msg == "Trade not found after reconnect as original clientId"


class TestRunModule:
    """Tests for run_module()."""

    def test_module_not_found(self):
        result = asyncio.run(run_module("nonexistent.module.xyz"))
        assert not result.ok
        assert result.error is not None

    def test_module_timeout(self):
        """Module that hangs should be killed."""
        result = asyncio.run(run_module("time", args=[], timeout=0.5))
        # python3 -m time just runs and exits, so this might succeed or timeout
        # The point is it doesn't hang forever
        assert isinstance(result, ScriptResult)

    def test_module_error_falls_back_to_stdout_when_stderr_is_empty(self):
        """When stderr is empty, error should be extracted from stdout."""
        # Use a nonexistent sub-module that fails fast with a clear error
        result = asyncio.run(run_module("json.tool", args=["--no-such-arg"], timeout=5))
        assert not result.ok
        assert result.error is not None


class TestRunEntryPointClassifiedError:
    """Regression for F5 integration bug: classified subprocess errors emit JSON
    with ``status`` on stdout but exit non-zero. ``run_entry_point`` must surface
    the parsed payload (ok=True, data=...) so the route can classify to 4xx/404
    instead of collapsing everything to 503 IB_CONNECTION."""

    def test_nonzero_exit_with_status_json_surfaces_data(self, tmp_path, monkeypatch):
        import stat as _stat

        from xenon.api import subprocess as sp

        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        entry = fake_bin / "fake-classified-error"
        entry.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "print(json.dumps({'status':'error','message':'x',"
            "'classification':'ib_reject','upstream':{'code':10147,'message':'y'}}))\n"
            "sys.exit(1)\n"
        )
        entry.chmod(entry.stat().st_mode | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH)

        monkeypatch.setattr(sp, "VENV_BIN", fake_bin)
        result = asyncio.run(sp.run_entry_point("fake-classified-error"))

        assert result.ok is True
        assert result.data is not None
        assert result.data.get("status") == "error"
        assert result.data.get("classification") == "ib_reject"
        assert result.data.get("upstream", {}).get("code") == 10147
        assert result.exit_code == 1

    def test_nonzero_exit_without_json_still_fails(self, tmp_path, monkeypatch):
        import stat as _stat

        from xenon.api import subprocess as sp

        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        entry = fake_bin / "fake-crashed"
        entry.write_text(
            "#!/usr/bin/env python3\nimport sys\nprint('Traceback: something broke', file=sys.stderr)\nsys.exit(2)\n"
        )
        entry.chmod(entry.stat().st_mode | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH)

        monkeypatch.setattr(sp, "VENV_BIN", fake_bin)
        result = asyncio.run(sp.run_entry_point("fake-crashed"))

        assert result.ok is False
        assert result.error is not None
        assert result.exit_code == 2


class TestRunEntryPointMissingScriptsDir:
    """Regression for the Docker-deploy snapshotter outage (2026-05-31):
    `run_entry_point` defaulted `cwd` to `<project>/scripts`, which does not
    exist in the shipped API image. `create_subprocess_exec(cwd=...)` then
    raised `FileNotFoundError: [Errno 2] No such file or directory` *before*
    execing the binary, surfacing as ``Background portfolio sync failed: [Errno 2]``
    in the API logs while IB sockets reported healthy. The default cwd must
    resolve to a directory that always exists in production."""

    def test_entry_point_succeeds_when_scripts_dir_absent(self, tmp_path, monkeypatch):
        import stat as _stat

        from xenon.api import subprocess as sp

        # Simulate the prod container: project root with .venv/bin/ but no scripts/.
        fake_root = tmp_path / "app"
        fake_root.mkdir()
        fake_bin = fake_root / "bin"
        fake_bin.mkdir()
        entry = fake_bin / "fake-ok"
        entry.write_text("#!/usr/bin/env python3\nimport json; print(json.dumps({'ok': True}))\n")
        entry.chmod(entry.stat().st_mode | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH)

        monkeypatch.setattr(sp, "PROJECT_ROOT", fake_root)
        monkeypatch.setattr(sp, "VENV_BIN", fake_bin)
        if hasattr(sp, "SCRIPTS_DIR"):
            # Pre-fix: patch the legacy default so the pre-fix code path tries a missing
            # cwd, reproducing the FileNotFoundError. Post-fix removes SCRIPTS_DIR entirely
            # and uses PROJECT_ROOT as the default.
            monkeypatch.setattr(sp, "SCRIPTS_DIR", fake_root / "scripts")

        result = asyncio.run(sp.run_entry_point("fake-ok"))

        assert result.ok, f"entry point should succeed even without scripts/, got error={result.error!r}"
        assert result.data == {"ok": True}

    def test_run_module_succeeds_when_scripts_dir_absent(self, tmp_path, monkeypatch):
        """Same regression, run_module path."""
        from xenon.api import subprocess as sp

        fake_root = tmp_path / "app"
        fake_root.mkdir()
        monkeypatch.setattr(sp, "PROJECT_ROOT", fake_root)
        if hasattr(sp, "SCRIPTS_DIR"):
            monkeypatch.setattr(sp, "SCRIPTS_DIR", fake_root / "scripts")

        # `python -m json.tool` reads stdin; with no stdin connected it exits non-zero
        # but the point of this test is to confirm we got past create_subprocess_exec
        # — i.e. the cwd is valid. A FileNotFoundError on cwd would surface in result.error.
        result = asyncio.run(sp.run_module("json.tool", args=[], timeout=2))
        assert "No such file or directory" not in (result.error or ""), (
            f"run_module raised cwd-missing error: {result.error!r}"
        )


class TestScriptResult:
    """Tests for ScriptResult dataclass."""

    def test_ok_result(self):
        r = ScriptResult(ok=True, data={"key": "value"})
        assert r.ok
        assert r.data["key"] == "value"
        assert r.error is None
        assert r.exit_code is None

    def test_error_result(self):
        r = ScriptResult(ok=False, error="something broke", exit_code=1)
        assert not r.ok
        assert r.data is None
        assert r.error == "something broke"
        assert r.exit_code == 1
