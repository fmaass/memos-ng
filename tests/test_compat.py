import hashlib

import pytest
from app.config import settings
from PIL import Image
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL

# The memos 0.29.1 contract field sets, from fixtures/live-0.29.1/NOTES.md (the captured live
# ground truth). Hardcoded here so the contract test is self-contained and CI-safe — the real
# fixtures are gitignored because they contain private family memo content.
MEMO_KEYS = {
    "name",
    "state",
    "creator",
    "createTime",
    "updateTime",
    "content",
    "visibility",
    "tags",
    "pinned",
    "attachments",
    "relations",
    "reactions",
    "property",
    "snippet",
}
ATTACHMENT_KEYS = {
    "name",
    "createTime",
    "filename",
    "content",
    "externalLink",
    "type",
    "size",
    "memo",
    "motionMedia",
}


def _seed_user(c, subject):
    return c.execute(
        text("INSERT INTO users (subject, role) VALUES (:s,'user') RETURNING id"), {"s": subject}
    ).scalar_one()


def _seed_memo(c, owner_id, uid, content, visibility, tag=None):
    mid = c.execute(
        text(
            "INSERT INTO memos (uid, owner_id, content, visibility, pinned) "
            "VALUES (:u,:o,:c,CAST(:v AS visibility), false) RETURNING id"
        ),
        {"u": uid, "o": owner_id, "c": content, "v": visibility},
    ).scalar_one()
    if tag:
        tid = c.execute(
            text(
                "INSERT INTO tags (name) VALUES (:n) ON CONFLICT (name) DO UPDATE SET name=:n "
                "RETURNING id"
            ),
            {"n": tag},
        ).scalar_one()
        c.execute(
            text("INSERT INTO memo_tags (memo_id, tag_id) VALUES (:m,:t)"), {"m": mid, "t": tid}
        )
    return mid


def _seed_attachment(c, owner_id, memo_id, uid, filename, mime, size):
    c.execute(
        text(
            "INSERT INTO attachments (uid, owner_id, memo_id, filename, mime, size, status) "
            "VALUES (:u,:o,:m,:f,:mi,:s,CAST('ready' AS attachment_status))"
        ),
        {"u": uid, "o": owner_id, "m": memo_id, "f": filename, "mi": mime, "s": size},
    )


def _seed_pat(c, user_id, token):
    c.execute(
        text("INSERT INTO pats (hashed_token, name, user_id) VALUES (:h,'compat',:u)"),
        {"h": hashlib.sha256(token.encode()).hexdigest(), "u": user_id},
    )
    return {"Authorization": f"Bearer {token}"}


READER_TOKEN = "memos_pat_compatreader1234567890"
READER = {"Authorization": f"Bearer {READER_TOKEN}"}


def _cleanup(*subjects):
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        for s in subjects:
            c.execute(text("DELETE FROM users WHERE subject=:s"), {"s": s})  # cascades
        # tags are global (not user-cascaded) — remove the seeded one so it doesn't collide
        # with other suites that insert the same name.
        c.execute(text("DELETE FROM tags WHERE name='curated'"))
    eng.dispose()


def _seed_world():
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        uid = _seed_user(c, "compat_owner")
        pub = _seed_memo(c, uid, "cmp-pub", "Public #curated memo", "PUBLIC", tag="curated")
        _seed_attachment(c, uid, pub, "cmp-att", "IMG.jpeg", "image/jpeg", 5797027)
        _seed_memo(c, uid, "cmp-priv", "Private memo", "PRIVATE")
        # a PAT for the widen test
        token = "memos_pat_compattoken1234567890"
        _seed_pat(c, uid, token)
        # another household member, who sees the PUBLIC memo and never the owner's PRIVATE one
        _seed_pat(c, _seed_user(c, "compat_reader"), READER_TOKEN)
    eng.dispose()
    return token


def test_compat_envelope_and_fields_match_contract(client):
    _seed_world()
    try:
        body = client.get("/api/v1/memos", headers=READER).json()
        assert set(body.keys()) == {"memos", "nextPageToken"}  # 0.29.1 envelope
        memo = next(m for m in body["memos"] if m["name"] == "memos/cmp-pub")
        assert set(memo.keys()) == MEMO_KEYS  # exact 0.29.1 memo field set
        assert memo["content"] == "Public #curated memo" and memo["visibility"] == "PUBLIC"
        att = memo["attachments"][0]
        assert set(att.keys()) == ATTACHMENT_KEYS  # exact attachment field set
        assert att["name"] == "attachments/cmp-att"
        assert att["size"] == "5797027" and isinstance(att["size"], str)  # size is a STRING
        assert att["content"] == "" and att["motionMedia"] is None
    finally:
        _cleanup("compat_owner", "compat_reader")


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer memos_pat_unknown1234567890"},
        {"Authorization": "Bearer not-a-pat"},
        {"Authorization": "Bearer "},
        # the real owner's token, but in another scheme: it states no bearer claim, so it is no
        # credential at all here and must not be read out of the header anyway
        {"Authorization": "Basic memos_pat_compattoken1234567890"},
    ],
    ids=["none", "unknown-pat", "not-a-pat", "empty", "token-in-another-scheme"],
)
def test_no_or_bad_token_is_401_not_the_public_slice(client, headers):
    """Departs from F1 on purpose (MEMNG-835): 0.29.1 answered a missing or bad token with 200
    and its PUBLIC memos. PUBLIC here means every signed-in member, so a caller with no valid
    credential gets nothing — not even a PUBLIC memo's existence."""
    _seed_world()
    try:
        assert client.get("/api/v1/memos", headers=headers).status_code == 401
        assert client.get("/api/v1/memos/cmp-pub", headers=headers).status_code == 401
        assert client.get("/api/v1/memos/nope", headers=headers).status_code == 401
    finally:
        _cleanup("compat_owner", "compat_reader")


