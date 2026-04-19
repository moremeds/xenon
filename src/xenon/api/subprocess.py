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
from typing import Any, List, Optional

logger = logging.getLogger("xenon.subprocess")

# run_entry_point() invokes installed .venv/bin/xenon-* console entry points directly.
# SCRIPTS_DIR is retained as the default cwd for subprocesses (config files, relative paths
# in data/ still resolve from there).
PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
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


@dataclass
class ScriptResult:
    ok: bool
    data: Optional[dict] = None
    error: Optional[str] = None
    exit_code: Optional[int] = None


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
        cwd: Working directory (defaults to scripts/)

    Returns:
        ScriptResult with parsed JSON data or error string.
    """
    entry_path = VENV_BIN / entry
    if not entry_path.exists():
        return ScriptResult(ok=False, error=f"Entry point not found: {entry}")

    cmd = [str(entry_path)] + (args or [])
    work_dir = cwd or str(SCRIPTS_DIR)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
        )

        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            err_msg = _extract_error_message(
                stdout,
                stderr,
                f"Entry point exited with code {proc.returncode}",
            )
            logger.warning("Entry point %s failed (code %d): %s", entry, proc.returncode, err_msg)
            return ScriptResult(ok=False, error=err_msg, exit_code=proc.returncode)

        json_start = stdout.find("{")
        if json_start == -1:
            return ScriptResult(ok=True, data={})

        data = json.loads(stdout[json_start:])
        return ScriptResult(ok=True, data=data)

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
            cwd=str(SCRIPTS_DIR),
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

        data = json.loads(stdout[json_start:])
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
