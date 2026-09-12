"""Keepalive CLI: one GET per entity, one line each, exit codes, no token text."""

import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from mercury_multiorg_mcp.client import MercuryClient
from mercury_multiorg_mcp.keepalive import EXIT_CONFIG, EXIT_FAIL, EXIT_OK, main

from .conftest import EXAMPLE_REGISTRY, FAKE_TOKEN_MAIN, FakeMercury

FAKE_TOKEN_OPS = "secret-token:mercury_test_fake_ops_ZYXWVUTS9876"
LINE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z (OK|FAIL) (\S+) (.*)$")


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
def fake_api():
    fake = FakeMercury()
    yield fake
    assert [r.method for r in fake.requests] == ["GET"] * len(fake.requests)


@pytest.fixture
def factory(fake_api):
    def make(token: str, api_base: str) -> MercuryClient:
        return MercuryClient(token, api_base=api_base, transport=httpx.MockTransport(fake_api.handler), sleep=_no_sleep)

    return make


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("MERCURY_ENTITIES_FILE", "MERCURY_API_BASE", "MERCURY_TOKEN_ACME_MAIN", "MERCURY_TOKEN_ACME_OPS"):
        monkeypatch.delenv(var, raising=False)


def _lines(capsys) -> tuple[list[str], str]:
    out, err = capsys.readouterr()
    lines = [ln for ln in out.splitlines() if ln]
    for ln in lines:
        assert LINE.match(ln), ln
        assert FAKE_TOKEN_MAIN not in ln and FAKE_TOKEN_OPS not in ln
    assert FAKE_TOKEN_MAIN not in err and FAKE_TOKEN_OPS not in err
    return lines, err


def test_all_ok(clean_env, monkeypatch, capsys, fake_api, factory):
    monkeypatch.setenv("MERCURY_TOKEN_ACME_MAIN", FAKE_TOKEN_MAIN)
    monkeypatch.setenv("MERCURY_TOKEN_ACME_OPS", FAKE_TOKEN_OPS)
    assert main(["--entities", str(EXAMPLE_REGISTRY)], client_factory=factory) == EXIT_OK
    lines, _ = _lines(capsys)
    assert [ln.split(" ", 1)[1] for ln in lines] == ["OK acme_main HTTP 200", "OK acme_ops HTTP 200"]
    # exactly one authenticated GET /accounts per entity, smallest page
    assert [r.url.path for r in fake_api.requests] == ["/api/v1/accounts", "/api/v1/accounts"]
    assert all(r.url.params["limit"] == "1" for r in fake_api.requests)
    assert fake_api.requests[0].headers["Authorization"] == f"Bearer {FAKE_TOKEN_MAIN}"
    assert fake_api.requests[1].headers["Authorization"] == f"Bearer {FAKE_TOKEN_OPS}"
    # default host when neither --api-base nor MERCURY_API_BASE is set
    assert all(str(r.url).startswith("https://api.mercury.com/api/v1/accounts?") for r in fake_api.requests)


def test_api_base_flag_reaches_the_request(clean_env, monkeypatch, capsys, fake_api, factory):
    monkeypatch.setenv("MERCURY_TOKEN_ACME_MAIN", FAKE_TOKEN_MAIN)
    monkeypatch.setenv("MERCURY_TOKEN_ACME_OPS", FAKE_TOKEN_OPS)
    monkeypatch.setenv("MERCURY_API_BASE", "https://api-sandbox.mercury.com")  # flag must win over the env var
    assert main(["--entities", str(EXAMPLE_REGISTRY), "--api-base", "http://127.0.0.1:8099"], client_factory=factory) == EXIT_OK
    lines, _ = _lines(capsys)
    assert len(lines) == 2
    assert all(str(r.url).startswith("http://127.0.0.1:8099/api/v1/accounts?") for r in fake_api.requests)


def test_api_base_env_var_reaches_the_request(clean_env, monkeypatch, capsys, fake_api, factory):
    monkeypatch.setenv("MERCURY_TOKEN_ACME_MAIN", FAKE_TOKEN_MAIN)
    monkeypatch.setenv("MERCURY_TOKEN_ACME_OPS", FAKE_TOKEN_OPS)
    monkeypatch.setenv("MERCURY_API_BASE", "https://api-sandbox.mercury.com")
    assert main(["--entities", str(EXAMPLE_REGISTRY)], client_factory=factory) == EXIT_OK
    _lines(capsys)
    assert all(str(r.url).startswith("https://api-sandbox.mercury.com/api/v1/accounts?") for r in fake_api.requests)


def test_one_entity_without_token_fails_but_others_still_run(clean_env, monkeypatch, capsys, fake_api, factory):
    monkeypatch.setenv("MERCURY_TOKEN_ACME_MAIN", FAKE_TOKEN_MAIN)
    assert main(["--entities", str(EXAMPLE_REGISTRY)], client_factory=factory) == EXIT_FAIL
    lines, _ = _lines(capsys)
    assert lines[0].endswith("OK acme_main HTTP 200")
    assert "FAIL acme_ops no token configured (MERCURY_TOKEN_ACME_OPS unset)" in lines[1]
    assert len(fake_api.requests) == 1


