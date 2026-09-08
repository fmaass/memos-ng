"""Everything `openapi.json` exports is outgoing text, and so is what it generates.

The document is served to clients and vendored verbatim into the native client, and the SPA's
`frontend/src/api/schema.d.ts` is generated from that same file — so a description written for a
maintainer travels straight out of the estate. The mechanism is easy to trip over: a Pydantic
model's docstring becomes its component `description` and a route function's docstring becomes the
operation `description`. Rationale belongs in ordinary `#` comments beside the code; only text
written FOR a client belongs in a docstring.

The invariant: an exported description may name only what the client can see — a route, a field, a
header, a status, an error code — never a source path, a private helper, a database object, or a
record where a decision was written down.

Two guards, and their scope is deliberate, because a blocklist that grows a word per round is not a
gate:

* `test_*_mechanical_alphabet` — the FORBIDDEN pattern, for the forms a regex can actually decide:
  tracker keys, internal hostnames, estate and repository paths, ADR/design/decision pointers, `§`,
  private identifiers, and a parenthesised internal label such as an `(F2)` finding id. It also
  re-reads each artifact with wrapped lines collapsed, so a two-word form split across a generated
  comment's line break cannot hide.
* `test_*_published_vocabulary` — the ALLOWLIST, for what a regex cannot decide. A regex cannot
  know whether `users` is a household concept or a table; the document can. Every backtick-quoted
  token in an exported description must be something openapi.json itself publishes — a property, a
  path, a parameter, a header, a literal, a schema or scheme name — or one of a short list of
  reviewed allowances below, each with its reason.

OUT OF SCOPE, by design: plain prose that is neither code-formatted nor in the mechanical alphabet.
No test decides whether an English sentence is written for a client or for a maintainer; the
outgoing-text review of every contract diff is the guard for that.
"""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OPENAPI = REPO_ROOT / "openapi.json"
SCHEMA_D_TS = REPO_ROOT / "frontend" / "src" / "api" / "schema.d.ts"

TEXT_KEYS = ("description", "summary", "title")

# The mechanical alphabet: tracker keys, the estate's hostnames and filesystem layout, this
# repository's own paths, the records where decisions are written down, private identifiers, and a
# bracketed internal label like "(F2)". Deliberately NOT here: "session", this API's own vocabulary.
FORBIDDEN = re.compile(
    r"MEMNG-|ARCH-|INFRA-|\bADR\b|Decision [0-9]|[Dd]esign [0-9]+|[Oo]ption [0-9]"
    r"|[Pp]hase [0-9]|round [0-9]|§"
    r"|BookStack|windshift|orchestrat|handover|estate|reviewer|ticket"
    r"|tests/|scripts/|app/|\bsee _|\b_[a-z]|\([A-Z][0-9]+\)"
    r"|discomarder|/home/fabian|jupiter|saturn|merkur"
)

# Reviewed exceptions to the allowlist: code-formatted tokens this API does not publish but a
# client still needs to read literally. An entry here is a decision, not a shortcut.
ALLOWED_TOKENS = {
    "/2/users/get_current_account": "Dropbox's own public endpoint, quoted so an admin can find it",
    "DROPBOX_*": "the configuration family the admin-facing `configured` result reports on",
    "memos_pat_…": "the literal prefix of the token handed to the client",
    "data:image/…": "the data-URL form a member avatar takes",
    "0001_<date>_<uid>.jpg": "a file-name shape inside the export archive",
    "<memo-uid>.md": "a file-name shape inside the export archive",
    "media/<memo-uid>/": "a directory shape inside the export archive",
    "print-4096": "the permanent default rendition profile, a value `profile` takes",
    "4:2:2": "chroma subsampling notation, a value the mail profile takes",
    "token_not_renewable": "an error `detail` value the client switches on, modelled as no literal",
}

BACKTICKED = re.compile(r"`([^`]+)`")


