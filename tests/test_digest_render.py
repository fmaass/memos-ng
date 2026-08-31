"""Rendering: Markdown → sanitised HTML → the mail body (MEMNG-234).

The load-bearing case is AC 4 — **the mail contains no link back to memos-ng**. That AC is
otherwise true by accident: the template's only `src` values are `cid:` and its only `href` values
are Dropbox, and the production corpus was measured on 2026-08-30 as **0 of 245 memos containing
any http(s) URL at all**. So the test carries its own fixture memo, which DOES contain one — an
assertion over today's corpus would pass with the stripping deleted.
"""

from datetime import UTC, datetime

import pytest
from app.config import settings as app_settings
from app.services.digest import render
from app.services.digest.hosting import MediaLinks
from app.services.digest.select import Candidate, MediaRef

from tests.digest_support import mail_profile, write_mobile, write_poster

MEMOS_HOST = "memos.discomarder.live"


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    return tmp_path


def _candidate(**overrides) -> Candidate:
    return Candidate(
        memo_id=overrides.pop("memo_id", 1),
        uid=overrides.pop("uid", "memo-1"),
        content=overrides.pop("content", ""),
        created_at=overrides.pop("created_at", datetime(2026, 7, 1, 9, 0, tzinfo=UTC)),
        images=overrides.pop("images", []),
        videos=overrides.pop("videos", []),
        incomplete=overrides.pop("incomplete", False),
    )


def _image(uid: str, attachment_id: int = 1, filename: str = "p.jpg") -> MediaRef:
    return MediaRef(attachment_id=attachment_id, uid=uid, filename=filename, mime="image/jpeg")


def _video(uid: str, attachment_id: int = 2, filename: str = "clip.mp4") -> MediaRef:
    return MediaRef(attachment_id=attachment_id, uid=uid, filename=filename, mime="video/mp4")


async def _encoded(candidates, links: MediaLinks, profile=None):
    """Every inline source of these candidates, read and encoded by the REAL pipeline step.

    Not a hand-built dict of raw bytes: `build_cards` is fed what `read_card_images` produces, so
    a test that assembled the mapping itself would stop exercising the encoder the moment one was
    introduced — which is exactly what happened here.
    """
    return await render.read_card_images(
        render.card_source_paths(candidates, links, 2), profile or mail_profile()
    )


# --- the sanitiser, and AC 4 --------------------------------------------------------------------


def test_markdown_renders_and_is_sanitised():
    html = render.sanitize_markdown("**bold** and <script>alert(1)</script>")
    assert "<strong>bold</strong>" in html
    assert "<script>" not in html


def test_the_hosts_this_deployment_is_known_by_come_from_configuration():
    hosts = render.memos_ng_hosts()
    assert MEMOS_HOST in hosts  # NATIVE_CALLBACK_URL
    assert "memos-ng.discomarder.live" in hosts  # OIDC_REDIRECT_URI


@pytest.mark.parametrize(
    "markdown_source",
    [
        f"see [the memo](https://{MEMOS_HOST}/m/abc)",
        f"see [the memo](http://{MEMOS_HOST}/m/abc)",
        f"see [the memo](//{MEMOS_HOST}/m/abc)",
    ],
)
def test_an_absolute_memos_ng_link_is_stripped_from_the_rendered_content(markdown_source):
    """The fixture memo CONTAINS the link — the corpus does not, which is why it must."""
    html = render.sanitize_markdown(markdown_source, render.memos_ng_hosts())
    assert MEMOS_HOST not in html
    assert "the memo" in html  # the words stay; only the navigation is removed
    assert "href" not in html


def test_a_memos_ng_image_is_removed_rather_than_left_srcless():
    html = render.sanitize_markdown(
        f"![shot](https://{MEMOS_HOST}/api/attachments/x/thumb)", render.memos_ng_hosts()
    )
    assert "<img" not in html
    assert MEMOS_HOST not in html


def test_a_foreign_link_survives():
    """The rule is about THIS host, not about links: a link to a recipe still links to it."""
    html = render.sanitize_markdown(
        "see [the recipe](https://example.com/x)", render.memos_ng_hosts()
    )
    assert 'href="https://example.com/x"' in html


def test_a_relative_link_is_left_alone():
    """A relative path has no host and cannot navigate anywhere from an inbox."""
    html = render.sanitize_markdown("[x](/m/abc)", render.memos_ng_hosts())
    assert 'href="/m/abc"' in html


def test_video_markdown_refs_are_stripped_from_the_content():
    content = "look at this\n\n![clip](/file/attachments/vid-1/clip.mp4)\n\ndone"
    stripped = render.strip_video_refs(content, ["vid-1"])
    assert "vid-1" not in stripped
    assert "look at this" in stripped and "done" in stripped


# --- day grouping and the subject  --------------------------------------------------------------


