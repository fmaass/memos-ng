from app.models import Attachment, Memo, Reaction, User, Visibility
from app.scripts.migrate_from_memos import epoch_to_dt, import_all
from sqlalchemy import select


def _dataset():
    return {
        "users": [
            {
                "id": 1, "username": "fabian", "role": "HOST", "email": "f@x",
                "nickname": "Fabian", "avatar_url": "data:image/png;base64,AAA",
                "theme": "default-dark", "default_visibility": "PUBLIC",
            },
            {
                "id": 3, "username": "laura", "role": "USER", "email": "l@x",
                "nickname": "laura", "avatar_url": "", "theme": None,
                "default_visibility": "PUBLIC",
            },
        ],
        "memos": [
            {
                "id": 10, "uid": "mig-pub", "creator_id": 1, "created_ts": 1700000000,
                "updated_ts": 1700000001, "row_status": "NORMAL", "content": "hello #curated",
                "visibility": "PUBLIC", "pinned": False, "payload": {"tags": ["curated"]},
            },
            {
                "id": 11, "uid": "mig-arch", "creator_id": 3, "created_ts": 1700000100,
                "updated_ts": 1700000200, "row_status": "ARCHIVED", "content": "old",
                "visibility": "PRIVATE", "pinned": True, "payload": {},
            },
        ],
        "attachments": [
            {
                "id": 20, "uid": "mig-att", "creator_id": 1, "filename": "a.jpg",
                "type": "image/jpeg", "size": 123, "memo_id": 10, "reference": "2024/a.jpg",
            },
            {
                "id": 21, "uid": "mig-orphan", "creator_id": 1, "filename": "o.jpg",
                "type": "image/jpeg", "size": 1, "memo_id": None, "reference": "2024/o.jpg",
            },
        ],
        "reactions": [{"memo_uid": "mig-pub", "subject": "fabian", "emoji": "👍"}],
    }


def test_import_maps_users_memos_attachments_reactions(db_session):
    counts = import_all(db_session, _dataset())
    assert counts == {"users": 2, "memos": 2, "attachments": 2, "reactions": 1}

    fabian = db_session.execute(select(User).where(User.subject == "fabian")).scalar_one()
    assert fabian.role == "admin"  # HOST → admin
    assert fabian.avatar and fabian.avatar.startswith("data:image/")
    assert fabian.default_visibility == Visibility.PUBLIC and fabian.theme == "default-dark"

    laura = db_session.execute(select(User).where(User.subject == "laura")).scalar_one()
    assert laura.role == "user" and laura.avatar is None  # empty avatar dropped

    pub = db_session.execute(select(Memo).where(Memo.uid == "mig-pub")).scalar_one()
    assert pub.owner_id == fabian.id and pub.visibility == Visibility.PUBLIC
    assert [t.name for t in pub.tags] == ["curated"]  # tags parsed from payload
    assert pub.created_at == epoch_to_dt(1700000000)

    arch = db_session.execute(select(Memo).where(Memo.uid == "mig-arch")).scalar_one()
    assert arch.archived_at is not None and arch.pinned is True and arch.owner_id == laura.id

    att = db_session.execute(select(Attachment).where(Attachment.uid == "mig-att")).scalar_one()
    assert att.memo_id == pub.id and att.owner_id == fabian.id and att.mime == "image/jpeg"
    assert att.storage_path.endswith("originals/a.jpg")  # flat basename of the reference

    orphan = db_session.execute(
        select(Attachment).where(Attachment.uid == "mig-orphan")
    ).scalar_one()
    assert orphan.memo_id is None  # kept, unlinked

    reactions = db_session.execute(select(Reaction)).scalars().all()
    assert len(reactions) == 1 and reactions[0].emoji == "👍"


def test_import_is_idempotent(db_session):
    data = _dataset()
    import_all(db_session, data)
    import_all(db_session, data)  # re-run

    assert len(db_session.execute(select(Memo)).scalars().all()) == 2  # no duplicates
    assert len(db_session.execute(select(User)).scalars().all()) == 2
    assert len(db_session.execute(select(Attachment)).scalars().all()) == 2
    assert len(db_session.execute(select(Reaction)).scalars().all()) == 1
