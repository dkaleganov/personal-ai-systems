"""CLI entry point: config resolution, exit codes, dotenv policy, and log redaction."""

import logging
import os
from io import StringIO

import pytest

from mercury_multiorg_mcp import server as server_mod
from mercury_multiorg_mcp.server import RedactingFilter, install_redacting_logging, main

from .conftest import EXAMPLE_REGISTRY, FAKE_TOKEN_MAIN


@pytest.fixture
def no_run(monkeypatch):
    """Stop main() from actually serving stdio; record the transport it asked for."""
    calls: list[str] = []
    monkeypatch.setattr(server_mod.MCPServer, "run", lambda self, transport="stdio", **kw: calls.append(transport))
    return calls


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("MERCURY_ENTITIES_FILE", "MERCURY_API_BASE", "MERCURY_TOKEN_ACME_MAIN", "MERCURY_TOKEN_ACME_OPS"):
        monkeypatch.delenv(var, raising=False)


def test_missing_registry_exits_2(clean_env, capsys):
    assert main([]) == 2
    err = capsys.readouterr().err
    assert "--entities" in err and "MERCURY_ENTITIES_FILE" in err


def test_nonexistent_registry_exits_2(clean_env, capsys, tmp_path):
    assert main(["--entities", str(tmp_path / "missing.yaml")]) == 2
    assert "not found" in capsys.readouterr().err


def test_invalid_api_base_exits_2(clean_env, capsys, no_run):
    assert main(["--entities", str(EXAMPLE_REGISTRY), "--api-base", "http://api.mercury.com"]) == 2
    assert "https" in capsys.readouterr().err
    assert no_run == []


def test_invalid_api_base_from_env_exits_2(clean_env, capsys, no_run, monkeypatch):
    monkeypatch.setenv("MERCURY_API_BASE", "ftp://nope")
    assert main(["--entities", str(EXAMPLE_REGISTRY)]) == 2
    assert no_run == []


def test_happy_path_uses_stdio_and_reports_config(clean_env, capsys, no_run):
    assert main(["--entities", str(EXAMPLE_REGISTRY)]) == 0
    assert no_run == ["stdio"]
    out, err = capsys.readouterr()
    assert out == ""  # stdout is the protocol channel
    assert "2 entities" in err and "api_base=https://api.mercury.com" in err


def test_api_base_flag_wins_over_env(clean_env, capsys, no_run, monkeypatch):
    monkeypatch.setenv("MERCURY_API_BASE", "https://api-sandbox.mercury.com")
    assert main(["--entities", str(EXAMPLE_REGISTRY), "--api-base", "http://localhost:8080"]) == 0
    assert "api_base=http://localhost:8080" in capsys.readouterr().err


def test_entities_env_var_fallback(clean_env, capsys, no_run, monkeypatch):
    monkeypatch.setenv("MERCURY_ENTITIES_FILE", str(EXAMPLE_REGISTRY))
    assert main([]) == 0
    assert no_run == ["stdio"]