def test_days_group_by_LOCAL_date_newest_first():
    """22:30 UTC on 1 July is already 2 July in Zurich — grouping on the UTC date would file the
    memo under the wrong day in the mail the household reads."""
    late = render.Card(
        uid="a", memo_id=1, content_html="", created_at=datetime(2026, 7, 1, 22, 30, tzinfo=UTC)
    )
    early = render.Card(
        uid="b", memo_id=2, content_html="", created_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    )
    groups = render.group_by_local_day([late, early], "Europe/Zurich")
    assert [g.iso for g in groups] == ["2026-07-02", "2026-07-01"]
    assert groups[0].cards[0].uid == "a"


def test_the_subject_date_range_matches_the_satellite():
    one = render.DayGroup(date_label="", iso="2026-07-01")
    same_month = render.DayGroup(date_label="", iso="2026-07-04")
    other_month = render.DayGroup(date_label="", iso="2026-08-02")
    assert render.subject_date_range([one]) == "July 1, 2026"
    assert render.subject_date_range([same_month, one]) == "July 1 – 4, 2026"
    assert render.subject_date_range([other_month, one]) == "July 1 – August 2, 2026"


def test_the_subject_names_the_part_only_when_there_is_more_than_one():
    title = "The Daily Dami Memos Mail"
    assert render.subject_for(title, "July 1, 2026", 1, 1) == f"{title} — July 1, 2026"
    assert (
        render.subject_for(title, "July 1, 2026", 1, 2)
        == f"{title} — July 1, 2026 (Part 1 of 2)"
    )
    assert (
        render.subject_for(title, "July 1, 2026", 2, 2)
        == f"{title} — July 1, 2026 (Part 2 of 2)"
    )


# --- cards --------------------------------------------------------------------------------------


async def test_a_photo_card_inlines_its_mobile_tier_as_a_cid(data_root):
    tier = write_mobile(data_root, "img-1", size=(800, 600))
    candidate = _candidate(content="hello", images=[_image("img-1")])
    files = await _encoded([candidate], MediaLinks())
    cards, inline = render.build_cards(
        [candidate],
        files,
        MediaLinks(),
        max_videos_per_memo=2,
        link_images_full=False,
        blocked_hosts=frozenset(),
    )
    assert len(cards) == 1 and len(cards[0].images) == 1
    assert inline[0].cid == cards[0].images[0].cid
    assert inline[0].content_type == "image/jpeg"
    # The bytes are the RE-ENCODE, not a copy of the tier: same picture, the mail profile's
    # encoding. The tier is written at q95 and the profile asks for q80, so the two cannot match.
    assert inline[0].data != tier.read_bytes()
    assert inline[0].data.startswith(b"\xff\xd8")  # still a JPEG
    # 800x600 is under the profile's 1600 long edge, so the re-encode does not resize it.
    assert (cards[0].images[0].pixel_width, cards[0].images[0].pixel_height) == (800, 600)


async def test_a_hosted_video_gets_a_poster_cid_and_a_link(data_root):
    write_poster(data_root, "vid-1")
    links = MediaLinks(by_attachment={2: "https://dropbox.example/v.mp4?raw=1"})
    candidate = _candidate(videos=[_video("vid-1")])
    files = await _encoded([candidate], links)
    cards, inline = render.build_cards(
        [candidate],
        files,
        links,
        max_videos_per_memo=2,
        link_images_full=False,
        blocked_hosts=frozenset(),
    )
    video = cards[0].videos[0]
    assert video.status == render.HOSTED
    assert video.url == "https://dropbox.example/v.mp4?raw=1"
    assert video.poster_cid is not None
    assert [image.cid for image in inline] == [video.poster_cid]


def test_an_unhosted_video_carries_the_not_hosted_chip_and_no_poster(data_root):
    """AC 2 as amended: hosting off or its token refresh failed → a "video not hosted" note, never
    an attached mp4."""
    write_poster(data_root, "vid-1")
    candidate = _candidate(videos=[_video("vid-1")])
    cards, inline = render.build_cards(
        [candidate],
        {},
        MediaLinks(),
        max_videos_per_memo=2,
        link_images_full=False,
        blocked_hosts=frozenset(),
    )
    assert cards[0].videos[0].status == render.NOT_HOSTED
    assert cards[0].videos[0].poster_cid is None
    assert inline == []


def test_a_video_whose_mp4_is_not_transcoded_yet_reads_as_processing():
    candidate = _candidate(videos=[_video("vid-1")])
    links = MediaLinks(processing={2})
    cards, _ = render.build_cards(
        [candidate],
        {},
        links,
        max_videos_per_memo=2,
        link_images_full=False,
        blocked_hosts=frozenset(),
    )
    assert cards[0].videos[0].status == render.PROCESSING


async def test_max_videos_per_memo_bounds_the_card(data_root):
    for uid in ("v1", "v2", "v3"):
        write_poster(data_root, uid)
    candidate = _candidate(
        videos=[_video("v1", 1), _video("v2", 2), _video("v3", 3)]
    )
    links = MediaLinks(by_attachment={1: "u1", 2: "u2", 3: "u3"})
    files = await _encoded([candidate], links)
    cards, _ = render.build_cards(
        [candidate],
        files,
        links,
        max_videos_per_memo=2,
        link_images_full=False,
        blocked_hosts=frozenset(),
    )
    assert len(cards[0].videos) == 2


