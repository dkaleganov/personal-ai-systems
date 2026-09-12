from mercury_multiorg_mcp.errors import REDACTED, MercuryAPIError, redact, token_suffix

TOKEN = "secret-token:mercury_test_fake_main_ABCDEFGH1234"


def test_token_suffix_is_last_four_only():
    assert token_suffix(TOKEN) == "1234"
    assert token_suffix("") == ""
    assert token_suffix(None) == ""


def test_redact_scrubs_authorization_header_forms():
    assert TOKEN not in redact(f"Authorization: Bearer {TOKEN}")
    assert TOKEN not in redact(f"{{'authorization': 'Bearer {TOKEN}'}}")
    assert TOKEN not in redact(f"authorization={TOKEN}")


def test_redact_scrubs_token_shape_without_header():
    out = redact(f"failed with {TOKEN} in body")
    assert TOKEN not in out
    assert REDACTED in out


def test_redact_scrubs_explicit_secret_even_if_unshaped():
    secret = "plainlongsecretvalue"
    assert secret not in redact(f"x={secret}", secret)


def test_redact_ignores_short_secrets_to_avoid_mangling_text():
    assert redact("abc abc", "abc") == "abc abc"


def test_api_error_message_is_redacted_on_construction():
    err = MercuryAPIError(f"boom {TOKEN}", status_code=401, path="/accounts")
    assert TOKEN not in str(err)
    assert err.status_code == 401
    assert err.path == "/accounts"