def _texts(node: object, path: str = "#"):
    """Every exported description/summary/title, with the JSON pointer that reaches it."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in TEXT_KEYS and isinstance(value, str):
                yield f"{path}/{key}", value
            else:
                yield from _texts(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _texts(value, f"{path}/{index}")


def _normalised_path(template: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", template)


def published_vocabulary(document: dict) -> set[str]:
    """Everything the document itself publishes, as the strings a description may name.

    Derived, never hand-listed: the moment a field is renamed the vocabulary follows it, and a
    description naming the old name fails — which is the point.
    """
    vocab: set[str] = {"null", "true", "false"}

    def literals(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    vocab.update(value)
                elif key in ("const", "default", "example") and isinstance(
                    value, str | int | float | bool
                ):
                    vocab.add(str(value))
                elif key in ("enum", "examples") and isinstance(value, list):
                    vocab.update(str(v) for v in value if isinstance(v, str | int | float | bool))
                literals(value)
        elif isinstance(node, list):
            for value in node:
                literals(value)

    literals(document)

    components = document.get("components", {})
    vocab.update(components.get("schemas", {}))
    vocab.update(components.get("securitySchemes", {}))
    for scheme in components.get("securitySchemes", {}).values():
        # An `http` scheme speaks through Authorization and names its own scheme word; an `apiKey`
        # scheme names the header or cookie it rides in. Both are things a client sends.
        if scheme.get("type") == "http":
            word = scheme.get("scheme", "")
            vocab.update({"Authorization", word, word.title()})
        if scheme.get("type") == "apiKey" and "name" in scheme:
            vocab.add(scheme["name"])
    vocab.update(tag["name"] for tag in document.get("tags", []) if "name" in tag)

    for template, item in document.get("paths", {}).items():
        vocab.add(template)
        vocab.add(_normalised_path(template))
        # Whole routes and their tails only — deliberately NOT the bare segments. `pats` as a lone
        # word is the table as readily as the route, and a segment that a client really does name
        # on its own reaches this set anyway, as a parameter, a property or a published literal.
        for method, operation in item.items():
            if not isinstance(operation, dict):
                continue
            vocab.update({method, method.upper()})
            for parameter in operation.get("parameters", []) or []:
                if "name" in parameter:
                    vocab.add(parameter["name"])
            for status, response in (operation.get("responses") or {}).items():
                vocab.add(str(status))
                vocab.update(response.get("headers") or {})
                vocab.update(response.get("content") or {})
            vocab.update((operation.get("requestBody") or {}).get("content") or {})
            vocab.update(operation.get("tags", []) or [])
    return vocab


def _path_suffixes(document: dict) -> set[str]:
    """`native/logout` and `/api/pats/{id}` name real routes by their tail; both are published."""
    suffixes: set[str] = set()
    for template in document.get("paths", {}):
        segments = _normalised_path(template).strip("/").split("/")
        for start in range(len(segments)):
            tail = "/".join(segments[start:])
            suffixes.update({tail, f"/{tail}"})
    return suffixes


def unpublished_tokens(text: str, vocabulary: set[str], suffixes: set[str]) -> list[str]:
    """The backticked tokens in `text` that the document does not publish."""
    unknown = []
    for token in BACKTICKED.findall(text):
        if _token_is_published(token, vocabulary, suffixes):
            continue
        unknown.append(token)
    return unknown


def _token_is_published(token: str, vocabulary: set[str], suffixes: set[str]) -> bool:
    token = token.strip()
    if token in vocabulary or token in ALLOWED_TOKENS:
        return True
    # "GET /api/members", "POST /run": a method and a route, or a route named by its tail.
    head, _, tail = token.partition(" ")
    route = tail if head in vocabulary and tail else token
    if _normalised_path(route) in suffixes or _normalised_path(route) in vocabulary:
        return True
    # "?owner=alice&owner=bob", "format=zip": published parameters carrying illustrative values.
    query = route.lstrip("?")
    if "=" in query and all(
        part.split("=", 1)[0] in vocabulary for part in query.split("&") if part
    ):
        return True
    # "detail.code": a path into a published body, every segment of it published.
    if "." in token and all(part in vocabulary for part in token.split(".") if part):
        return True
    # "status: null", "Authorization: Grant …": a published field or header, with its value.
    name, colon, _value = token.partition(":")
    if colon and name.strip() in vocabulary:
        return True
    return False


def _offending_lines(path: Path) -> list[str]:
    return [
        f"{path.name}:{number}: {line.strip()[:160]}"
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if FORBIDDEN.search(line)
    ]


def _offending_unwrapped(path: Path) -> list[str]:
    """The same alphabet, read across line breaks.

    A generated comment wraps the description at spaces, so a two-word form — "design 3285", "see
    `_helper`" — can straddle two lines and pass a line-at-a-time scan. Runs of whitespace and the
    comment's own `*` gutter collapse to ONE space, which rejoins those without gluing a path back
    together across a wrap that never happens mid-token.
    """
    flat = re.sub(r"[\s*]+", " ", path.read_text())
    return [
        f"{path.name}: …{flat[max(0, m.start() - 60) : m.end() + 60]}…"
        for m in FORBIDDEN.finditer(flat)
    ]


def test_the_served_schema_carries_no_internal_reference_in_the_mechanical_alphabet():
    assert '"paths"' in OPENAPI.read_text()  # positive control: the document was really read
    offenders = _offending_lines(OPENAPI)
    assert offenders == [], (
        f"{len(offenders)} exported description(s) name something internal — rewrite the model or "
        f"route docstring as client-facing prose and move the rationale into a `#` comment, then "
        f"regenerate: {offenders[:3]}"
    )


def test_the_generated_frontend_types_carry_no_internal_reference_in_the_mechanical_alphabet():
    assert "export interface paths" in SCHEMA_D_TS.read_text()  # positive control
    offenders = _offending_lines(SCHEMA_D_TS)
    assert offenders == [], (
        f"{len(offenders)} generated type comment(s) name something internal — the source is a "
        f"docstring in app/, not this file; fix it there and regenerate both artifacts: "
        f"{offenders[:3]}"
    )


def test_neither_artifact_hides_an_internal_reference_across_a_line_break():
    for path, control in ((OPENAPI, '"paths"'), (SCHEMA_D_TS, "export interface paths")):
        assert control in path.read_text()  # positive control
        offenders = _offending_unwrapped(path)
        assert offenders == [], (
            f"{len(offenders)} internal reference(s) in {path.name} spanning a wrapped line: "
            f"{offenders[:3]}"
        )


def test_every_code_formatted_token_belongs_to_the_published_vocabulary():
    document = json.loads(OPENAPI.read_text())
    vocabulary = published_vocabulary(document)
    suffixes = _path_suffixes(document)
    assert "upload_uid" in vocabulary  # positive control: a real property reached the vocabulary
    assert len(vocabulary) > 100  # positive control: the derivation ran on the whole document

    offenders = [
        f"`{token}` in {pointer}"
        for pointer, text in _texts(document)
        for token in unpublished_tokens(text, vocabulary, suffixes)
    ]
    assert offenders == [], (
        f"{len(offenders)} code-formatted token(s) name something this API does not publish — say "
        f"it in words, name the published field instead, or add a reviewed entry to ALLOWED_TOKENS "
        f"with its reason: {offenders[:5]}"
    )


def test_the_vocabulary_guard_rejects_a_token_the_document_does_not_publish():
    """The guard, driven directly on strings: no regeneration, no dependence on today's document."""
    document = json.loads(OPENAPI.read_text())
    vocabulary = published_vocabulary(document)
    suffixes = _path_suffixes(document)

    assert unpublished_tokens("takes an `upload_uid`", vocabulary, suffixes) == []
    assert unpublished_tokens("`GET /api/members` lists them", vocabulary, suffixes) == []
    assert unpublished_tokens("a `not_a_published_field` here", vocabulary, suffixes) == [
        "not_a_published_field"
    ]
    assert unpublished_tokens("mirrors `_viewable`", vocabulary, suffixes) == ["_viewable"]


def test_the_mechanical_alphabet_rejects_a_bracketed_internal_label():
    """Also driven directly: an (F2)-shaped finding id, an ADR and a design page are all refused."""
    assert FORBIDDEN.search("derived tiers only (Z9): the rest is unchanged")
    assert FORBIDDEN.search("see ADR web/print renditions")
    assert FORBIDDEN.search("behind one Save bar (design 3285 §6)")
    assert FORBIDDEN.search("locks the same row as `_refuse_leaving_nobody`")
    assert not FORBIDDEN.search("the upload session and the browser session are both fine")