def test_a_session_cookie_reads_the_compat_feed(logged_in_client):
    """`require_pat_or_user` takes the browser session too, so the SPA's own login reaches it."""
    client, _ = logged_in_client
    _seed_world()
    try:
        names = {m["name"] for m in client.get("/api/v1/memos").json()["memos"]}
        assert "memos/cmp-pub" in names and "memos/cmp-priv" not in names
    finally:
        _cleanup("compat_owner", "compat_reader")


def test_pat_widens_to_private(client):
    token = _seed_world()
    try:
        body = client.get("/api/v1/memos", headers={"Authorization": f"Bearer {token}"}).json()
        names = {m["name"] for m in body["memos"]}
        assert "memos/cmp-priv" in names  # PAT owner sees their PRIVATE memo
        others = {m["name"] for m in client.get("/api/v1/memos", headers=READER).json()["memos"]}
        assert "memos/cmp-pub" in others and "memos/cmp-priv" not in others
    finally:
        _cleanup("compat_owner", "compat_reader")


def test_cel_filter_tag_in_works_eq_400s(client):
    _seed_world()
    try:
        ok = client.get('/api/v1/memos?filter=tag in ["curated"]', headers=READER)
        assert ok.status_code == 200
        assert {m["name"] for m in ok.json()["memos"]} == {"memos/cmp-pub"}
        # tag== is rejected exactly as 0.29.1 does (04_filter_tag_eq_BROKEN.json → 400)
        bad = client.get('/api/v1/memos?filter=tag == "curated"', headers=READER)
        assert bad.status_code == 400
    finally:
        _cleanup("compat_owner", "compat_reader")


def test_pagination_envelope_terminates(client):
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        uid = _seed_user(c, "compat_pager")
        for i in range(3):
            _seed_memo(c, uid, f"pg-{i}", f"m{i}", "PUBLIC")
        pager = _seed_pat(c, uid, "memos_pat_compatpager1234567890")
    eng.dispose()
    try:
        first = client.get("/api/v1/memos?pageSize=2", headers=pager).json()
        assert len(first["memos"]) == 2 and first["nextPageToken"]
        second = client.get(
            f"/api/v1/memos?pageSize=2&pageToken={first['nextPageToken']}", headers=pager
        ).json()
        assert len(second["memos"]) == 1 and not second["nextPageToken"]  # terminates
    finally:
        _cleanup("compat_pager")


def test_single_memo_compat(client):
    token = _seed_world()
    try:
        assert client.get("/api/v1/memos/cmp-pub", headers=READER).json()["name"] == "memos/cmp-pub"
        # someone else's PRIVATE memo is as absent as one that does not exist
        assert client.get("/api/v1/memos/cmp-priv", headers=READER).status_code == 404
        assert client.get("/api/v1/memos/nope", headers=READER).status_code == 404
        owner = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/v1/memos/cmp-priv", headers=owner).status_code == 200
    finally:
        _cleanup("compat_owner", "compat_reader")


def test_f2_file_serves_derived_tiers_only(client, monkeypatch, tmp_path):
    """F2 ground truth: original /file/ → 404; ?thumbnail=true → the thumb tier.

    Both assertions are the captured contract, unchanged. The attachment is now seeded on a
    PUBLIC memo because derived tiers are visibility-gated since MEMNG-25 — F2 fixes which
    TIERS are served, and says nothing about serving a uid that has no attachment row.
    """
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    thumb = tmp_path / "assets" / "thumbs" / "fu.jpg"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (1, 2, 3)).save(thumb, "JPEG")
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        owner = _seed_user(c, "compat_owner")
        memo = _seed_memo(c, owner, "cmp-pub", "Public memo", "PUBLIC")
        _seed_attachment(c, owner, memo, "fu", "IMG.jpeg", "image/jpeg", 1)
        h = _seed_pat(c, owner, "memos_pat_compatfile1234567890")
    eng.dispose()
    try:
        # original (no thumbnail, image) → 404 (not served, as prod today)
        assert client.get("/file/attachments/fu/IMG.jpeg", headers=h).status_code == 404
        # ?thumbnail=true → 200 image/jpeg
        r = client.get("/file/attachments/fu/IMG.jpeg?thumbnail=true", headers=h)
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
        # and neither is answered at all without a credential
        assert client.get("/file/attachments/fu/IMG.jpeg").status_code == 401
        assert client.get("/file/attachments/fu/IMG.jpeg?thumbnail=true").status_code == 401
    finally:
        _cleanup("compat_owner")
