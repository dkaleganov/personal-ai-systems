import pytest

from mercury_multiorg_mcp.errors import MissingTokenError, RegistryError, UnknownEntityError
from mercury_multiorg_mcp.registry import ENTITIES_FILE_ENV, Registry

from .conftest import EXAMPLE_REGISTRY, FAKE_TOKEN_MAIN


def test_example_registry_loads():
    reg = Registry.from_path(EXAMPLE_REGISTRY)
    assert reg.keys == ["acme_main", "acme_ops"]
    assert reg.get("acme_main").display_name == "Acme Holdings (main)"
    assert reg.get("acme_ops").token_env == "MERCURY_TOKEN_ACME_OPS"
    assert len(reg) == 2
    assert "acme_main" in reg


@pytest.mark.parametrize(
    "bad_key",
    ["Acme", "acme-main", "1acme", "", "acme main"],
)
def test_key_shape_is_validated(bad_key):
    with pytest.raises(RegistryError, match=r"entities\[0\]"):
        Registry.from_mapping({"entities": [{"key": bad_key, "display_name": "x", "token_env": "MERCURY_TOKEN_X"}]})


@pytest.mark.parametrize(
    "bad_env",
    ["lower", "1ABC", "HAS-DASH", "", "MERCURY_TOKEN_", "mercury_token_x", "OTHER_SECRET", "AWS_SECRET_ACCESS_KEY", "MERCURY_TOKENX"],
)
def test_token_env_shape_is_validated(bad_env):
    """token_env must live under the reserved MERCURY_TOKEN_ prefix (hardening: a registry cannot name any env var)."""
    with pytest.raises(RegistryError, match="token_env.*MERCURY_TOKEN_"):
        Registry.from_mapping({"entities": [{"key": "acme", "display_name": "x", "token_env": bad_env}]})


def test_blank_display_name_rejected():
    with pytest.raises(RegistryError, match="display_name"):
        Registry.from_mapping({"entities": [{"key": "acme", "display_name": "   ", "token_env": "MERCURY_TOKEN_X"}]})


def test_extra_fields_rejected():
    with pytest.raises(RegistryError, match="token"):
        Registry.from_mapping(
            {"entities": [{"key": "acme", "display_name": "x", "token_env": "MERCURY_TOKEN_X", "token": "leak"}]}
        )


def test_duplicate_keys_rejected():
    with pytest.raises(RegistryError, match="Duplicate"):
        Registry.from_mapping(
            {
                "entities": [
                    {"key": "acme", "display_name": "a", "token_env": "MERCURY_TOKEN_A"},
                    {"key": "acme", "display_name": "b", "token_env": "MERCURY_TOKEN_B"},
                ]
            }
        )


def test_empty_registry_rejected():
    with pytest.raises(RegistryError, match="no entities"):
        Registry.from_mapping({"entities": []})


@pytest.mark.parametrize("data", [None, [], "x", {"orgs": []}, {"entities": {}}])
def test_bad_root_shapes_rejected(data):
    with pytest.raises(RegistryError):
        Registry.from_mapping(data)


def test_unknown_top_level_keys_rejected():
    with pytest.raises(RegistryError, match="Unexpected top-level"):
        Registry.from_mapping({"entities": [{"key": "a", "display_name": "a", "token_env": "MERCURY_TOKEN_A"}], "tokens": {}})


def test_missing_file(tmp_path):
    with pytest.raises(RegistryError, match="not found"):
        Registry.from_path(tmp_path / "nope.yaml")


def test_invalid_yaml(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("entities: [unclosed", encoding="utf-8")
    with pytest.raises(RegistryError, match="not valid YAML"):
        Registry.from_path(p)


def test_resolve_path_prefers_explicit_then_env(monkeypatch, tmp_path):
    monkeypatch.delenv(ENTITIES_FILE_ENV, raising=False)
    with pytest.raises(RegistryError, match="--entities"):
        Registry.resolve_path(None)
    monkeypatch.setenv(ENTITIES_FILE_ENV, str(tmp_path / "from_env.yaml"))
    assert Registry.resolve_path(None).name == "from_env.yaml"
    assert Registry.resolve_path(tmp_path / "explicit.yaml").name == "explicit.yaml"


def test_resolve_token_reads_env_only(monkeypatch):
    reg = Registry.from_path(EXAMPLE_REGISTRY)
    monkeypatch.setenv("MERCURY_TOKEN_ACME_MAIN", f"  {FAKE_TOKEN_MAIN}  ")
    monkeypatch.delenv("MERCURY_TOKEN_ACME_OPS", raising=False)
    assert reg.resolve_token("acme_main") == FAKE_TOKEN_MAIN
    assert reg.token_status("acme_main") is True
    assert reg.token_status("acme_ops") is False
    with pytest.raises(MissingTokenError) as info:
        reg.resolve_token("acme_ops")
    assert "MERCURY_TOKEN_ACME_OPS" in str(info.value)
    assert info.value.entity == "acme_ops"


def test_blank_token_counts_as_missing(monkeypatch):
    reg = Registry.from_path(EXAMPLE_REGISTRY)
    monkeypatch.setenv("MERCURY_TOKEN_ACME_OPS", "   ")
    with pytest.raises(MissingTokenError):
        reg.resolve_token("acme_ops")


def test_unknown_entity_lists_known_keys():
    reg = Registry.from_path(EXAMPLE_REGISTRY)
    with pytest.raises(UnknownEntityError, match="acme_main, acme_ops"):
        reg.get("acme_other")
