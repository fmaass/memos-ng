import hashlib
import json
from pathlib import Path

from app.config import settings
from PIL import Image
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "live-0.29.1"


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
            text("INSERT INTO tags (name) VALUES (:n) ON CONFLICT (name) DO UPDATE SET name=:n "
                 "RETURNING id"),
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
        c.execute(
            text("INSERT INTO pats (hashed_token, name, user_id) VALUES (:h,'compat',:u)"),
            {"h": hashlib.sha256(token.encode()).hexdigest(), "u": uid},
        )
    eng.dispose()
    return token


def test_compat_envelope_and_fields_match_fixture(client):
    token = _seed_world()
    try:
        fx = json.loads((FIXTURES / "01_memos_list.json").read_text())
        fx_memo, fx_att = fx["memos"][0], fx["memos"][0]["attachments"][0]

        body = client.get("/api/v1/memos").json()
        assert set(body.keys()) == {"memos", "nextPageToken"}  # 0.29.1 envelope
        memo = next(m for m in body["memos"] if m["name"] == "memos/cmp-pub")
        assert set(memo.keys()) == set(fx_memo.keys())  # exact 0.29.1 memo field set
        assert memo["content"] == "Public #curated memo" and memo["visibility"] == "PUBLIC"
        att = memo["attachments"][0]
        assert set(att.keys()) == set(fx_att.keys())  # exact attachment field set
        assert att["name"] == "attachments/cmp-att"
        assert att["size"] == "5797027" and isinstance(att["size"], str)  # size is a STRING
        assert att["content"] == "" and att["motionMedia"] is None
    finally:
        _cleanup("compat_owner")
        del token


def test_f1_no_auth_returns_200_public_only(client):
    """F1 ground truth: no/bad token → HTTP 200 PUBLIC-only, NOT 401."""
    _seed_world()
    try:
        body = client.get("/api/v1/memos").json()
        vis = {m["visibility"] for m in body["memos"]}
        names = {m["name"] for m in body["memos"]}
        assert client.get("/api/v1/memos").status_code == 200  # not 401
        assert vis <= {"PUBLIC"}  # private/protected hidden from anonymous
        assert "memos/cmp-priv" not in names
    finally:
        _cleanup("compat_owner")


def test_pat_widens_to_private(client):
    token = _seed_world()
    try:
        body = client.get("/api/v1/memos", headers={"Authorization": f"Bearer {token}"}).json()
        names = {m["name"] for m in body["memos"]}
        assert "memos/cmp-priv" in names  # PAT owner sees their PRIVATE memo
    finally:
        _cleanup("compat_owner")


def test_cel_filter_tag_in_works_eq_400s(client):
    _seed_world()
    try:
        ok = client.get('/api/v1/memos?filter=tag in ["curated"]')
        assert ok.status_code == 200
        assert {m["name"] for m in ok.json()["memos"]} == {"memos/cmp-pub"}
        # tag== is rejected exactly as 0.29.1 does (04_filter_tag_eq_BROKEN.json → 400)
        assert client.get('/api/v1/memos?filter=tag == "curated"').status_code == 400
    finally:
        _cleanup("compat_owner")


def test_pagination_envelope_terminates(client):
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        uid = _seed_user(c, "compat_pager")
        for i in range(3):
            _seed_memo(c, uid, f"pg-{i}", f"m{i}", "PUBLIC")
    eng.dispose()
    try:
        first = client.get("/api/v1/memos?pageSize=2").json()
        assert len(first["memos"]) == 2 and first["nextPageToken"]
        second = client.get(f"/api/v1/memos?pageSize=2&pageToken={first['nextPageToken']}").json()
        assert len(second["memos"]) == 1 and not second["nextPageToken"]  # terminates
    finally:
        _cleanup("compat_pager")


def test_single_memo_compat(client):
    _seed_world()
    try:
        assert client.get("/api/v1/memos/cmp-pub").json()["name"] == "memos/cmp-pub"
        assert client.get("/api/v1/memos/cmp-priv").status_code == 404  # PRIVATE, anonymous
        assert client.get("/api/v1/memos/nope").status_code == 404
    finally:
        _cleanup("compat_owner")


def test_f2_file_serves_derived_tiers_only(client, monkeypatch, tmp_path):
    """F2 ground truth: original /file/ → 404; ?thumbnail=true → the thumb tier."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    thumb = tmp_path / "assets" / "thumbs" / "fu.jpg"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (1, 2, 3)).save(thumb, "JPEG")

    # original (no thumbnail, image) → 404 (not served, as prod today)
    assert client.get("/file/attachments/fu/IMG.jpeg").status_code == 404
    # ?thumbnail=true → 200 image/jpeg
    r = client.get("/file/attachments/fu/IMG.jpeg?thumbnail=true")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