def test_env_file_is_loaded_only_when_requested(clean_env, no_run, tmp_path, monkeypatch):
    var = "MERCURY_TOKEN_TEST_ENVFILE"
    registry = tmp_path / "entities.yaml"
    registry.write_text(f"entities:\n  - key: t\n    display_name: T\n    token_env: {var}\n", encoding="utf-8")
    dotenv = tmp_path / "private.env"
    dotenv.write_text(f"{var}={FAKE_TOKEN_MAIN}\n", encoding="utf-8")
    # a .env in the working directory must NOT be picked up implicitly
    (tmp_path / ".env").write_text(f"{var}=from-cwd-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(var, raising=False)
    try:
        assert main(["--entities", str(registry)]) == 0
        assert var not in os.environ
        assert main(["--entities", str(registry), "--env-file", str(dotenv)]) == 0
        assert os.environ.get(var) == FAKE_TOKEN_MAIN
    finally:
        os.environ.pop(var, None)


def test_env_file_does_not_override_existing_env(clean_env, no_run, tmp_path, monkeypatch):
    var = "MERCURY_TOKEN_TEST_ENVFILE2"
    dotenv = tmp_path / "private.env"
    dotenv.write_text(f"{var}=from-file\n", encoding="utf-8")
    monkeypatch.setenv(var, "from-process")
    assert main(["--entities", str(EXAMPLE_REGISTRY), "--env-file", str(dotenv)]) == 0
    assert os.environ[var] == "from-process"


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert "mercury-multiorg-mcp" in capsys.readouterr().out


def _capture_logger(name: str) -> tuple[logging.Logger, StringIO]:
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    buf = StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger, buf


def test_redacting_filter_scrubs_message_args_and_traceback():
    logger, buf = _capture_logger("test.redact.direct")
    install_redacting_logging(logger)
    logger.error("token in msg: Authorization: Bearer %s", FAKE_TOKEN_MAIN)
    logger.error("dict args %(t)s", {"t": FAKE_TOKEN_MAIN})
    try:
        raise RuntimeError(f"boom with {FAKE_TOKEN_MAIN}")
    except RuntimeError:
        logger.exception("crashed")
    out = buf.getvalue()
    assert FAKE_TOKEN_MAIN not in out
    assert out.count("[REDACTED]") >= 3
    assert "Traceback" in out and "RuntimeError" in out


def test_redacting_filter_covers_records_propagated_from_child_loggers():
    """Handler-level filtering is what catches SDK loggers propagating to root."""
    root, buf = _capture_logger("test.redact.root")
    install_redacting_logging(root)
    child = logging.getLogger("test.redact.root.mcp.server")
    child.propagate = True
    child.error("child says %s", FAKE_TOKEN_MAIN)
    out = buf.getvalue()
    assert FAKE_TOKEN_MAIN not in out and "[REDACTED]" in out


def test_redacting_filter_adds_stderr_handler_when_none(capsys):
    logger = logging.getLogger("test.redact.bare")
    logger.handlers.clear()
    logger.propagate = False
    install_redacting_logging(logger)
    assert len(logger.handlers) == 1
    logger.error("x %s", FAKE_TOKEN_MAIN)
    assert FAKE_TOKEN_MAIN not in capsys.readouterr().err


def test_redacting_filter_renders_before_scrubbing():
    rec = logging.LogRecord("n", logging.INFO, "p", 1, "count=%d header Authorization: Bearer %s", (3, FAKE_TOKEN_MAIN), None)
    assert RedactingFilter().filter(rec) is True
    assert rec.args == ()
    assert rec.getMessage() == "count=3 header Authorization: Bearer [REDACTED]"


def test_redacting_filter_survives_bad_format_strings():
    rec = logging.LogRecord("n", logging.INFO, "p", 1, "only one %s", ("a", FAKE_TOKEN_MAIN), None)
    assert RedactingFilter().filter(rec) is True
    msg = rec.getMessage()
    assert FAKE_TOKEN_MAIN not in msg and "only one" in msg


def test_uncaught_exception_hooks_are_redacted(monkeypatch, capsys):
    import sys
    import threading

    from mercury_multiorg_mcp.server import install_redacting_excepthooks

    monkeypatch.setattr(sys, "excepthook", sys.excepthook)  # restored after the test
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    install_redacting_excepthooks()
    try:
        raise RuntimeError(f"crash with Authorization: Bearer {FAKE_TOKEN_MAIN}")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())
    err = capsys.readouterr().err
    assert "Traceback" in err and "RuntimeError" in err
    assert FAKE_TOKEN_MAIN not in err and "[REDACTED]" in err

    def worker() -> None:
        raise ValueError(f"thread {FAKE_TOKEN_MAIN}")

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    err = capsys.readouterr().err
    assert "ValueError" in err and FAKE_TOKEN_MAIN not in err and "[REDACTED]" in err


def test_main_installs_hooks_and_quiets_http_loggers(clean_env, no_run, monkeypatch):
    import sys
    import threading

    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    for name in ("httpx", "httpcore"):
        monkeypatch.setattr(logging.getLogger(name), "level", logging.NOTSET)
    before = sys.excepthook
    assert main(["--entities", str(EXAMPLE_REGISTRY)]) == 0
    assert sys.excepthook is not before
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_api_base_with_credentials_exits_2_without_echoing_them(clean_env, capsys, no_run):
    assert main(["--entities", str(EXAMPLE_REGISTRY), "--api-base", "https://user:hunter2@api.mercury.com"]) == 2
    err = capsys.readouterr().err
    assert "credentials" in err and "hunter2" not in err
    assert no_run == []
