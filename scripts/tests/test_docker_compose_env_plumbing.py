"""Pin docker-compose.yml api service env_file to forward UW_TOKEN.

Background: src/xenon/api/server.py:547-549 emits
"UW_TOKEN not set — UW-dependent endpoints will fail" when UW_TOKEN is
absent from os.environ at boot, and disables the shared UWClient. Per
CLAUDE.md, UW_TOKEN lives in web/.env (not the root .env). The in-process
load_dotenv at server.py:81-82 covers source-run FastAPI but is a silent
no-op inside the api container because docker/api.Dockerfile does not ship
the web/ tree. The only working channel inside Docker is Compose-injected
env, so the api service must list both env files.

Order matters: Compose applies later env_file entries with higher precedence.
./web/.env must come AFTER ./.env so any future overlapping key resolves to
the web/.env value. Reversing the order would silently invert this without
any test signal — that's the regression this test exists to catch.

./web/.env on the api service is also marked `required: false` defensively,
so the api block parses cleanly if web/.env is absent. This does NOT make
the full stack runnable without web/.env — the `web` service still requires
it — but it keeps the api block tolerant in isolation.

See docs/plans/2026-05-04-docker-uw-token-plumbing.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"


@dataclass(frozen=True)
class EnvFileEntry:
    path: str
    required: bool  # Compose default is True


def _service_env_file_entries(compose_text: str, service_name: str) -> list[EnvFileEntry]:
    """Extract the env_file list for a top-level service from compose YAML.

    Walks the file with awareness of just enough YAML structure to find
    `<service_name>:` at indent 2, then `env_file:` at indent 4, then
    collects the dash-prefixed list items at indent 6 until the next
    same-or-shallower key.

    Recognizes both Compose env_file forms:
      - ``- ./path``                              (string form, required by default)
      - ``- path: ./path``                        (object form, required by default)
        ``  required: false``                     (continuation at indent 8)

    Avoids a PyYAML dependency for a single assertion.
    """
    lines = compose_text.splitlines()
    in_service = False
    in_env_file = False
    entries: list[EnvFileEntry] = []
    current_path: str | None = None
    current_required: bool = True
    for raw in lines:
        stripped = raw.strip()
        if not in_service:
            if raw == f"  {service_name}:":
                in_service = True
            continue
        leading = len(raw) - len(raw.lstrip(" "))
        if stripped == "" or raw.lstrip().startswith("#"):
            continue
        if leading <= 2 and stripped.endswith(":") and not in_env_file:
            break  # next top-level service
        if not in_env_file:
            if leading == 4 and stripped == "env_file:":
                in_env_file = True
            continue
        # Inside env_file list.
        if leading == 6 and stripped.startswith("- "):
            # Flush previous object-form entry if any.
            if current_path is not None:
                entries.append(EnvFileEntry(current_path, current_required))
                current_path = None
                current_required = True
            body = stripped[2:].strip()
            if body.startswith("path:"):
                # Object-form item start: "- path: ./web/.env"
                current_path = body[len("path:") :].strip()
                current_required = True
            else:
                # String-form item: "- ./.env"
                entries.append(EnvFileEntry(body, required=True))
            continue
        if leading == 8 and current_path is not None and "required:" in stripped:
            # Continuation of object-form entry.
            value = stripped.split("required:", 1)[1].strip().lower()
            current_required = value not in {"false", "no", "off", "0"}
            continue
        # Any other line at indent <=4 ends the env_file list.
        if leading <= 4:
            break
    if current_path is not None:
        entries.append(EnvFileEntry(current_path, current_required))
    return entries


def _paths(entries: list[EnvFileEntry]) -> list[str]:
    return [e.path for e in entries]


def test_compose_file_exists() -> None:
    assert COMPOSE_PATH.is_file(), f"missing {COMPOSE_PATH}"


def test_api_service_loads_root_and_web_env_files_in_order() -> None:
    """api service must load ./.env then ./web/.env, in that exact order.

    Without ./web/.env the FastAPI container boots without UW_TOKEN and every
    UW-backed endpoint returns 503. Reversing the order silently changes
    Compose precedence semantics for any overlapping key.
    """
    text = COMPOSE_PATH.read_text()
    paths = _paths(_service_env_file_entries(text, "api"))
    assert paths == ["./.env", "./web/.env"], (
        "docker-compose.yml api.env_file must be exactly "
        "['./.env', './web/.env'] in that order. Got: "
        f"{paths!r}. See docs/plans/2026-05-04-docker-uw-token-plumbing.md."
    )


def test_api_web_env_file_marked_optional() -> None:
    """./web/.env on the api service must be `required: false`.

    Defensive: keeps the api service block parsing cleanly when web/.env is
    absent. The web service's own env_file dependency on ./web/.env still
    blocks full-stack `compose up` without the file — but the api block
    shouldn't fail for a file it merely opportunistically reads.
    """
    text = COMPOSE_PATH.read_text()
    entries = _service_env_file_entries(text, "api")
    web_entries = [e for e in entries if e.path == "./web/.env"]
    assert web_entries, f"./web/.env not found in api env_file: {entries!r}"
    assert not web_entries[0].required, (
        "api.env_file entry for ./web/.env must set `required: false` so "
        "API-only deployments without web/.env still boot. Got: "
        f"{web_entries[0]!r}."
    )


def test_web_service_still_loads_web_env_file() -> None:
    """Sanity: don't accidentally break the web service while editing api."""
    text = COMPOSE_PATH.read_text()
    paths = _paths(_service_env_file_entries(text, "web"))
    assert "./web/.env" in paths, f"web service must still list ./web/.env in env_file. Got: {paths!r}"


@pytest.mark.parametrize("service", ["realtime", "migrator"])
def test_other_services_unchanged(service: str) -> None:
    """Out-of-scope guard: only api should pull web/.env.

    The plan deliberately scopes the change to the api service. If a future
    edit broadens this without updating the plan and least-privilege
    discussion, this test fails and forces a re-decision.
    """
    text = COMPOSE_PATH.read_text()
    paths = _paths(_service_env_file_entries(text, service))
    assert paths == ["./.env"], (
        f"{service} service env_file expected to be ['./.env']; got "
        f"{paths!r}. If you intentionally added web/.env here, update "
        "docs/plans/2026-05-04-docker-uw-token-plumbing.md and this test."
    )
