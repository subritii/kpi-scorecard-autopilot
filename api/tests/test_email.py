"""TEST_EMAIL_OVERRIDE: every outgoing email goes to plus-aliases of one test inbox."""

import pytest

from app import email

ME = "me@gmail.com"


@pytest.fixture
def override(monkeypatch):
    monkeypatch.setenv("TEST_EMAIL_OVERRIDE", ME)
    monkeypatch.delenv("EMAIL_MODE", raising=False)


@pytest.fixture
def live(monkeypatch):
    monkeypatch.delenv("TEST_EMAIL_OVERRIDE", raising=False)
    monkeypatch.setenv("EMAIL_MODE", "live")


def test_live_mode_sends_to_real_addresses(live):
    assert email.recipient("ceo@brightline.example") == "ceo@brightline.example"
    msg = email.compose(["ceo@brightline.example"], "Subject", "Body")
    assert msg == {"to": ["ceo@brightline.example"], "subject": "Subject", "body": "Body"}


def test_override_maps_each_recipient_to_a_plus_alias(override):
    assert email.recipient("ceo@brightline.example") == "me+ceo.brightline@gmail.com"
    assert email.recipient("avery.chen@brightline.example") == "me+avery.chen.brightline@gmail.com"


def test_same_role_at_two_clients_stays_distinct(override):
    # Why the alias keeps the domain label: ceo@ at each client is a different person.
    assert email.recipient("ceo@brightline.example") != email.recipient("ceo@northgate.example")


def test_alias_tags_are_safe_characters(override):
    assert (
        email.recipient("O'Brien+x@Weird_Domain.example") == "me+o-brien-x.weird-domain@gmail.com"
    )


def test_override_with_its_own_plus_tag_uses_the_base_address(monkeypatch):
    monkeypatch.setenv("TEST_EMAIL_OVERRIDE", "me+kpi@gmail.com")
    monkeypatch.delenv("EMAIL_MODE", raising=False)
    assert email.recipient("ceo@brightline.example") == "me+ceo.brightline@gmail.com"


def test_overridden_email_gets_a_banner_naming_the_original_recipients(override):
    msg = email.compose(["ceo@brightline.example", "coo@brightline.example"], "Packet", "Hello")
    assert msg["to"] == ["me+ceo.brightline@gmail.com", "me+coo.brightline@gmail.com"]
    assert msg["body"] == (
        "Test mode: originally addressed to ceo@brightline.example, coo@brightline.example\n\nHello"
    )
    assert msg["subject"] == "Packet"


@pytest.mark.parametrize("bad", ["not-an-email", "a@b@c", "me @gmail.com", "@gmail.com", "me@"])
def test_invalid_override_is_a_clear_error(monkeypatch, bad):
    monkeypatch.setenv("TEST_EMAIL_OVERRIDE", bad)
    monkeypatch.delenv("EMAIL_MODE", raising=False)
    with pytest.raises(email.EmailConfigError, match="must be an email address"):
        email.check_override()
    with pytest.raises(email.EmailConfigError):
        email.recipient("ceo@brightline.example")


# --- fail safe: real addresses only when asked for explicitly ---------------------------


@pytest.mark.parametrize("override_value", [None, "", "   "])
def test_no_override_and_no_mode_never_sends_to_real_addresses(monkeypatch, override_value):
    if override_value is None:
        monkeypatch.delenv("TEST_EMAIL_OVERRIDE", raising=False)
    else:
        monkeypatch.setenv("TEST_EMAIL_OVERRIDE", override_value)
    monkeypatch.delenv("EMAIL_MODE", raising=False)
    with pytest.raises(email.EmailConfigError, match="EMAIL_MODE=live"):
        email.check_override()
    # And at send time too, in case startup were bypassed.
    with pytest.raises(email.EmailConfigError):
        email.recipient("ceo@brightline.example")
    with pytest.raises(email.EmailConfigError):
        email.compose(["ceo@brightline.example"], "s", "b")


def test_live_mode_and_an_override_together_are_ambiguous(monkeypatch):
    monkeypatch.setenv("TEST_EMAIL_OVERRIDE", ME)
    monkeypatch.setenv("EMAIL_MODE", "live")
    with pytest.raises(email.EmailConfigError, match="both"):
        email.check_override()


@pytest.mark.parametrize("mode", ["Live", "production", "yes"])
def test_only_the_exact_word_live_turns_on_real_addresses(monkeypatch, mode):
    monkeypatch.delenv("TEST_EMAIL_OVERRIDE", raising=False)
    monkeypatch.setenv("EMAIL_MODE", mode)
    with pytest.raises(email.EmailConfigError, match="EMAIL_MODE"):
        email.check_override()


def test_api_refuses_to_start_with_an_invalid_override(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("TEST_EMAIL_OVERRIDE", "not-an-email")
    monkeypatch.delenv("EMAIL_MODE", raising=False)
    with pytest.raises(email.EmailConfigError, match="TEST_EMAIL_OVERRIDE"):
        with TestClient(app):  # runs the app's startup
            pass


def test_api_refuses_to_start_with_no_email_mode(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.delenv("TEST_EMAIL_OVERRIDE", raising=False)
    monkeypatch.delenv("EMAIL_MODE", raising=False)
    with pytest.raises(email.EmailConfigError, match="EMAIL_MODE=live"):
        with TestClient(app):
            pass


@pytest.mark.parametrize(
    "settings", [{"TEST_EMAIL_OVERRIDE": ME}, {"EMAIL_MODE": "live"}], ids=["test", "live"]
)
def test_api_starts_in_either_explicit_mode(monkeypatch, settings):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.delenv("TEST_EMAIL_OVERRIDE", raising=False)
    monkeypatch.delenv("EMAIL_MODE", raising=False)
    for k, v in settings.items():
        monkeypatch.setenv(k, v)
    with TestClient(app):
        pass
