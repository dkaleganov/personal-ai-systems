"""v0.1.3: packaging for PyPI and the official MCP Registry (no runtime changes).

Pins (a) the registry ownership marker in README.md, which is the PyPI long
description the registry reads, (b) that README.md has no relative links,
because PyPI renders it out of context, and that its repository links point
at this release's tag, (c) the pinned PyPI form in every client snippet, each
parsed as JSON or TOML, and (d) server.json agreeing with pyproject.toml and
with the command-line contract.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from mercury_multiorg_mcp import __version__
from mercury_multiorg_mcp import client as client_mod
from mercury_multiorg_mcp import registry as registry_mod
from mercury_multiorg_mcp import server as server_mod

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
VERSION = PROJECT["version"]
SERVER_JSON = ROOT / "server.json"
SERVER_NAME = "io.github.dkaleganov/mercury-multiorg-mcp"
SCHEMA_URL = "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
REPO = "https://github.com/dkaleganov/personal-ai-systems"
TAG_BASE = f"{REPO}/blob/mercury-v{VERSION}/mercury-multiorg-mcp/"

# server.json is tracked in git but deliberately not shipped in the sdist; CLAUDE.md is not shipped either.
needs_server_json = pytest.mark.skipif(not SERVER_JSON.is_file(), reason="server.json not present (sdist)")
needs_checkout = pytest.mark.skipif(not (ROOT / "CLAUDE.md").is_file(), reason="CLAUDE.md not present (sdist)")

INLINE_LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)")
REFERENCE_LINK = re.compile(r"^ {0,3}\[[^\]]+\]:\s*<?([^\s>]+)", re.M)
HTML_LINK = re.compile(r"""\b(?:href|src)\s*=\s*["']([^"']+)["']""", re.I)
ABSOLUTE = re.compile(r"^(?:https?:|mailto:)", re.I)


def _head() -> str:
    return README.split("\n## ", 1)[0]


def test_version_is_consistent():
    assert VERSION == "0.1.3" == __version__
    assert f"Version {VERSION}" in README


def test_readme_carries_the_registry_marker_exactly_once_near_the_top():
    marker = f"<!-- mcp-name: {SERVER_NAME} -->"
    assert README.count(marker) == 1
    assert README.count("mcp-name:") == 1  # no second, possibly different, server name anywhere
    assert marker in _head()


def test_readme_disclaimer_near_the_top():
    assert "Unofficial. Not affiliated with or endorsed by Mercury." in _head()


def test_pyproject_description_and_urls():
    assert PROJECT["description"].startswith("Unofficial") and len(PROJECT["description"]) <= 200
    assert PROJECT["urls"] == {
        "Homepage": f"{REPO}/tree/main/mercury-multiorg-mcp",
        "Source": REPO,
        "Documentation": f"{TAG_BASE}docs/tools.md",
        "Changelog": f"{TAG_BASE}CHANGELOG.md",
        "Issues": f"{REPO}/issues",
    }


def test_readme_has_no_relative_links():
    targets = INLINE_LINK.findall(README) + REFERENCE_LINK.findall(README) + HTML_LINK.findall(README)
    assert len(targets) >= 10, targets  # guards against a pattern that silently matches nothing
    # fragment-only links are relative too: PyPI rewrites heading ids, so they break there
    assert [t for t in targets if not ABSOLUTE.match(t)] == []


def test_readme_repository_links_are_pinned_to_this_release():
    repo_links = [t for t in INLINE_LINK.findall(README) if t.startswith(f"{REPO}/blob/") or t.startswith(f"{REPO}/tree/")]
    assert all(link.startswith(TAG_BASE) for link in repo_links), repo_links
    assert {link[len(TAG_BASE):] for link in repo_links} == {
        "CHANGELOG.md",
        "docs/tools.md",
        "docs/keepalive.md",
        "CLAUDE.md",
        "LICENSE",
        "entities.example.yaml",
        ".env.example",
    }


@needs_checkout
def test_readme_repository_links_name_files_that_exist():
    for link in INLINE_LINK.findall(README):
        if link.startswith(TAG_BASE):
            assert (ROOT / link[len(TAG_BASE):]).is_file(), link


def test_every_pinned_install_uses_the_project_version():
    assert set(re.findall(r"mercury-multiorg-mcp@(\d[\w.+-]*\w)", README)) == {VERSION}
    assert set(re.findall(r"mercury-multiorg-mcp==(\d[\w.+-]*\w)", README)) == {VERSION}
    assert f"uvx mercury-multiorg-mcp@{VERSION} --entities /private/path/entities.yaml" in README
    assert "### From source / pinned commit" in README
    assert "git+https://github.com/dkaleganov/personal-ai-systems@<FULL_COMMIT_SHA>#subdirectory=mercury-multiorg-mcp" in README


def test_client_snippets_parse_and_run_the_pinned_pypi_release():
    json_blocks = [json.loads(b) for b in re.findall(r"```json\n(.*?)\n```", README, re.S)]
    toml_blocks = [tomllib.loads(b) for b in re.findall(r"```toml\n(.*?)\n```", README, re.S)]
    assert len(json_blocks) == 5 and len(toml_blocks) == 1
    servers = [b.get("mcpServers", b.get("servers"))["mercury-multiorg"] for b in json_blocks]
    servers.append(toml_blocks[0]["mcp_servers"]["mercury-multiorg"])
    for entry in servers:
        assert entry["command"] == "uvx"
        assert entry["args"][:3] == [f"mercury-multiorg-mcp@{VERSION}", "--entities", "/private/path/entities.yaml"]
        assert "--from" not in entry["args"]
    vscode = [b for b in json_blocks if "servers" in b]
    assert len(vscode) == 1 and vscode[0]["servers"]["mercury-multiorg"]["type"] == "stdio"


@needs_server_json
def test_server_json_matches_pyproject():
    data = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
    assert data["$schema"] == SCHEMA_URL
    assert data["name"] == SERVER_NAME
    assert data["version"] == VERSION
    assert data["description"].startswith("Unofficial") and len(data["description"]) <= 100  # schema maxLength
    assert data["repository"] == {"url": REPO, "source": "github", "subfolder": "mercury-multiorg-mcp"}
    assert len(data["packages"]) == 1
    package = data["packages"][0]
    assert package["registryType"] == "pypi"
    assert package["identifier"] == PROJECT["name"]
    assert package["version"] == VERSION
    assert package["runtimeHint"] == "uvx"
    assert package["transport"] == {"type": "stdio"}


@needs_server_json
def test_server_json_inputs_match_the_command_line_contract():
    package = json.loads(SERVER_JSON.read_text(encoding="utf-8"))["packages"][0]
    arguments = {a["name"]: a for a in package["packageArguments"]}
    assert set(arguments) == {"--entities", "--env-file"}
    assert all(a["type"] == "named" and a["format"] == "filepath" and not a.get("isSecret", False) for a in arguments.values())
    assert arguments["--entities"]["isRequired"] is True and arguments["--env-file"]["isRequired"] is False
    parsed = server_mod._parse_args(["--entities", "/private/path/entities.yaml", "--env-file", "/private/path/mercury.env"])
    assert parsed.entities == "/private/path/entities.yaml" and parsed.env_file == "/private/path/mercury.env"

    env = {e["name"]: e for e in package["environmentVariables"]}
    tokens = [name for name in env if name.startswith("MERCURY_TOKEN_")]
    assert len(tokens) == 1  # one example; token variable names are defined by each user's registry
    token = env[tokens[0]]
    assert registry_mod._ENV_RE.fullmatch(tokens[0])
    assert token["isSecret"] is True and token["isRequired"] is False
    assert "MERCURY_TOKEN_[A-Z0-9_]+" in token["description"] and "token_env" in token["description"]
    assert "value" not in token and "default" not in token

    others = {name: e for name, e in env.items() if name not in tokens}
    assert set(others) == {registry_mod.ENTITIES_FILE_ENV, client_mod.API_BASE_ENV, server_mod.ALLOW_DOCUMENTS_ENV}
    assert all(e["isRequired"] is False and e["isSecret"] is False for e in others.values())
    assert others[client_mod.API_BASE_ENV]["default"] == client_mod.DEFAULT_API_BASE
    flag = others[server_mod.ALLOW_DOCUMENTS_ENV]
    assert flag["format"] == "boolean" and "true" in server_mod._TRUE_VALUES and flag["default"] not in server_mod._TRUE_VALUES
    assert "secret-token:" not in SERVER_JSON.read_text(encoding="utf-8")
