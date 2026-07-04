import pytest
from app.config import _DEV_SESSION_SECRET, Settings


def test_is_production_true_when_env_not_development():
    # Gates docs_url/redoc_url exposure in app.main — must be True outside dev.
    assert Settings(ENV="production").is_production is True
    assert Settings(ENV="staging").is_production is True


def test_is_production_false_in_development():
    assert Settings(ENV="development").is_production is False


def test_validate_runtime_secrets_raises_on_default_in_production():
    # Production on the published dev-default key → forgeable signatures + decryptable tokens.
    s = Settings(ENV="production", SESSION_SECRET=_DEV_SESSION_SECRET)
    with pytest.raises(RuntimeError, match="dev default in production"):
        s.validate_runtime_secrets()


def test_validate_runtime_secrets_ok_when_overridden_in_production():
    # A real, non-default secret in prod is fine (no raise).
    Settings(ENV="production", SESSION_SECRET="a-real-unique-prod-key").validate_runtime_secrets()


def test_validate_runtime_secrets_noop_in_development():
    # Dev keeps the default and must not raise (the guard is production-only).
    Settings(ENV="development", SESSION_SECRET=_DEV_SESSION_SECRET).validate_runtime_secrets()


def test_smtp_credentials_excluded_from_repr():
    # SMTP creds must never surface in a repr()/structlog dump of the settings object.
    s = Settings(SMTP_USERNAME="sender@me.com", SMTP_PASSWORD="super-secret-app-pw")
    text = repr(s)
    assert "super-secret-app-pw" not in text
    assert "sender@me.com" not in text
    # ...but the values are still readable for use and drive the disable switch.
    assert s.SMTP_PASSWORD == "super-secret-app-pw"
    assert bool(s.SMTP_USERNAME and s.SMTP_PASSWORD) is True
