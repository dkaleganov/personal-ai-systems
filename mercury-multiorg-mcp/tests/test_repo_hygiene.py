"""Repository hygiene: the cron snippet is crontab-valid and the gitleaks config catches a Mercury token.

The gitleaks tests read the monorepo-root ``.gitleaks.toml``. On a standalone
checkout of just this package (an sdist, say) that file is absent and those
tests skip; everything else here is self-contained.
"""

import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from .conftest import FAKE_TOKEN_MAIN, PACKAGE_ROOT

MONOREPO_ROOT = PACKAGE_ROOT.parent
GITLEAKS_CONFIG = MONOREPO_ROOT / ".gitleaks.toml"
KEEPALIVE_DOC = PACKAGE_ROOT / "docs" / "keepalive.md"

# five schedule fields, then a command that starts with a slash (absolute path)
CRON_ENTRY = re.compile(r"^(\S+\s+){5}/\S.*$")

needs_root_config = pytest.mark.skipif(
    not GITLEAKS_CONFIG.is_file(), reason="monorepo-root .gitleaks.toml not present (standalone package checkout)"
)
needs_gitleaks = pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks binary not installed")


def _synthetic_token(environment: str, body: str = "Ab0-_" * 8) -> str:
    """Build a Mercury-shaped token at runtime.

    Assembled from a parameter, not from adjacent literals: CPython folds
    ``"a" + "b"`` into one constant at compile time, which would put the full
    token into ``__pycache__/*.pyc`` where a working-tree scan would flag it.
    """
    return "secret-token:mercury_" + environment + "_" + body


def _fenced_block(text: str, lang: str) -> list[str]:
    m = re.search(rf"```{lang}\n(.*?)\n```", text, re.S)
    assert m, f"no ```{lang} block in {KEEPALIVE_DOC}"
    return m.group(1).splitlines()


def test_cron_snippet_is_one_physical_crontab_line():
    lines = [ln for ln in _fenced_block(KEEPALIVE_DOC.read_text(encoding="utf-8"), "cron") if ln.strip()]
    entries = [ln for ln in lines if not ln.lstrip().startswith("#")]
    assert len(entries) == 1, entries
    entry = entries[0]
    assert CRON_ENTRY.match(entry), entry
    assert not entry.rstrip().endswith("\\")  # crontab has no line continuation
    assert "%" not in entry  # cron turns a literal % into a newline
    assert "\t" not in entry
    fields = entry.split()
    assert fields[:5] == ["15", "9", "*", "*", "1"]
    assert "mercury-multiorg-mcp-keepalive" in entry and "--entities" in entry
    # placeholder paths only
    assert "/path/to/" in entry and "/private/path/" in entry
    assert "/Users/" not in entry and "/home/" not in entry


def test_launchd_snippet_uses_placeholder_paths_only():
    xml = "\n".join(_fenced_block(KEEPALIVE_DOC.read_text(encoding="utf-8"), "xml"))
    assert "/path/to/venv/bin/mercury-multiorg-mcp-keepalive" in xml
    assert "/Users/" not in xml and "/home/" not in xml


def test_this_module_compiles_to_no_token_shaped_constant():
    """Guard against a future edit reintroducing a foldable literal (see _synthetic_token)."""
    source = Path(__file__).read_text(encoding="utf-8")
    code = compile(source, str(__file__), "exec")
    shape = re.compile(r"secret-token:mercury_(production|sandbox)_")

    def walk(co):
        for const in co.co_consts:
            if isinstance(const, str):
                assert not shape.search(const), f"folded token-shaped constant: {const[:40]}..."
            elif hasattr(const, "co_consts"):
                walk(const)

    walk(code)


@needs_root_config
def test_gitleaks_config_declares_the_mercury_rule():
    cfg = tomllib.loads(GITLEAKS_CONFIG.read_text(encoding="utf-8"))
    assert cfg["extend"]["useDefault"] is True
    rules = {r["id"]: r for r in cfg["rules"]}
    rule = rules["mercury-api-token"]
    assert rule["keywords"] == ["secret-token:mercury"]
    regex = re.compile(rule["regex"])
    assert regex.search(_synthetic_token("production", "x" * 32))
    assert regex.search(_synthetic_token("sandbox", "y" * 40))
    assert not regex.search(FAKE_TOKEN_MAIN)  # test fixtures use the mercury_test_fake_ prefix
    assert not regex.search(_synthetic_token("production", "short"))


@needs_root_config
@needs_gitleaks
def test_gitleaks_catches_a_synthetic_production_token(tmp_path: Path):
    token = _synthetic_token("production")
    leak = tmp_path / "leak"
    leak.mkdir()
    (leak / "job.env").write_text(f"MERCURY_TOKEN_ACME_MAIN={token}\n", encoding="utf-8")
    report = tmp_path / "report.json"
    proc = subprocess.run(
        [
            "gitleaks", "dir", str(leak),
            "--config", str(GITLEAKS_CONFIG),
            "--no-banner", "--exit-code", "1",
            "--report-format", "json", "--report-path", str(report),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 1, proc.stderr
    findings = json.loads(report.read_text(encoding="utf-8"))
    assert "mercury-api-token" in {f["RuleID"] for f in findings}

    # the fixture token used throughout the test-suite is not a finding
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "job.env").write_text(f"MERCURY_TOKEN_ACME_MAIN={FAKE_TOKEN_MAIN}\n", encoding="utf-8")
    proc = subprocess.run(
        ["gitleaks", "dir", str(clean), "--config", str(GITLEAKS_CONFIG), "--no-banner", "--exit-code", "1"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_python_version_supports_tomllib():
    assert sys.version_info >= (3, 11)
