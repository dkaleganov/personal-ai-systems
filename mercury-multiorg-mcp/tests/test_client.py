import httpx
import pytest

from mercury_multiorg_mcp.client import MercuryClient
from mercury_multiorg_mcp.errors import MercuryAPIError

from .conftest import FAKE_API_BASE, FAKE_TOKEN_MAIN, FakeMercury


async def _no_sleep(_: float) -> None:
    return None


def _client(fake: FakeMercury, **kw) -> MercuryClient:
    return MercuryClient(
        FAKE_TOKEN_MAIN,
        api_base=FAKE_API_BASE,
        transport=httpx.MockTransport(fake.handler),
        sleep=_no_sleep,
        **kw,
    )


def test_client_is_read_only_by_construction():
    """The only request method is _get; there must be nothing that could write."""
    names = {n for n in dir(MercuryClient) if not n.startswith("__")}
    forbidden = {n for n in names if any(v in n.lower() for v in ("post", "put", "patch", "delete", "send", "create", "update"))}
    assert not forbidden, forbidden


def test_repr_and_suffix_never_expose_token(fake_api):
    c = _client(fake_api)
    assert FAKE_TOKEN_MAIN not in repr(c)
    assert c.token_suffix == "1234"
    assert c.base_url == f"{FAKE_API_BASE}/api/v1"


def test_empty_token_rejected():
    with pytest.raises(ValueError):
        MercuryClient("   ")


async def test_bearer_header_and_accounts_pagination(fake_api):
    async with _client(fake_api) as c:
        accounts = await c.list_accounts()
    assert [a["name"] for a in accounts] == ["Acme Main Checking", "Acme Savings", "Acme Tax Reserve"]
    assert len(fake_api.requests) == 2
    first, second = fake_api.requests
    assert first.headers["Authorization"] == f"Bearer {FAKE_TOKEN_MAIN}"
    assert first.url.path == "/api/v1/accounts"
    assert "start_after" not in first.url.params
    assert first.url.params["limit"] == "1000"
    # cursor is the last id actually received on page 1
    assert second.url.params["start_after"] == "22222222-2222-4222-8222-222222222222"


async def test_transactions_pagination_and_filters(fake_api):
    async with _client(fake_api) as c:
        txns = await c.list_transactions(
            account_id="11111111-1111-4111-8111-111111111111",
            start="2026-03-01",
            end="2026-03-31",
            search="invoice",
            limit=10,
        )
    assert [t["id"][9:13] for t in txns] == ["0001", "0002", "0003"]
    req = fake_api.requests[0]
    p = req.url.params
    assert p["accountId"] == "11111111-1111-4111-8111-111111111111"
    assert p["start"] == "2026-03-01"
    assert p["end"] == "2026-03-31"
    assert p["search"] == "invoice"
    assert p["order"] == "desc"
    assert p["limit"] == "10"
    assert "status" not in p  # None params are dropped


async def test_transactions_limit_caps_results_and_page_size(fake_api):
    async with _client(fake_api) as c:
        txns = await c.list_transactions(limit=1)
    assert len(txns) == 1
    assert len(fake_api.requests) == 1
    assert fake_api.requests[0].url.params["limit"] == "1"


async def test_invalid_order_rejected(fake_api):
    async with _client(fake_api) as c:
        with pytest.raises(ValueError):
            await c.list_transactions(order="sideways")


async def test_429_is_retried_with_retry_after(fake_api):
    fake_api.rate_limit_first = 2
    fake_api.retry_after = "1"
    async with _client(fake_api, max_retries=3) as c:
        accounts = await c.list_accounts()
    assert len(accounts) == 3
    # 2 x 429 + 2 real pages
    assert [r.url.path for r in fake_api.requests].count("/api/v1/accounts") == 4


async def test_429_retries_exhausted_raises_clean_error(fake_api):
    fake_api.rate_limit_first = 10
    async with _client(fake_api, max_retries=2) as c:
        with pytest.raises(MercuryAPIError) as info:
            await c.list_accounts()
    assert info.value.status_code == 429
    assert len(fake_api.requests) == 3  # initial + 2 retries
    assert FAKE_TOKEN_MAIN not in str(info.value)


async def test_error_body_is_redacted(fake_api):
    fake_api.force_status = 401
    fake_api.force_body = f"Unauthorized: bad header Authorization: Bearer {FAKE_TOKEN_MAIN}"
    async with _client(fake_api) as c:
        with pytest.raises(MercuryAPIError) as info:
            await c.list_accounts()
    msg = str(info.value)
    assert "401" in msg
    assert FAKE_TOKEN_MAIN not in msg
    assert "[REDACTED]" in msg


async def test_transport_error_is_wrapped_and_redacted():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    c = MercuryClient(FAKE_TOKEN_MAIN, api_base=FAKE_API_BASE, transport=httpx.MockTransport(boom), sleep=_no_sleep)
    async with c:
        with pytest.raises(MercuryAPIError) as info:
            await c.list_accounts()
    assert "connection refused" in str(info.value)
    assert FAKE_TOKEN_MAIN not in str(info.value)


async def test_unexpected_shape_raises(fake_api):
    fake_api.force_status = 200
    fake_api.force_body = '{"nope": []}'
    async with _client(fake_api) as c:
        with pytest.raises(MercuryAPIError, match="missing 'accounts'"):
            await c.list_accounts()


async def test_non_json_raises(fake_api):
    fake_api.force_status = 200
    fake_api.force_body = "<html>maintenance</html>"
    async with _client(fake_api) as c:
        with pytest.raises(MercuryAPIError, match="non-JSON"):
            await c.list_accounts()


def test_api_base_env_override(monkeypatch):
    monkeypatch.setenv("MERCURY_API_BASE", "https://api-sandbox.mercury.example/")
    c = MercuryClient(FAKE_TOKEN_MAIN)
    assert c.base_url == "https://api-sandbox.mercury.example/api/v1"
    monkeypatch.delenv("MERCURY_API_BASE")
    c2 = MercuryClient(FAKE_TOKEN_MAIN)
    assert c2.base_url == "https://api.mercury.com/api/v1"
