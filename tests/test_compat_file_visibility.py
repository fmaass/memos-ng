"""MEMNG-25: /file/* derived tiers are visibility-gated, so a flip/deletion revokes them.

Before this, `serve_file_compat` did no memo lookup at all: any derived tier of any memo was
anonymously fetchable by uid forever, including after the memo went PRIVATE or was deleted.
Each test here drives the real route through the real DB — remove the authorization branch it
covers and it goes red (mutation-verified, see the phase ledger).
"""

import hashlib

from app.config import settings
from PIL import Image
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL

MOBILE_UA = {"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Mobile/15E148"}
OWNER = "filevis_owner"
OTHER = "filevis_other"
OWNER_PAT = "memos_pat_filevisowner0000000000"
OTHER_PAT = "memos_pat_filevisother0000000000"


def _seed(visibility: str = "PUBLIC"):
    """One owner (+PAT), one other user (+PAT), one memo with an image and a video attachment."""
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        owner = c.execute(
            text("INSERT INTO users (subject, role) VALUES (:s,'user') RETURNING id"),
            {"s": OWNER},
        ).scalar_one()
        other = c.execute(
            text("INSERT INTO users (subject, role) VALUES (:s,'user') RETURNING id"),
            {"s": OTHER},
        ).scalar_one()
        for token, uid in ((OWNER_PAT, owner), (OTHER_PAT, other)):
            c.execute(
                text("INSERT INTO pats (hashed_token, name, user_id) VALUES (:h,'fv',:u)"),
                {"h": hashlib.sha256(token.encode()).hexdigest(), "u": uid},
            )
        memo = c.execute(
            text(
                "INSERT INTO memos (uid, owner_id, content, visibility, pinned) "
                "VALUES ('fv-memo',:o,'media memo',CAST(:v AS visibility),false) RETURNING id"
            ),
            {"o": owner, "v": visibility},
        ).scalar_one()
        for uid, filename, mime in (
            ("fv-img", "IMG.jpeg", "image/jpeg"),
            ("fv-vid", "CLIP.mov", "video/quicktime"),
        ):
            c.execute(
                text(
                    "INSERT INTO attachments (uid, owner_id, memo_id, filename, mime, size, "
                    "status) VALUES (:u,:o,:m,:f,:mi,1,CAST('ready' AS attachment_status))"
                ),
                {"u": uid, "o": owner, "m": memo, "f": filename, "mi": mime},
            )
    eng.dispose()


def _cleanup():
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        for s in (OWNER, OTHER):
            c.execute(text("DELETE FROM users WHERE subject=:s"), {"s": s})  # cascades
    eng.dispose()


def _exec(sql: str, **params):
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text(sql), params)
    eng.dispose()


def _assets(tmp_path):
    """Materialise every derived tier on disk, so a 404 can only come from authorization."""
    for sub, name in (("thumbs", "fv-img.jpg"), ("mobile", "fv-img.jpg")):
        p = tmp_path / "assets" / sub / name
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), (1, 2, 3)).save(p, "JPEG")
    web = tmp_path / "assets" / "web" / "fv-vid.mp4"
    web.parent.mkdir(parents=True, exist_ok=True)
    web.write_bytes(b"\x00\x00\x00\x18ftypmp42")


# Another household member: the caller PUBLIC and PROTECTED media are for. Since MEMNG-835 nobody
# without a credential reads any tier, so the visibility rules are exercised through this caller.
MEMBER = {"Authorization": f"Bearer {OTHER_PAT}"}
ANON_REFUSED = (401, 401, 401)


def _tiers(client, headers=None):
    """(thumb, mobile, video) status codes for the seeded attachments."""
    h = headers or {}
    return (
        client.get("/file/attachments/fv-img/IMG.jpeg?thumbnail=true", headers=h).status_code,
        client.get("/file/attachments/fv-img/IMG.jpeg", headers={**MOBILE_UA, **h}).status_code,
        client.get("/file/attachments/fv-vid/CLIP.mov", headers=h).status_code,
    )


