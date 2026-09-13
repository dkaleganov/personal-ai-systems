import httpx
import pytest

from mercury_multiorg_mcp.client import MercuryClient
from mercury_multiorg_mcp.errors import MercuryAPIError

from .conftest import FAKE_API_BASE, FAKE_TOKEN_MAIN, FakeMercury, load_fixture


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


async def test_transport_error_is_retried_then_wrapped_and_redacted():
    calls: list[str] = []

    def boom(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        # Mimic a transport error whose message carries the request headers.
        raise httpx.ConnectError(
            f"connection refused; headers={{'Authorization': 'Bearer {FAKE_TOKEN_MAIN}'}}", request=request
        )

    c = MercuryClient(
        FAKE_TOKEN_MAIN, api_base=FAKE_API_BASE, transport=httpx.MockTransport(boom), sleep=_no_sleep, max_retries=2
    )
    async with c:
        with pytest.raises(MercuryAPIError) as info:
            await c.list_accounts()
    msg = str(info.value)
    assert calls == ["GET", "GET", "GET"]  # initial + 2 retries, all GET
    assert "after 3 attempts" in msg
    assert "connection refused" in msg
    assert FAKE_TOKEN_MAIN not in msg
    assert "[REDACTED]" in msg


async def test_transport_error_recovers_on_retry(fake_api):
    failures = {"left": 1}

    def flaky(request: httpx.Request) -> httpx.Response:
        if failures["left"]:
            failures["left"] -= 1
            raise httpx.ReadTimeout("timed out", request=request)
        return fake_api.handler(request)

    c = MercuryClient(FAKE_TOKEN_MAIN, api_base=FAKE_API_BASE, transport=httpx.MockTransport(flaky), sleep=_no_sleep)
    async with c:
        accounts = await c.list_accounts()
    assert len(accounts) == 3


async def test_non_transport_httpx_error_is_not_retried():
    calls = 0

    def bad(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        # An httpx.HTTPError that is NOT a TransportError: wrapped, never retried.
        raise httpx.TooManyRedirects("redirect loop", request=request)

    c = MercuryClient(FAKE_TOKEN_MAIN, api_base=FAKE_API_BASE, transport=httpx.MockTransport(bad), sleep=_no_sleep)
    async with c:
        with pytest.raises(MercuryAPIError, match="redirect loop"):
            await c.list_accounts()
    assert calls == 1


async def test_backoff_honours_retry_after_and_exponential_fallback(fake_api):
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    fake_api.rate_limit_first = 3
    fake_api.retry_after = "2"
    c = MercuryClient(
        FAKE_TOKEN_MAIN, api_base=FAKE_API_BASE, transport=httpx.MockTransport(fake_api.handler), sleep=record
    )
    async with c:
        await c.list_accounts()
    assert slept == [2.0, 2.0, 2.0]

    slept.clear()
    fake2 = type(fake_api)()
    fake2.rate_limit_first = 3  # no Retry-After header this time
    c2 = MercuryClient(FAKE_TOKEN_MAIN, api_base=FAKE_API_BASE, transport=httpx.MockTransport(fake2.handler), sleep=record)
    async with c2:
        await c2.list_accounts()
    assert len(slept) == 3
    for attempt, seconds in enumerate(slept):
        base = 0.5 * (2**attempt)
        assert base <= seconds <= base + 0.25, (attempt, seconds)


def test_retry_after_is_capped_and_non_numeric_falls_back():
    resp = httpx.Response(429, headers={"Retry-After": "600"})
    assert MercuryClient._backoff_seconds(resp, 0) == 60.0
    resp = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert 0.5 <= MercuryClient._backoff_seconds(resp, 0) <= 0.75
    assert MercuryClient._backoff_seconds(None, 10) <= 16.25  # capped


async def test_error_body_is_scrubbed_before_truncation(fake_api):
    fake_api.force_status = 403
    # Token straddles the 500-char cut; a truncate-then-scrub order would leave a fragment.
    fake_api.force_body = "x" * 470 + f" Authorization: Bearer {FAKE_TOKEN_MAIN}"
    async with _client(fake_api) as c:
        with pytest.raises(MercuryAPIError) as info:
            await c.list_accounts()
    msg = str(info.value)
    assert FAKE_TOKEN_MAIN not in msg
    assert FAKE_TOKEN_MAIN[:12] not in msg  # no partial token survives the cut
    assert "[REDACTED]" in msg


async def test_empty_page_returns_empty_list(fake_api):
    fake_api.force_status = 200
    fake_api.force_body = '{"transactions": [], "page": {"nextPage": null, "previousPage": null}}'
    async with _client(fake_api) as c:
        assert await c.list_transactions() == []
    assert len(fake_api.requests) == 1


async def test_pagination_dedupes_and_stops_when_cursor_does_not_advance():
    """A server that keeps returning the same page with nextPage set must not loop."""
    page = load_fixture("transactions_page1.json")
    calls = 0

    def stuck(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=page)

    c = MercuryClient(FAKE_TOKEN_MAIN, api_base=FAKE_API_BASE, transport=httpx.MockTransport(stuck), sleep=_no_sleep)
    async with c:
        txns = await c.list_transactions(limit=50)
    assert [t["id"] for t in txns] == [t["id"] for t in page["transactions"]]
    assert calls == 2  # second page yielded nothing fresh -> stop


async def test_pagination_exhausting_max_pages_is_a_clean_error(monkeypatch):
    """A server that always has 'more' unique items: stop at MAX_PAGES and fail loudly, never a short list."""
    import mercury_multiorg_mcp.client as client_mod

    monkeypatch.setattr(client_mod, "MAX_PAGES", 3)
    calls = 0

    def endless(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        item = {"id": f"00000000-0000-4000-8000-{calls:012d}", "name": f"acct {calls}"}
        return httpx.Response(200, json={"accounts": [item], "page": {"nextPage": item["id"], "previousPage": None}})

    c = MercuryClient(FAKE_TOKEN_MAIN, api_base=FAKE_API_BASE, transport=httpx.MockTransport(endless), sleep=_no_sleep)
    async with c:
        with pytest.raises(MercuryAPIError, match="more than 3 pages") as info:
            await c.list_accounts()
    assert calls == 3
    assert info.value.path == "/accounts"
    assert FAKE_TOKEN_MAIN not in str(info.value)


async def test_pagination_limit_satisfied_on_last_allowed_page_is_not_an_error(monkeypatch):
    """max_items reached exactly on the MAX_PAGES-th page while the server still says 'more': complete, not short."""
    import mercury_multiorg_mcp.client as client_mod

    monkeypatch.setattr(client_mod, "MAX_PAGES", 3)
    calls = 0

    def endless(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        item = {"id": f"00000000-0000-4000-8000-{calls:012d}"}
        return httpx.Response(200, json={"transactions": [item], "page": {"nextPage": item["id"], "previousPage": None}})

    c = MercuryClient(FAKE_TOKEN_MAIN, api_base=FAKE_API_BASE, transport=httpx.MockTransport(endless), sleep=_no_sleep)
    async with c:
        txns = await c.list_transactions(limit=3)
        assert len(txns) == 3 and calls == 3
        # but a short result under a limit is still an error
        with pytest.raises(MercuryAPIError, match="more than 3 pages"):
            await c.list_transactions(limit=4)


async def test_pagination_last_allowed_page_without_more_is_fine(monkeypatch):
    """Exactly MAX_PAGES pages with the last one reporting no more is a normal result."""
    import mercury_multiorg_mcp.client as client_mod

    monkeypatch.setattr(client_mod, "MAX_PAGES", 3)
    calls = 0

    def three_pages(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        item = {"id": f"00000000-0000-4000-8000-{calls:012d}", "name": f"acct {calls}"}
        more = item["id"] if calls < 3 else None
        return httpx.Response(200, json={"accounts": [item], "page": {"nextPage": more, "previousPage": None}})

    c = MercuryClient(FAKE_TOKEN_MAIN, api_base=FAKE_API_BASE, transport=httpx.MockTransport(three_pages), sleep=_no_sleep)
    async with c:
        accounts = await c.list_accounts()
    assert calls == 3 and len(accounts) == 3


async def test_limit_equal_to_total_is_not_truncated(fake_api):
    async with _client(fake_api) as c:
        txns = await c.list_transactions(limit=3)
    assert len(txns) == 3


@pytest.mark.parametrize(
    "url",
    [
        "https://api.mercury.com",
        "https://api-sandbox.mercury.com/",
        "http://localhost:8080",
        "http://127.0.0.1:9999",
        "http://LOCALHOST",
    ],
)
def test_api_base_accepts_https_and_loopback_http(url):
    c = MercuryClient(FAKE_TOKEN_MAIN, api_base=url)
    assert c.base_url == url.rstrip("/") + "/api/v1"


@pytest.mark.parametrize(
    "url",
    [
        "http://api.mercury.com",
        "http://evil.example",
        "ftp://api.mercury.com",
        "api.mercury.com",
        "https://api.mercury.com/api/v1",
        "https://api.mercury.com?x=1",
    ],
)
def test_api_base_rejects_non_https_and_paths(url):
    with pytest.raises(ValueError, match="api_base"):
        MercuryClient(FAKE_TOKEN_MAIN, api_base=url)


def test_api_base_env_rejected_when_insecure(monkeypatch):
    monkeypatch.setenv("MERCURY_API_BASE", "http://api.mercury.com")
    with pytest.raises(ValueError, match="https"):
        MercuryClient(FAKE_TOKEN_MAIN)


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


# -- Phase 3 client additions -------------------------------------------------


def test_validate_path_id_rejects_anything_that_could_change_the_path():
    from mercury_multiorg_mcp.client import validate_path_id

    assert validate_path_id("11111111-1111-4111-8111-111111111111", "x") == "11111111-1111-4111-8111-111111111111"
    for bad in ("", "../accounts", "a/b", "id?x=1", "id#frag", "id with space", "x" * 65, None, 42, "abc\n", "abc\r"):
        with pytest.raises(ValueError, match="must be an id"):
            validate_path_id(bad, "thing")  # type: ignore[arg-type]


async def test_download_caps_by_declared_length_and_by_stream(fake_api):
    stmt = "66666666-0001-4666-8666-666666666666"
    async with _client(fake_api) as c:
        body, ctype = await c.get_statement_pdf(stmt)
        assert body.startswith(b"%PDF") and ctype == "application/pdf"
        fake_api.pdf_bytes = b"%PDF-1.4\n" + b"y" * 100
        with pytest.raises(MercuryAPIError, match="above the 50-byte limit"):
            await c.get_statement_pdf(stmt, max_bytes=50)
        fake_api.pdf_send_content_length = False
        with pytest.raises(MercuryAPIError, match="exceeded the 50-byte limit"):
            await c.get_statement_pdf(stmt, max_bytes=50)
        # a body exactly at the cap is fine
        fake_api.pdf_bytes = b"%PDF-" + b"z" * 45
        body, _ = await c.get_invoice_pdf("1a000000-0001-4a00-8a00-1a0000000000", max_bytes=50)
        assert len(body) == 50


async def test_download_retries_429_and_redacts_error_bodies(fake_api):
    stmt = "66666666-0001-4666-8666-666666666666"
    fake_api.rate_limit_first = 2
    async with _client(fake_api, max_retries=3) as c:
        body, _ = await c.get_statement_pdf(stmt)
    assert body.startswith(b"%PDF")
    assert len(fake_api.requests) == 3
    fake2 = FakeMercury()
    fake2.force_status = 403
    fake2.force_body = f"denied for Authorization: Bearer {FAKE_TOKEN_MAIN}"
    async with _client(fake2) as c:
        with pytest.raises(MercuryAPIError) as info:
            await c.get_statement_pdf(stmt)
    assert info.value.status_code == 403 and FAKE_TOKEN_MAIN not in str(info.value) and "[REDACTED]" in str(info.value)


async def test_treasury_transactions_int_cursor_stop_and_guards(fake_api, monkeypatch):
    import mercury_multiorg_mcp.client as client_mod

    treasury = "33333333-3333-4333-8333-333333333333"
    monkeypatch.setattr(client_mod, "MAX_PAGE_SIZE", 2)
    async with _client(fake_api) as c:
        rows = await c.list_treasury_transactions(treasury)
        assert len(rows) == 7
        cursors = [r.url.params.get("cursor") for r in fake_api.requests]
        assert cursors == [None, "2", "4", "6"]
        # limit caps both the walk and the result
        fake_api.requests.clear()
        rows = await c.list_treasury_transactions(treasury, limit=3)
        assert len(rows) == 3 and len(fake_api.requests) == 2
        # stop_at ends the walk on the page where it first matches
        fake_api.requests.clear()
        rows = await c.list_treasury_transactions(treasury, stop_at=lambda r: r["canonicalDay"] < "2026-03-01")
        assert [r["canonicalDay"] for r in rows] == ["2026-03-31", "2026-03-15", "2026-03-01"]
        assert len(fake_api.requests) == 2
        with pytest.raises(ValueError):
            await c.list_treasury_transactions(treasury, order="sideways")

    # a server whose cursor never advances must not loop
    calls = 0

    def stuck(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"transactions": [{"id": f"t{calls}", "canonicalDay": "2026-01-01"}], "cursor": 1})

    c = MercuryClient(FAKE_TOKEN_MAIN, api_base=FAKE_API_BASE, transport=httpx.MockTransport(stuck), sleep=_no_sleep)
    async with c:
        rows = await c.list_treasury_transactions(treasury)
    assert calls == 2 and len(rows) == 2

    # exhausting MAX_PAGES with more remaining is an error
    monkeypatch.setattr(client_mod, "MAX_PAGES", 2)
    calls = 0

    def endless(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"transactions": [{"id": f"t{calls}"}], "cursor": calls})

    c = MercuryClient(FAKE_TOKEN_MAIN, api_base=FAKE_API_BASE, transport=httpx.MockTransport(endless), sleep=_no_sleep)
    async with c:
        with pytest.raises(MercuryAPIError, match="more than 2 pages"):
            await c.list_treasury_transactions(treasury)
        rows = await c.list_treasury_transactions(treasury, limit=2)  # satisfied exactly: fine
        assert len(rows) == 2


async def test_paginate_stop_at_drops_the_matching_item_and_everything_after(fake_api, monkeypatch):
    import mercury_multiorg_mcp.client as client_mod

    monkeypatch.setattr(client_mod, "MAX_PAGE_SIZE", 2)
    async with _client(fake_api) as c:
        rows = await c.list_events(order="desc", stop_at=lambda e: e["occurredAt"] < "2026-03-03")
    assert [r["id"][-1] for r in rows] == ["6", "5", "4", "3"]
    assert len(fake_api.requests) == 3  # pages [6,5], [4,3], [2,1] -> stop at 2; nothing after it is fetched


async def test_phase3_client_methods_validate_ids_before_any_request(fake_api):
    async with _client(fake_api) as c:
        for call in (
            lambda: c.list_account_statements("../x"),
            lambda: c.get_statement_pdf("a/b"),
            lambda: c.list_treasury_transactions(""),
            lambda: c.list_treasury_statements("id?x"),
            lambda: c.get_card("..%2F"),
            lambda: c.get_invoice("x y"),
            lambda: c.get_invoice_pdf("x/pdf"),
            lambda: c.list_invoice_attachments("#"),
        ):
            with pytest.raises(ValueError, match="must be an id"):
                await call()
    assert fake_api.requests == []


async def test_unexpected_shapes_are_clean_errors(fake_api):
    fake_api.force_status = 200
    fake_api.force_body = '{"unexpected": true}'
    async with _client(fake_api) as c:
        with pytest.raises(MercuryAPIError, match="missing 'organization'"):
            await c.get_organization()
        with pytest.raises(MercuryAPIError, match="missing 'accounts'"):
            await c.list_credit_accounts()
        with pytest.raises(MercuryAPIError, match="missing 'transactions'"):
            await c.list_treasury_transactions("33333333-3333-4333-8333-333333333333")
        with pytest.raises(MercuryAPIError, match="missing 'attachments'"):
            await c.list_invoice_attachments("1a000000-0001-4a00-8a00-1a0000000000")


async def test_invalid_url_from_httpx_is_a_clean_error(fake_api):
    """An id that passes nowhere near validate_path_id (internal misuse) still cannot escape as a raw httpx error."""
    async with _client(fake_api) as c:
        with pytest.raises(MercuryAPIError, match="HTTP error calling GET"):
            await c._get("/cards/abc\n")
    assert fake_api.requests == []


async def test_treasury_int_cursor_walk_dedupes_overlapping_pages():
    pages = {None: ([{"id": "t1"}, {"id": "t2"}], 2), 2: ([{"id": "t2"}, {"id": "t3"}], None)}

    def overlapping(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        rows, nxt = pages[int(cursor) if cursor else None]
        return httpx.Response(200, json={"transactions": rows, "cursor": nxt})

    c = MercuryClient(FAKE_TOKEN_MAIN, api_base=FAKE_API_BASE, transport=httpx.MockTransport(overlapping), sleep=_no_sleep)
    async with c:
        rows = await c.list_treasury_transactions("33333333-3333-4333-8333-333333333333")
    assert [r["id"] for r in rows] == ["t1", "t2", "t3"]


async def test_error_body_unreadable_on_streamed_response_is_wrapped():
    class Boom(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadError("gone")
            yield b""  # pragma: no cover

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, stream=Boom())

    c = MercuryClient(FAKE_TOKEN_MAIN, api_base=FAKE_API_BASE, transport=httpx.MockTransport(handler), sleep=_no_sleep)
    async with c:
        with pytest.raises(MercuryAPIError, match="HTTP 403 .* body unreadable") as info:
            await c.get_statement_pdf("66666666-0001-4666-8666-666666666666")
    assert info.value.status_code == 403


def test_api_base_rejects_credentials_without_echoing_them():
    from mercury_multiorg_mcp.client import validate_api_base

    for bad in ("https://user:hunter2@api.mercury.com", "https://user@api.mercury.com", "https://api.mercury.com:notaport"):
        with pytest.raises(ValueError) as info:
            validate_api_base(bad)
        assert "hunter2" not in str(info.value) and "user:" not in str(info.value) and "user@" not in str(info.value)
    assert validate_api_base("https://api-sandbox.mercury.com/") == "https://api-sandbox.mercury.com"
    # a credential hidden in the path or query is never echoed either
    for bad in ("https://api.mercury.com/?token=hunter2", "https://api.mercury.com/hunter2", "http://api.mercury.com/?k=hunter2"):
        with pytest.raises(ValueError) as info:
            validate_api_base(bad)
        assert "hunter2" not in str(info.value)


async def test_error_body_on_streamed_response_is_read_to_the_cap_only():
    import mercury_multiorg_mcp.client as client_mod

    yielded = 0

    class Big(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal yielded
            for _ in range(1000):  # 1000 x 1 KiB = far beyond the 64 KiB cap
                yielded += 1
                yield b"x" * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, stream=Big(), headers={"Content-Type": "text/plain"})

    c = MercuryClient(FAKE_TOKEN_MAIN, api_base=FAKE_API_BASE, transport=httpx.MockTransport(handler), sleep=_no_sleep, max_retries=0)
    async with c:
        with pytest.raises(MercuryAPIError) as info:
            await c.get_statement_pdf("66666666-0001-4666-8666-666666666666")
    assert yielded <= client_mod.ERROR_BODY_CAP // 1024 + 1
    assert len(str(info.value)) < 700 and "HTTP 500" in str(info.value)


async def test_treasury_cursor_guard_is_non_advancing_only():
    """A decreasing cursor is unusual but not a loop by itself; only a repeated cursor stops the walk."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        cursor = request.url.params.get("cursor")
        nxt = {None: 5, 5: 3, 3: None}[int(cursor) if cursor else None]
        return httpx.Response(200, json={"transactions": [{"id": f"t{calls}"}], "cursor": nxt})

    c = MercuryClient(FAKE_TOKEN_MAIN, api_base=FAKE_API_BASE, transport=httpx.MockTransport(handler), sleep=_no_sleep)
    async with c:
        rows = await c.list_treasury_transactions("33333333-3333-4333-8333-333333333333")
    assert [r["id"] for r in rows] == ["t1", "t2", "t3"] and calls == 3
