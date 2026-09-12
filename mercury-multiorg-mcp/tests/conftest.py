"""Shared fixtures: a fake Mercury API served from synthetic JSON, wired into the MCP server in-process."""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import Client

from mercury_multiorg_mcp.client import MercuryClient
from mercury_multiorg_mcp.registry import Registry
from mercury_multiorg_mcp.server import build_server

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXAMPLE_REGISTRY = PACKAGE_ROOT / "entities.example.yaml"

FAKE_TOKEN_MAIN = "secret-token:mercury_test_fake_main_ABCDEFGH1234"
FAKE_API_BASE = "https://api.mercury.example"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeMercury:
    """Serves the fixture pages by path + ``start_after`` cursor and records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.rate_limit_first: int = 0  # respond 429 to the first N requests
        self.retry_after: str | None = None
        self.force_status: int | None = None  # respond with this status to every request
        self.force_body: str = ""
        self._served = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._served < self.rate_limit_first:
            self._served += 1
            headers = {"Retry-After": self.retry_after} if self.retry_after else {}
            return httpx.Response(429, headers=headers, json={"message": "slow down"})
        if self.force_status is not None:
            return httpx.Response(self.force_status, text=self.force_body)

        path = request.url.path
        cursor = request.url.params.get("start_after")
        if path == "/api/v1/accounts":
            page = "accounts_page2.json" if cursor else "accounts_page1.json"
            return httpx.Response(200, json=load_fixture(page))
        if path == "/api/v1/transactions":
            page = "transactions_page2.json" if cursor else "transactions_page1.json"
            data = load_fixture(page)
            limit = int(request.url.params.get("limit", "1000"))
            data["transactions"] = data["transactions"][:limit]
            return httpx.Response(200, json=data)
        return httpx.Response(404, json={"message": f"no fake route for {path}"})


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Run every coroutine test under anyio without per-module marks."""
    for item in items:
        if isinstance(item, pytest.Function) and inspect.iscoroutinefunction(item.obj):
            item.add_marker(pytest.mark.anyio)


@pytest.fixture
def fake_api() -> Iterator[FakeMercury]:
    fake = FakeMercury()
    yield fake
    # Behavioural read-only guarantee: whatever a test did, only GETs reached the wire.
    assert [r.method for r in fake.requests] == ["GET"] * len(fake.requests)


@pytest.fixture
def registry() -> Registry:
    return Registry.from_path(EXAMPLE_REGISTRY)


@pytest.fixture
def env_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """acme_main has a token; acme_ops deliberately does not."""
    monkeypatch.setenv("MERCURY_TOKEN_ACME_MAIN", FAKE_TOKEN_MAIN)
    monkeypatch.delenv("MERCURY_TOKEN_ACME_OPS", raising=False)


@pytest.fixture
def make_client(fake_api: FakeMercury):
    def factory(token: str) -> MercuryClient:
        return MercuryClient(
            token,
            api_base=FAKE_API_BASE,
            transport=httpx.MockTransport(fake_api.handler),
            sleep=_no_sleep,
        )

    return factory


@pytest.fixture
def server(registry: Registry, make_client, env_tokens: None):
    return build_server(registry, api_base=FAKE_API_BASE, client_factory=make_client)


@pytest.fixture
async def mcp_client(server) -> AsyncIterator[Client]:
    async with Client(server) as client:
        yield client
