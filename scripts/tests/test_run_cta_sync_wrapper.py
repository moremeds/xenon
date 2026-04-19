from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_run_cta_sync_preserves_literal_env_values(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    services_dir = repo_dir / "scripts" / "services"
    web_dir = repo_dir / "web"
    data_dir = repo_dir / "data" / "menthorq_cache"
    logs_dir = repo_dir / "logs"
    venv_bin_dir = repo_dir / ".venv" / "bin"
    bin_dir = tmp_path / "bin"
    output_path = tmp_path / "captured-env.json"

    services_dir.mkdir(parents=True)
    web_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    logs_dir.mkdir(parents=True)
    venv_bin_dir.mkdir(parents=True)
    bin_dir.mkdir(parents=True)

    wrapper_src = Path(__file__).resolve().parents[1] / "services" / "run_cta_sync.sh"
    wrapper_dst = services_dir / "run_cta_sync.sh"
    shutil.copy2(wrapper_src, wrapper_dst)
    wrapper_dst.chmod(wrapper_dst.stat().st_mode | stat.S_IXUSR)

    literal_user = "cta-user@example.com"
    literal_pass = r"Abc$HOME!xyz%42"

    (repo_dir / ".env").write_text(
        f"MENTHORQ_USER={literal_user}\nMENTHORQ_PASS={literal_pass}\n",
        encoding="utf-8",
    )
    (web_dir / ".env").write_text("", encoding="utf-8")

    # Stub xenon-cta-sync-service entry point: capture env vars to disk.
    _write_executable(
        venv_bin_dir / "xenon-cta-sync-service",
        "\n".join(
            [
                "#!/bin/bash",
                f"cat > {output_path!s} <<EOF",
                '{"MENTHORQ_USER": "$MENTHORQ_USER", "MENTHORQ_PASS": "$MENTHORQ_PASS"}',
                "EOF",
            ]
        ),
    )

    _write_executable(
        bin_dir / "python3.13",
        "\n".join(
            [
                "#!/bin/bash",
                'if [ "$1" = "-" ]; then',
                "  cat >/dev/null",
                "  exit 0",
                "fi",
                'exec /usr/bin/env python3 "$@"',
            ]
        ),
    )

    env = {
        **os.environ,
        "XENON_PYTHON_BIN": str(bin_dir / "python3.13"),
    }
    result = subprocess.run(
        ["bash", str(wrapper_dst), "--source", "test"],
        cwd=repo_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    captured = json.loads(output_path.read_text(encoding="utf-8"))
    assert captured["MENTHORQ_USER"] == literal_user
    assert captured["MENTHORQ_PASS"] == literal_pass
