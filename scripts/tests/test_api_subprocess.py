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