def test_public_to_private_flip_revokes_every_derived_tier(client, monkeypatch, tmp_path):
    """AC1: a member reads every tier while PUBLIC; after the flip, every tier is 404."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _assets(tmp_path)
    _seed("PUBLIC")
    try:
        assert _tiers(client, MEMBER) == (200, 200, 200)
        _exec("UPDATE memos SET visibility='PRIVATE' WHERE uid='fv-memo'")
        assert _tiers(client, MEMBER) == (404, 404, 404)  # revoked on the very next request
        _exec("UPDATE memos SET visibility='PUBLIC' WHERE uid='fv-memo'")
        assert _tiers(client, MEMBER) == (200, 200, 200)  # and restored on a flip back
        assert _tiers(client) == ANON_REFUSED  # PUBLIC is never the internet
    finally:
        _cleanup()


def test_protected_is_not_anonymous_but_is_household_visible(client, monkeypatch, tmp_path):
    """PROTECTED mirrors the memo surfaces: refused to anonymous, visible to any user."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _assets(tmp_path)
    _seed("PROTECTED")
    try:
        assert _tiers(client) == ANON_REFUSED
        auth = {"Authorization": f"Bearer {OTHER_PAT}"}
        assert _tiers(client, auth) == (200, 200, 200)
    finally:
        _cleanup()


def test_memo_deletion_revokes_derived_tiers(client, monkeypatch, tmp_path):
    """AC3/deletion: the memo goes away (attachments.memo_id → NULL) → the media is gone too."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _assets(tmp_path)
    _seed("PUBLIC")
    try:
        assert _tiers(client, MEMBER) == (200, 200, 200)
        _exec("DELETE FROM memos WHERE uid='fv-memo'")
        assert _tiers(client, MEMBER) == (404, 404, 404)
        # the orphaned attachment is still the owner's, so a PAT of theirs may still read it
        assert _tiers(client, {"Authorization": f"Bearer {OWNER_PAT}"}) == (200, 200, 200)
    finally:
        _cleanup()


def test_attachment_deletion_and_unknown_uid_are_404(client, monkeypatch, tmp_path):
    """A deleted (or never-existing) attachment row is a denial, even with the file on disk."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _assets(tmp_path)
    _seed("PUBLIC")
    try:
        url = "/file/attachments/fv-img/IMG.jpeg?thumbnail=true"
        assert client.get(url, headers=MEMBER).status_code == 200
        _exec("DELETE FROM attachments WHERE uid='fv-img'")
        assert client.get(url, headers=MEMBER).status_code == 404
        owner = {"Authorization": f"Bearer {OWNER_PAT}"}
        assert (
            client.get("/file/attachments/fv-img/IMG.jpeg?thumbnail=true", headers=owner)
        ).status_code == 404  # not even the owner: the row is gone
        nosuch = "/file/attachments/nosuch/IMG.jpeg?thumbnail=true"
        assert client.get(nosuch, headers=MEMBER).status_code == 404
    finally:
        _cleanup()


def test_private_media_owner_pat_allowed_other_user_denied(client, monkeypatch, tmp_path):
    """AC3/ownership: on a PRIVATE memo the owner's PAT reads the tiers, another user's does not."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _assets(tmp_path)
    _seed("PRIVATE")
    try:
        assert _tiers(client, {"Authorization": f"Bearer {OWNER_PAT}"}) == (200, 200, 200)
        assert _tiers(client, {"Authorization": f"Bearer {OTHER_PAT}"}) == (404, 404, 404)
        assert _tiers(client) == ANON_REFUSED  # anonymous
        # an invalid PAT is refused like no credential at all — never downgraded to anonymous
        assert _tiers(client, {"Authorization": "Bearer memos_pat_bogus"}) == ANON_REFUSED
    finally:
        _cleanup()


def test_denial_is_indistinguishable_from_a_missing_file(client, monkeypatch, tmp_path):
    """404 (not 403) with the 0.29.1 body, so a denial never discloses that the uid exists."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _assets(tmp_path)
    _seed("PRIVATE")
    try:
        denied = client.get("/file/attachments/fv-img/IMG.jpeg?thumbnail=true", headers=MEMBER)
        unknown = client.get("/file/attachments/nosuch/IMG.jpeg?thumbnail=true", headers=MEMBER)
        assert denied.status_code == unknown.status_code == 404
        assert denied.json() == unknown.json() == {"message": "not found"}
    finally:
        _cleanup()


def test_served_tier_is_not_cacheable_by_a_shared_cache(client, monkeypatch, tmp_path):
    """A cache that outlived a flip would defeat the revocation — the response forbids storing."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _assets(tmp_path)
    _seed("PUBLIC")
    try:
        r = client.get("/file/attachments/fv-img/IMG.jpeg?thumbnail=true", headers=MEMBER)
        assert r.status_code == 200
        assert "no-store" in r.headers["cache-control"]
        assert "public" not in r.headers["cache-control"]
    finally:
        _cleanup()
