"""What size an inline picture actually is, and the box it is laid out in (MEMNG-274).

The owner reported "the images are tiny" the morning after 1.6.0 routed the digest's inline
pictures through the export profiles. The reproduction (a real render of the 2026-09-07 07:00 run)
found the encoded pictures LARGER than before — 1600 px on the long edge where the pre-1.6.0 code
inlined the 800 px thumb tier verbatim — and the `<img>` boxes unchanged. This suite is what keeps
that true: a regression that quietly re-pointed the mail at a smaller tier, or that transposed a
portrait picture's dimensions, would produce exactly the complaint that was reported.

Two properties, and they are deliberately checked against different oracles:

* the ORDERED dimensions on the card are the ones the ENCODED BYTES decode to. Pillow encoded
  them, so asking the encoder what it produced would be a conversation it has with itself; the
  bytes that leave for the SMTP server are re-opened here instead. Ordered, because a
  width/height transposition survives every unordered comparison and is invisible on a square
  fixture — which is why one source is landscape and one is portrait;
* the `height` attributes in the rendered HTML are CONCRETE numbers written out here, not
  arithmetic repeated from the template. A test that recomputed the ratio would agree with any
  ratio the template invented.
"""

import re
from datetime import UTC, datetime
from io import BytesIO

import pytest
from app.config import settings as app_settings
from app.services.digest import render
from app.services.digest.hosting import MediaLinks
from app.services.digest.select import Candidate, MediaRef
from PIL import Image

from tests.digest_support import jpeg_bytes, mail_profile, write_mobile, write_poster

# The two real sources, as the worker writes the mobile tier for a phone photo.
LANDSCAPE_SOURCE = (2048, 1536)
PORTRAIT_SOURCE = (1536, 2048)
# What the mail profile's 1600 px long edge makes of each: `min(long_edge, source long edge)` on
# the long side (Pillow's `thumbnail` never enlarges), the other side scaled.
LANDSCAPE_ENCODED = (1600, 1200)
PORTRAIT_ENCODED = (1200, 1600)
# The pre-1.6.0 reference: the thumb tier the old code inlined was 800 px on the long edge, so an
# inline picture must never be smaller than that again.
PRE_1_6_0_LONG_EDGE = 800


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    return tmp_path


def _write_thumb(data_root, uid: str, size: tuple[int, int]):
    """The `thumb` tier beside the `mobile` one, because production has both.

    Nothing in the digest reads it — that is the point. It exists so the mutation that re-points
    `MAIL_IMAGE_TIER` at it produces a real 800 px encode for the assertions to catch, rather than
    a missing file that would fail this suite for the wrong reason.
    """
    path = data_root / "assets" / "thumbs" / f"{uid}.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(jpeg_bytes(size))
    return path


def _source(data_root, uid: str, size: tuple[int, int]):
    write_mobile(data_root, uid, size=size, noise=True)
    thumb = (800, 600) if size[0] > size[1] else (600, 800)
    _write_thumb(data_root, uid, thumb)


def _candidate(uid: str, image_uids: list[str]) -> Candidate:
    return Candidate(
        memo_id=abs(hash(uid)) % 100000,
        uid=uid,
        content="",
        created_at=datetime(2026, 9, 7, 5, 0, tzinfo=UTC),
        images=[
            MediaRef(attachment_id=index, uid=image_uid, filename="p.jpg", mime="image/jpeg")
            for index, image_uid in enumerate(image_uids, start=1)
        ],
    )


async def _cards_and_inline(candidates):
    profile = mail_profile()
    files = await render.read_card_images(
        render.card_source_paths(candidates, MediaLinks(), 2), profile
    )
    return render.build_cards(
        candidates,
        files,
        MediaLinks(),
        max_videos_per_memo=2,
        link_images_full=False,
        blocked_hosts=frozenset(),
    )


def _img_boxes(html: str) -> list[tuple[int, int]]:
    """Every `<img>`'s `(width, height)` attribute pair, in document order."""
    boxes: list[tuple[int, int]] = []
    for tag in re.findall(r"<img[^>]*>", html):
        width = re.search(r'\swidth="(\d+)"', tag)
        height = re.search(r'\sheight="(\d+)"', tag)
        assert width is not None, f"an inline image with no width attribute: {tag}"
        assert height is not None, f"an inline image with no height attribute: {tag}"
        boxes.append((int(width.group(1)), int(height.group(1))))
    return boxes


def _render(cards) -> str:
    return render.render_html(
        render.group_by_local_day(cards, "Europe/Zurich"),
        subject="s",
        mail_title="t",
        date_range="r",
        memo_count=len(cards),
    )


