"""Async subprocess helper for running Python scripts from FastAPI.

Replaces the Node.js spawn pattern in runner.ts with asyncio subprocesses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("xenon.subprocess")

# run_entry_point() invokes installed .venv/bin/xenon-* console entry points directly.
# Default cwd is PROJECT_ROOT — the only directory guaranteed to exist in every deploy
# (the Docker image ships src/, .venv/, data/ but not scripts/). Any relative path used
# by an entry point — e.g. `Path("data/foo")` — resolves correctly from there.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
VENV_BIN = PROJECT_ROOT / ".venv" / "bin"


def _extract_error_message(stdout: str, stderr: str, default: str) -> str:
    """Prefer the last meaningful stderr line, then stdout, then the default."""
    for stream in (stderr, stdout):
        lines = [
            l for l in stream.strip().split("\n") if l and "warnings.warn(" not in l and "NotOpenSSLWarning" not in l
        ]
        if lines:
            err_msg = lines[-1]
            try:
                parsed = json.loads(err_msg)
                if isinstance(parsed, dict):
                    err_msg = parsed.get("detail") or parsed.get("message") or parsed.get("error") or err_msg
            except Exception:
                pass
            if len(err_msg) > 300:
                err_msg = err_msg[:300] + "..."
            return err_msg
    return default


def _last_line_json(stdout: str) -> Optional[dict]:
    """The last non-empty stdout line, if it is a JSON object (else None)."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        if not line.startswith("{"):
            return None
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            return None
        return candidate if isinstance(candidate, dict) else None
    return None


def _parse_stdout_json(stdout: str) -> Optional[dict]:
    """JSON payload of a script's stdout: from the first "{" to the end; when
    that fails or is not an object, the last non-empty line if it is a JSON
    object. Progress text may contain "{" (e.g. SQLAlchemy "[parameters: {…}]"
    in a warning) before a script's final one-line JSON result."""
    json_start = stdout.find("{")
    if json_start == -1:
        return None
    try:
        candidate = json.loads(stdout[json_start:])
    except json.JSONDecodeError:
        candidate = None
    if isinstance(candidate, dict):
        return candidate
    return _last_line_json(stdout)


@dataclass
class ScriptResult:
    ok: bool
    data: Optional[dict] = None
    error: Optional[str] = None
    exit_code: Optional[int] = None
    # False only when the caller refused BEFORE starting the subprocess
    # (cooldown, gateway down, send window closed): nothing reached IB.
    spawned: bool = True


async def run_entry_point(
    entry: str,
    args: Optional[List[str]] = None,
    timeout: float = 30.0,
    cwd: Optional[str] = None,
) -> ScriptResult:
    """Run a xenon-* console entry-point binary from .venv/bin/ as an async subprocess.

    Invokes the installed entry-point binary directly. The entry-point is defined
    in pyproject.toml [project.scripts].

    Args:
        entry: Entry-point name (e.g. "xenon-trend-scan"), must exist in .venv/bin/
        args: CLI arguments
        timeout: Seconds before SIGKILL
        cwd: Working directory (defaults to PROJECT_ROOT)

    Returns:
        ScriptResult with parsed JSON data or error string.
    """
    entry_path = VENV_BIN / entry
    if not entry_path.exists():
        return ScriptResult(ok=False, error=f"Entry point not found: {entry}", spawned=False)

    cmd = [str(entry_path)] + (args or [])
    work_dir = cwd or str(PROJECT_ROOT)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
        )
    except OSError as e:
        # The process never started (exec failure): nothing reached IB.
        logger.error("Entry point %s failed to spawn: %s", entry, e)
        return ScriptResult(ok=False, error=f"Entry point failed to start: {e}", spawned=False)

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        parsed_data = _parse_stdout_json(stdout)

        if proc.returncode != 0:
            # Structured subprocess errors emit JSON with ``status`` and
            # classification on stdout but exit non-zero (convention:
            # exit(1) on error, exit(0) on success). Surface the parsed
            # payload so the route can classify — otherwise every
            # classified failure collapses to a generic 503.
            if parsed_data is not None and "status" in parsed_data:
                return ScriptResult(ok=True, data=parsed_data, exit_code=proc.returncode)

            err_msg = _extract_error_message(
                stdout,
                stderr,
                f"Entry point exited with code {proc.returncode}",
            )
            logger.warning("Entry point %s failed (code %d): %s", entry, proc.returncode, err_msg)
            return ScriptResult(ok=False, error=err_msg, exit_code=proc.returncode)

        if parsed_data is None:
            return ScriptResult(ok=True, data={})
        return ScriptResult(ok=True, data=parsed_data)

    except asyncio.TimeoutError:
        logger.error("Entry point %s timed out after %.0fs", entry, timeout)
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return ScriptResult(ok=False, error=f"Entry point timed out after {timeout}s")

    except json.JSONDecodeError as e:
        logger.error("Entry point %s returned invalid JSON: %s", entry, e)
        return ScriptResult(ok=False, error=f"Invalid JSON output: {e}")

    except Exception as e:
        logger.error("Entry point %s error: %s", entry, e)
        return ScriptResult(ok=False, error=str(e))


async def run_module(
    module: str,
    args: Optional[List[str]] = None,
    timeout: float = 30.0,
) -> ScriptResult:
    """Run a Python module (-m) as an async subprocess.

    For scripts invoked as `python3 -m trade_blotter.flex_query --json`.
    """
    cmd = [sys.executable, "-m", module] + (args or [])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(PROJECT_ROOT),
        )

        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            err_msg = _extract_error_message(
                stdout,
                stderr,
                f"Module exited with code {proc.returncode}",
            )
            return ScriptResult(ok=False, error=err_msg, exit_code=proc.returncode)

        json_start = stdout.find("{")
        if json_start == -1:
            return ScriptResult(ok=True, data={})

        try:
            data = json.loads(stdout[json_start:])
        except json.JSONDecodeError:
            # Noise with "{" before a final one-line JSON result.
            data = _last_line_json(stdout)
            if data is None:
                raise
        else:
            if not isinstance(data, dict):
                data = _last_line_json(stdout) or data
        return ScriptResult(ok=True, data=data)

    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return ScriptResult(ok=False, error=f"Module timed out after {timeout}s")

    except json.JSONDecodeError as e:
        return ScriptResult(ok=False, error=f"Invalid JSON output: {e}")

    except Exception as e:
        return ScriptResult(ok=False, error=str(e))
