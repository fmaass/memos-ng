"""The guest door's file-count caps (MEMNG-1027): a pass takes at most
`INGEST_PASS_FILES_PER_DAY` new files in any 24 hours, and one device at most
`GUEST_FILES_PER_DEVICE` files in all. Each answers 429 AT the limit and admits one below it."""

from app.config import settings

from tests.ingest_support import (
    JPEG,
    bearer,
    door,  # noqa: F401
    home_iface,  # noqa: F401
    ingest_env,  # noqa: F401
    redeem,
    seeded_pass,
)
from tests.test_attachments_taken import _sql


async def _create(door, token: str, n: int):  # noqa: F811
    return await door.post(
        "/api/guest/uploads",
        json={"filename": f"IMG_{n}.jpg", "mime": "image/jpeg", "size": len(JPEG)},
        headers=bearer(token),
    )


async def test_the_pass_files_per_day_cap_admits_one_below_and_refuses_at_the_limit(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setattr(settings, "INGEST_PASS_FILES_PER_DAY", 3)
    monkeypatch.setattr(settings, "GUEST_FILES_PER_DEVICE", 100)
    _, secret = await seeded_pass(home_iface)
    anna = (await redeem(door, secret, "Anna"))["device_token"]
    ben = (await redeem(door, secret, "Ben"))["device_token"]
    assert (await _create(door, anna, 1)).status_code == 201
    assert (await _create(door, ben, 2)).status_code == 201
    # Two of three taken: the third (one below the limit counted before it) is admitted.
    assert (await _create(door, anna, 3)).status_code == 201
    refused = await _create(door, ben, 4)
    assert refused.status_code == 429, refused.text
    assert refused.headers["Retry-After"] == "3600"
    assert _sql("SELECT count(*) FROM guest_uploads") == [(3,)]
    # The window is a day: files started more than 24 hours ago no longer count.
    _sql("UPDATE pass_events SET at = at - interval '25 hours' WHERE kind = 'upload_started'")
    assert (await _create(door, ben, 5)).status_code == 201


async def test_the_device_file_cap_admits_one_below_and_refuses_at_the_limit(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setattr(settings, "GUEST_FILES_PER_DEVICE", 3)
    _, secret = await seeded_pass(home_iface)
    anna = (await redeem(door, secret, "Anna"))["device_token"]
    ben = (await redeem(door, secret, "Ben"))["device_token"]
    # Filed files count as well as open sessions.
    _sql("UPDATE pass_devices SET uploads_count = 1 WHERE display_name = 'Anna'")
    assert (await _create(door, anna, 1)).status_code == 201
    assert (await _create(door, anna, 2)).status_code == 201
    refused = await _create(door, anna, 3)
    assert refused.status_code == 429, refused.text
    assert refused.headers["Retry-After"] == "3600"
    # The cap is the device's own: another device on the same pass is unaffected.
    assert (await _create(door, ben, 4)).status_code == 201