async def test_an_inline_picture_is_the_size_its_own_bytes_decode_to(data_root):
    """The ordered pin, against the encoded bytes rather than against the encoder."""
    _source(data_root, "land-1", LANDSCAPE_SOURCE)
    _source(data_root, "port-1", PORTRAIT_SOURCE)
    candidates = [_candidate("m-land", ["land-1"]), _candidate("m-port", ["port-1"])]
    cards, inline = await _cards_and_inline(candidates)
    by_cid = {image.cid: image.data for image in inline}
    expected = {"m-land": LANDSCAPE_ENCODED, "m-port": PORTRAIT_ENCODED}
    assert [len(card.images) for card in cards] == [1, 1]
    for card in cards:
        image = card.images[0]
        with Image.open(BytesIO(by_cid[image.cid])) as decoded:
            assert (image.pixel_width, image.pixel_height) == decoded.size
        assert (image.pixel_width, image.pixel_height) == expected[card.uid]
        assert max(image.pixel_width or 0, image.pixel_height or 0) == 1600  # the profile's cap
        assert max(image.pixel_width or 0, image.pixel_height or 0) >= PRE_1_6_0_LONG_EDGE


async def test_a_source_smaller_than_the_profile_is_never_enlarged(data_root):
    """`min(profile.long_edge, source long edge)` is the rule, not `profile.long_edge`: the
    encoder's `thumbnail` never scales up, and asserting the cap alone would hide a tier that had
    quietly shrunk."""
    write_mobile(data_root, "small-1", size=(1000, 750), noise=True)
    cards, inline = await _cards_and_inline([_candidate("m-small", ["small-1"])])
    image = cards[0].images[0]
    with Image.open(BytesIO(inline[0].data)) as decoded:
        assert (image.pixel_width, image.pixel_height) == decoded.size
    assert (image.pixel_width, image.pixel_height) == (1000, 750)


async def test_a_single_picture_card_states_the_540_box_at_the_right_height(data_root):
    _source(data_root, "land-1", LANDSCAPE_SOURCE)
    _source(data_root, "port-1", PORTRAIT_SOURCE)
    landscape, _ = await _cards_and_inline([_candidate("m-land", ["land-1"])])
    portrait, _ = await _cards_and_inline([_candidate("m-port", ["port-1"])])
    # 540 x 1200/1600 = 405; 540 x 1600/1200 = 720. Written out, not recomputed.
    assert _img_boxes(_render(landscape)) == [(540, 405)]
    assert _img_boxes(_render(portrait)) == [(540, 720)]


async def test_a_grid_card_states_the_262_box_at_the_right_height(data_root):
    _source(data_root, "land-1", LANDSCAPE_SOURCE)
    _source(data_root, "land-2", LANDSCAPE_SOURCE)
    _source(data_root, "port-1", PORTRAIT_SOURCE)
    _source(data_root, "port-2", PORTRAIT_SOURCE)
    landscape, _ = await _cards_and_inline([_candidate("m-land", ["land-1", "land-2"])])
    portrait, _ = await _cards_and_inline([_candidate("m-port", ["port-1", "port-2"])])
    # 262 x 1200/1600 = 196.5 → 197 (half-up); 262 x 1600/1200 = 349.33 → 349.
    assert _img_boxes(_render(landscape)) == [(262, 197), (262, 197)]
    assert _img_boxes(_render(portrait)) == [(262, 349), (262, 349)]


async def test_a_hosted_videos_poster_anchor_keeps_its_box_when_the_poster_is_absent(data_root):
    """The template defect the reproduction surfaced: with the poster bytes missing, the anchor's
    `display:inline-block` collapsed to the alt text (57x101 in the headless render) and the play
    badge landed on top of it. A block anchor bounded by the layout width keeps the box whatever
    the client does about the image."""
    write_poster(data_root, "vid-1")
    links = MediaLinks(by_attachment={9: "https://dropbox.example/v.mp4?raw=1"})
    candidate = _candidate("m-vid", [])
    candidate.videos = [
        MediaRef(attachment_id=9, uid="vid-1", filename="clip.mp4", mime="video/mp4")
    ]
    profile = mail_profile()
    files = await render.read_card_images(
        render.card_source_paths([candidate], links, 2), profile
    )
    cards, _ = render.build_cards(
        [candidate],
        files,
        links,
        max_videos_per_memo=2,
        link_images_full=False,
        blocked_hosts=frozenset(),
    )
    html = _render(cards)
    anchor = re.search(r'<a href="https://dropbox\.example/v\.mp4\?raw=1"[^>]*>', html)
    assert anchor is not None
    style = anchor.group(0)
    assert "display:block" in style
    assert "display:inline-block" not in style
    assert f"max-width:{render.SINGLE_IMAGE_WIDTH}px" in style
