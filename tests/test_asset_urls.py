import re

import pytest
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


def test_signed_tier_urls_map_to_correct_tiers(monkeypatch):
    # Each derived field must point at its OWN tier — a mis-mapping (e.g. previewUrl→thumb) would
    # serve the wrong rendition yet still verify, so assert the path-to-tier binding directly.
    _fixed(monkeypatch)
    urls = asset_urls.signed_tier_urls("uidA", 7, now=1000)
    expected = {
        "thumbnailUrl": "thumb",
        "previewUrl": "mobile",
        "webUrl": "web",
        "posterUrl": "poster",
    }
    for field, tier in expected.items():
        assert urls[field].split("?")[0] == f"/api/attachments/uidA/{tier}"


def test_non_numeric_exp_and_none_scope_fail(monkeypatch):
    # Defensive, fail-closed branches in verify_asset_sig: a non-numeric exp must not raise, and a
    # missing scope must reject (these arrive from raw query params, so they can be malformed).
    _fixed(monkeypatch)
    q = _q(asset_urls.sign_asset_url("uidA", "thumb", 42, now=1000))
    v = asset_urls.verify_asset_sig
    assert not v("uidA", "thumb", "not-a-number", q["scope"], q["sig"], now=1000)  # ValueError path
    assert not v("uidA", "thumb", q["exp"], None, q["sig"], now=1000)  # scope is None


def test_scope_none_guard_rejects_even_a_crafted_matching_sig(monkeypatch):
    # Isolates the `scope is None` early-return: f"{None}" == f"{'None'}", so a sig crafted for the
    # string "None" WOULD verify if the guard were removed. The guard must reject the missing scope.
    _fixed(monkeypatch)
    exp = asset_urls.bucketed_exp(1000)
    crafted = asset_urls._sig("uidA", "thumb", exp, "None", asset_urls._secret())
    assert not asset_urls.verify_asset_sig("uidA", "thumb", exp, None, crafted, now=1000)


@pytest.mark.parametrize(
    ("mime", "tier"),
    [
        ("video/quicktime", "poster"),
        ("video/mp4", "poster"),
        ("image/jpeg", "thumb"),
        ("image/heic", "thumb"),
        ("application/pdf", "thumb"),
        ("image/x-video", "thumb"),  # "video" inside the mime is not a video
        ("", "thumb"),
    ],
)
def test_signed_cover_url_is_the_poster_for_a_video_else_the_thumb(monkeypatch, mime, tier):
    # A video has a poster and never a thumb (app/services/poster.py): a cover minted as its thumb
    # names a file that is never written.
    _fixed(monkeypatch)
    url = asset_urls.signed_cover_url("uidC", mime, 7, now=1000)
    assert url.split("?")[0] == f"/api/attachments/uidC/{tier}"
    q = _q(url)
    assert asset_urls.verify_asset_sig("uidC", tier, q["exp"], q["scope"], q["sig"], now=1000)