async def test_link_images_full_turns_the_inline_image_into_a_link(data_root):
    write_mobile(data_root, "img-1")
    links = MediaLinks(by_attachment={1: "https://dropbox.example/full.jpg?raw=1"})
    candidate = _candidate(images=[_image("img-1")])
    files = await _encoded([candidate], links)
    cards, _ = render.build_cards(
        [candidate],
        files,
        links,
        max_videos_per_memo=2,
        link_images_full=True,
        blocked_hosts=frozenset(),
    )
    assert cards[0].images[0].link_url == "https://dropbox.example/full.jpg?raw=1"


async def test_the_incomplete_flag_reaches_the_card_and_the_template(data_root):
    write_mobile(data_root, "img-1")
    candidate = _candidate(images=[_image("img-1")], incomplete=True)
    files = await _encoded([candidate], MediaLinks())
    cards, _ = render.build_cards(
        [candidate],
        files,
        MediaLinks(),
        max_videos_per_memo=2,
        link_images_full=False,
        blocked_hosts=frozenset(),
    )
    assert cards[0].is_incomplete is True
    html = render.render_html(
        render.group_by_local_day(cards, "Europe/Zurich"),
        subject="s",
        mail_title="t",
        date_range="r",
        memo_count=1,
    )
    assert "weren't ready at send time" in html


# --- the rendered message  ----------------------------------------------------------------------


async def _one_card_groups(data_root, content: str = "hello") -> list[render.DayGroup]:
    write_mobile(data_root, "img-1")
    candidate = _candidate(content=content, images=[_image("img-1")])
    files = await _encoded([candidate], MediaLinks())
    cards, _ = render.build_cards(
        [candidate],
        files,
        MediaLinks(),
        max_videos_per_memo=2,
        link_images_full=False,
        blocked_hosts=render.memos_ng_hosts(),
    )
    return render.group_by_local_day(cards, "Europe/Zurich")


async def test_the_html_uses_the_configured_mail_title(data_root):
    html = render.render_html(
        await _one_card_groups(data_root),
        subject="s",
        mail_title="Household Post",
        date_range="July 1, 2026",
        memo_count=1,
    )
    assert "Household Post" in html


async def test_the_part_banner_appears_only_in_a_multi_part_mail(data_root):
    groups = await _one_card_groups(data_root)
    single = render.render_html(
        groups, subject="s", mail_title="t", date_range="r", memo_count=1
    )
    multi = render.render_html(
        groups,
        subject="s",
        mail_title="t",
        date_range="r",
        memo_count=1,
        part_index=2,
        part_total=3,
    )
    assert "Part 2 of 3" in multi
    assert "Part 1 of 1" not in single


async def test_a_memos_ng_link_in_a_memo_never_reaches_the_rendered_html(data_root):
    """The end-to-end shape of AC 4, through the real template."""
    groups = await _one_card_groups(data_root, content=f"[home](https://{MEMOS_HOST}/m/abc)")
    html = render.render_html(
        groups, subject="s", mail_title="t", date_range="r", memo_count=1
    )
    assert MEMOS_HOST not in html


async def test_the_plain_alternative_carries_the_text_and_the_media_summary(data_root):
    groups = await _one_card_groups(data_root, content="hello world")
    plain = render.render_plain(groups, subject="Subject", memo_count=1)
    assert plain.startswith("Subject\n=======")
    assert "hello world" in plain
    assert "[1 image(s)]" in plain
    assert "1 new memo(s) across 1 day(s)" in plain
    assert "<p>" not in plain  # the HTML is reduced to text, not pasted in


def test_the_empty_body_says_so_in_both_alternatives():
    html, plain = render.render_empty("Household Post")
    assert "No memos selected." in html
    assert "Household Post" in html
    assert render.EMPTY_BODY_NOTE in plain


def test_the_template_is_compiled_once_and_never_re_read():
    """Every render used to construct a fresh Jinja `Environment` and load the template off the
    filesystem — on the deployed host that is a network bind mount, inside the event loop, once per
    PART of every mail. Caching the compiled template is what makes a render pure CPU, which is
    also why it can stay synchronous: there is no filesystem call left in it to offload.

    Asserted by counting loader reads rather than by reading the source, because the manifest of a
    cache is that the second read does not happen.
    """
    from app.services.digest import render as render_module
    from jinja2 import FileSystemLoader

    render_module._template.cache_clear()
    reads: list[str] = []
    real_get_source = FileSystemLoader.get_source

    def _counting_get_source(self, environment, template):
        reads.append(template)
        return real_get_source(self, environment, template)

    FileSystemLoader.get_source = _counting_get_source
    try:
        first = render_module.render_html(
            [], subject="s", mail_title="t", date_range="", memo_count=0
        )
        second = render_module.render_html(
            [], subject="s", mail_title="t", date_range="", memo_count=0
        )
    finally:
        FileSystemLoader.get_source = real_get_source
        render_module._template.cache_clear()
    assert first == second  # control: both calls really rendered the same template
    assert reads == [render_module.TEMPLATE_NAME]
