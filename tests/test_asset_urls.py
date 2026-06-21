import re

from app.config import settings
from app.services import asset_urls


def _q(url: str) -> dict[str, str]:
    return dict(re.findall(r"[?&]([^=]+)=([^&]+)", url))


def _fixed(monkeypatch, secret="test-secret", prev=""):
    monkeypatch.setattr(settings, "ASSET_URL_SECRET", secret)
    monkeypatch.setattr(settings, "ASSET_URL_SECRET_PREV", prev)
    monkeypatch.setattr(settings, "ASSET_URL_BUCKET", 3600)
    monkeypatch.setattr(settings, "ASSET_URL_TTL", 7200)


def test_sign_verify_roundtrip(monkeypatch):
    _fixed(monkeypatch)
    url = asset_urls.sign_asset_url("uidA", "thumb", 42, now=1000)
    q = _q(url)
    assert url.startswith("/api/attachments/uidA/thumb?")
    assert asset_urls.verify_asset_sig("uidA", "thumb", q["exp"], q["scope"], q["sig"], now=1000)


def test_tampered_or_wrong_fields_fail(monkeypatch):
    _fixed(monkeypatch)
    q = _q(asset_urls.sign_asset_url("uidA", "thumb", 42, now=1000))
    v = asset_urls.verify_asset_sig
    tampered = ("1" if q["sig"][0] == "0" else "0") + q["sig"][1:]  # flip first hex char
    assert not v("uidA", "thumb", q["exp"], q["scope"], tampered, now=1000)  # tampered sig
    assert not v("uidB", "thumb", q["exp"], q["scope"], q["sig"], now=1000)  # wrong uid
    assert not v("uidA", "mobile", q["exp"], q["scope"], q["sig"], now=1000)  # wrong tier
    assert not v("uidA", "thumb", q["exp"], "99", q["sig"], now=1000)  # wrong scope
    assert not v("uidA", "thumb", q["exp"], q["scope"], None, now=1000)  # missing sig
    assert not v("uidA", "thumb", None, q["scope"], q["sig"], now=1000)  # missing exp


def test_expired_fails(monkeypatch):
    _fixed(monkeypatch)
    q = _q(asset_urls.sign_asset_url("uidA", "thumb", 42, now=1000))  # exp = 0 + 7200
    v = asset_urls.verify_asset_sig
    assert not v("uidA", "thumb", q["exp"], q["scope"], q["sig"], now=99999)


def test_bucketing_is_cache_stable(monkeypatch):
    _fixed(monkeypatch)
    a = asset_urls.sign_asset_url("uidA", "thumb", 42, now=1000)
    b = asset_urls.sign_asset_url("uidA", "thumb", 42, now=3500)  # same bucket [0,3600) → identical
    c = asset_urls.sign_asset_url("uidA", "thumb", 42, now=4000)  # next bucket → different
    assert a == b
    assert a != c


def test_rotation_ring(monkeypatch):
    # a URL signed with the OLD key still verifies after rotation (old key moves to PREV)
    _fixed(monkeypatch, secret="old-key")
    q = _q(asset_urls.sign_asset_url("uidA", "thumb", 42, now=1000))
    _fixed(monkeypatch, secret="new-key", prev="old-key")
    assert asset_urls.verify_asset_sig("uidA", "thumb", q["exp"], q["scope"], q["sig"], now=1000)
