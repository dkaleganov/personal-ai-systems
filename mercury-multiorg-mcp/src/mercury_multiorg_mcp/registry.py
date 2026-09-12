"""Entity registry: maps a short entity key to a display name and a token env var.

The registry file is YAML and lives *outside* this repo in real deployments
(``--entities <path>`` or ``MERCURY_ENTITIES_FILE``). The committed
``entities.example.yaml`` shows the shape with fake entries only.

The package resolves environment variables and nothing else. It knows
nothing about any secret manager. Tokens are never stored on the registry
objects; they are looked up at call time so a missing token for one entity
is a clean per-entity error and never affects the others.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .errors import MissingTokenError, RegistryError, UnknownEntityError

ENTITIES_FILE_ENV = "MERCURY_ENTITIES_FILE"

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class EntityConfig(BaseModel):
    """One organization: its routing key, human label, and where its token lives."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(description="Short routing key used as the `entity` argument of every tool.")
    display_name: str = Field(min_length=1, description="Human-readable label. Not used for routing.")
    token_env: str = Field(description="Name of the environment variable holding this org's read-only API token.")

    @field_validator("key")
    @classmethod
    def _key_shape(cls, v: str) -> str:
        if not _KEY_RE.match(v):
            raise ValueError("must be lowercase letters, digits, and underscores, starting with a letter (e.g. acme_main)")
        return v

    @field_validator("token_env")
    @classmethod
    def _env_shape(cls, v: str) -> str:
        if not _ENV_RE.match(v):
            raise ValueError("must look like an environment variable name (e.g. MERCURY_TOKEN_ACME_MAIN)")
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
        raw = data.get("entities")
        if not isinstance(raw, list):
            raise RegistryError("Registry must contain an `entities:` list.")
        unknown_top = sorted(set(data) - {"entities"})
        if unknown_top:
            raise RegistryError(f"Unexpected top-level keys in registry: {', '.join(unknown_top)}")
        entities: list[EntityConfig] = []
        for i, item in enumerate(raw):
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
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise RegistryError(f"Entity registry is not valid YAML ({p}): {exc}") from None
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