def test_http_error_is_a_fail_line_with_status(clean_env, monkeypatch, capsys, fake_api, factory):
    monkeypatch.setenv("MERCURY_TOKEN_ACME_MAIN", FAKE_TOKEN_MAIN)
    monkeypatch.setenv("MERCURY_TOKEN_ACME_OPS", FAKE_TOKEN_OPS)
    fake_api.force_status = 401
    fake_api.force_body = f"Unauthorized; header was Authorization: Bearer {FAKE_TOKEN_MAIN}"
    assert main(["--entities", str(EXAMPLE_REGISTRY)], client_factory=factory) == EXIT_FAIL
    lines, _ = _lines(capsys)
    assert [ln.split(" ", 1)[1] for ln in lines] == ["FAIL acme_main HTTP 401", "FAIL acme_ops HTTP 401"]


def test_transport_error_is_a_fail_line_without_token(clean_env, monkeypatch, capsys):
    monkeypatch.setenv("MERCURY_TOKEN_ACME_MAIN", FAKE_TOKEN_MAIN)
    monkeypatch.setenv("MERCURY_TOKEN_ACME_OPS", FAKE_TOKEN_OPS)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused; headers={{'Authorization': 'Bearer {FAKE_TOKEN_MAIN}'}}", request=request)

    def factory(token: str, api_base: str) -> MercuryClient:
        return MercuryClient(token, api_base=api_base, transport=httpx.MockTransport(boom), sleep=_no_sleep, max_retries=1)

    assert main(["--entities", str(EXAMPLE_REGISTRY)], client_factory=factory) == EXIT_FAIL
    lines, _ = _lines(capsys)
    assert len(lines) == 2
    for ln in lines:
        assert " FAIL " in ln and "refused" in ln and "[REDACTED]" in ln and "\n" not in ln


def test_zero_tokens_configured_is_a_failure(clean_env, capsys, fake_api, factory):
    assert main(["--entities", str(EXAMPLE_REGISTRY)], client_factory=factory) == EXIT_FAIL
    lines, _ = _lines(capsys)
    assert len(lines) == 1 and "FAIL (no entity has a token configured" in lines[0]
    assert fake_api.requests == []


def test_config_errors_exit_2(clean_env, capsys, tmp_path):
    assert main([]) == EXIT_CONFIG
    assert "--entities" in capsys.readouterr().err
    assert main(["--entities", str(tmp_path / "missing.yaml")]) == EXIT_CONFIG
    assert main(["--entities", str(EXAMPLE_REGISTRY), "--api-base", "http://api.mercury.com"]) == EXIT_CONFIG
    assert "https" in capsys.readouterr().err


def test_env_file_and_entities_env_var(clean_env, monkeypatch, capsys, tmp_path, fake_api, factory):
    dotenv = tmp_path / "private.env"
    dotenv.write_text(f"MERCURY_TOKEN_ACME_MAIN={FAKE_TOKEN_MAIN}\nMERCURY_TOKEN_ACME_OPS={FAKE_TOKEN_OPS}\n", encoding="utf-8")
    monkeypatch.setenv("MERCURY_ENTITIES_FILE", str(EXAMPLE_REGISTRY))
    try:
        assert main(["--env-file", str(dotenv)], client_factory=factory) == EXIT_OK
    finally:
        monkeypatch.delenv("MERCURY_TOKEN_ACME_MAIN", raising=False)
        monkeypatch.delenv("MERCURY_TOKEN_ACME_OPS", raising=False)
    lines, _ = _lines(capsys)
    assert len(lines) == 2 and all(" OK " in ln for ln in lines)


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert "mercury-multiorg-mcp-keepalive" in capsys.readouterr().out


_SUBPROCESS_ENV = {"PATH": "/usr/bin:/bin", "MERCURY_TOKEN_ACME_MAIN": "", "MERCURY_TOKEN_ACME_OPS": ""}


def _run_subprocess(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, env=_SUBPROCESS_ENV, timeout=60)


def test_module_entry_point_runs_as_subprocess():
    """`python -m mercury_multiorg_mcp.keepalive` with no tokens: one FAIL line, exit 1, nothing on the wire."""
    proc = _run_subprocess([sys.executable, "-m", "mercury_multiorg_mcp.keepalive", "--entities", str(EXAMPLE_REGISTRY)])
    assert proc.returncode == EXIT_FAIL, proc.stderr
    assert "FAIL (no entity has a token configured" in proc.stdout


def test_console_script_runs_as_subprocess():
    """The `mercury-multiorg-mcp-keepalive` console script installed next to the interpreter behaves the same."""
    script = Path(sys.executable).with_name("mercury-multiorg-mcp-keepalive")
    assert script.is_file(), f"console script not installed at {script}; run `uv sync`"
    proc = _run_subprocess([str(script), "--entities", str(EXAMPLE_REGISTRY)])
    assert proc.returncode == EXIT_FAIL, proc.stderr
    assert "FAIL (no entity has a token configured" in proc.stdout
    assert proc.stdout.count("\n") == 1
    proc = _run_subprocess([str(script), "--version"])
    assert proc.returncode == 0 and "mercury-multiorg-mcp-keepalive" in proc.stdout
