"""Entity registry: maps a short entity key to a display name and a token env var.

The registry file is YAML and lives *outside* this repo in real deployments
(``--entities <path>`` or ``MERCURY_ENTITIES_FILE``). The committed
``entities.example.yaml`` shows the shape with fake entries only.

The package resolves environment variables and nothing else. It knows
nothing about any secret manager. Tokens are never stored on the registry
objects; they are looked up at call time so a missing token for one entity
is a clean per-entity error and never affects the others.

``token_env`` must match ``MERCURY_TOKEN_[A-Z0-9_]+``: the registry is
configuration, but a hostile or mistaken registry must not be able to name
an arbitrary environment variable and have its value sent as a bearer token
to the configured API host.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .errors import TOKEN_PREFIX, MissingTokenError, RegistryError, UnknownEntityError, token_suffix

ENTITIES_FILE_ENV = "MERCURY_ENTITIES_FILE"

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# Reserved namespace: only variables under this prefix may ever be used as a bearer token.
TOKEN_ENV_PREFIX = "MERCURY_TOKEN_"
_ENV_RE = re.compile(r"^MERCURY_TOKEN_[A-Z0-9_]+$")


def _yaml_problem(exc: yaml.YAMLError) -> str:
    """One line describing a YAML parse failure: the parser's problem and the line/column, never a source snippet.

    ``str(exc)`` on a ``MarkedYAMLError`` spans several lines and quotes the
    offending source; only ``context``, ``problem``, and the mark position
    are kept, whitespace-collapsed (B7).
    """
    parts: list[str] = []
    if isinstance(exc, yaml.MarkedYAMLError):
        for piece in (exc.context, exc.problem):
            if piece:
                parts.append(" ".join(str(piece).split()))
        mark = exc.problem_mark or exc.context_mark
        if mark is not None:
            parts.append(f"(line {mark.line + 1}, column {mark.column + 1})")
    if not parts:
        parts.append(exc.__class__.__name__)
    return " ".join(parts)


class EntityConfig(BaseModel):
    """One organization: its routing key, human label, and where its token lives."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(description="Short routing key used as the `entity` argument of every tool.")
    display_name: str = Field(min_length=1, description="Human-readable label. Not used for routing.")
    token_env: str = Field(description="Name of the environment variable holding this org's read-only API token.")

    @field_validator("key")
    @classmethod
    def _key_shape(cls, v: str) -> str:
        if not _KEY_RE.fullmatch(v):  # fullmatch: `$` alone would accept a trailing newline
            raise ValueError("must be lowercase letters, digits, and underscores, starting with a letter (e.g. acme_main)")
        return v

    @field_validator("token_env")
    @classmethod
    def _env_shape(cls, v: str) -> str:
        if not _ENV_RE.fullmatch(v):  # fullmatch: `$` alone would accept a trailing newline
            raise ValueError(
                f"must be an environment variable name starting with {TOKEN_ENV_PREFIX} "
                "(uppercase letters, digits, underscores; e.g. MERCURY_TOKEN_ACME_MAIN)"
            )
        return v

    @field_validator("display_name")
    @classmethod
    def _display_name_strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class Registry:
    """Validated, ordered collection of :class:`EntityConfig` entries."""

    def __init__(self, entities: list[EntityConfig], *, source: str | None = None) -> None:
        keys = [e.key for e in entities]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        if dupes:
            raise RegistryError(f"Duplicate entity keys in registry: {', '.join(dupes)}")
        if not entities:
            raise RegistryError("Registry has no entities. Add at least one entry under `entities:`.")
        self._entities: dict[str, EntityConfig] = {e.key: e for e in entities}
        self.source = source

    # -- construction -----------------------------------------------------

    @classmethod
    def from_mapping(cls, data: Any, *, source: str | None = None) -> Registry:
        if not isinstance(data, dict):
            raise RegistryError("Registry root must be a mapping with an `entities:` list.")
        # YAML happily yields integer, float, boolean, or null keys; reject
        # them before anything sorts or joins the key set (m4).
        non_string = [k for k in data if not isinstance(k, str)]
        if non_string:
            raise RegistryError(f"Registry top-level keys must be strings ({len(non_string)} non-string key(s) found).")
        raw = data.get("entities")
        if not isinstance(raw, list):
            raise RegistryError("Registry must contain an `entities:` list.")
        unknown_top = sorted(set(data) - {"entities"})
        if unknown_top:
            raise RegistryError(f"Unexpected top-level keys in registry: {', '.join(unknown_top)}")
        entities: list[EntityConfig] = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                raise RegistryError(f"Invalid entity at entities[{i}]: must be a mapping with key, display_name, token_env")
            if any(not isinstance(k, str) for k in item):
                raise RegistryError(f"Invalid entity at entities[{i}]: field names must be strings")
            try:
                entities.append(EntityConfig.model_validate(item))
            except ValidationError as exc:
                problems = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or '<entry>'}: {e['msg']}" for e in exc.errors())
                raise RegistryError(f"Invalid entity at entities[{i}]: {problems}") from None
        return cls(entities, source=source)

    @classmethod
    def from_path(cls, path: str | os.PathLike[str]) -> Registry:
        p = Path(path).expanduser()
        if not p.is_file():
            raise RegistryError(f"Entity registry file not found: {p}")
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            raise RegistryError(f"Entity registry could not be read ({p}): {exc.__class__.__name__}") from None
        except UnicodeDecodeError:
            raise RegistryError(f"Entity registry is not UTF-8 text ({p})") from None
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise RegistryError(f"Entity registry is not valid YAML ({p}): {_yaml_problem(exc)}") from None
        except (ValueError, RecursionError, OverflowError) as exc:
            raise RegistryError(f"Entity registry could not be parsed ({p}): {exc.__class__.__name__}") from None
        return cls.from_mapping(data, source=str(p))

    @classmethod
    def resolve_path(cls, explicit: str | os.PathLike[str] | None) -> Path:
        """Pick the registry path: explicit CLI arg first, then the env var. No implicit default."""
        candidate = explicit or os.environ.get(ENTITIES_FILE_ENV)
        if not candidate:
            raise RegistryError(
                f"No entity registry configured. Pass --entities <path> or set {ENTITIES_FILE_ENV}. "
                "See entities.example.yaml for the shape."
            )
        return Path(candidate).expanduser()

    # -- lookup -----------------------------------------------------------

    @property
    def keys(self) -> list[str]:
        return list(self._entities)

    def __len__(self) -> int:
        return len(self._entities)

    def __contains__(self, key: object) -> bool:
        return key in self._entities

    def entities(self) -> list[EntityConfig]:
        return list(self._entities.values())

    def get(self, key: str) -> EntityConfig:
        try:
            return self._entities[key]
        except KeyError:
            raise UnknownEntityError(key, self.keys) from None

    def resolve_token(self, key: str) -> str:
        """Read the token for ``key`` from its env var. Raises per-entity errors only."""
        entity = self.get(key)
        value = os.environ.get(entity.token_env, "")
        if not value.strip():
            raise MissingTokenError(key, entity.token_env)
        return value.strip()

    def token_status(self, key: str) -> bool:
        """True if the env var for ``key`` is set and non-blank. Never returns the value."""
        entity = self.get(key)
        return bool(os.environ.get(entity.token_env, "").strip())

    def token_shape_warnings(self) -> list[str]:
        """One warning line per configured token that does not look like a Mercury API token.

        Mercury tokens start with the documented ``secret-token:`` prefix.
        A configured value without it is usually a copy-paste mistake or the
        wrong secret in the env var. The line names the entity and the env
        var and shows at most the last four characters of the value.
        """
        warnings: list[str] = []
        for entity in self._entities.values():
            value = os.environ.get(entity.token_env, "").strip()
            if value and not value.startswith(TOKEN_PREFIX):
                warnings.append(
                    f"warning: entity {entity.key!r}: {entity.token_env} does not look like a Mercury API token "
                    f"(expected the documented {TOKEN_PREFIX!r} prefix; value ends with ...{token_suffix(value)})"
                )
        return warnings
