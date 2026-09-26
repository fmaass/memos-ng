"""Hold a served OpenAPI document to a client's snapshot of it — pure, so it can be tested on
hand-built pairs as well as on the real documents (`tests/test_ios_client_contract.py`).

The walk follows the SNAPSHOT: every path, operation, parameter, request body, response and
schema property the snapshot names must still be served with the same `type`. Whatever the
snapshot lacks is ignored — additions are allowed — except a new requirement the client never
knew to meet: a new required parameter, request body or request property on an operation the
snapshot has.

Enums and `required` are judged by which side of the wire the value travels:

* REQUEST (parameters, request bodies) — the server must still accept everything the client may
  send: the snapshot's enum members must be a subset of the served ones (growth passes), and the
  served `required` a subset of the snapshot's (loosening passes, tightening fails).
* RESPONSE — the client must still be able to decode everything the server may return: the served
  enum members must be a subset of the snapshot's (a closed-enum decoder breaks on a new member),
  and the snapshot's `required` a subset of the served one (a property the client relies on may
  not become optional; a newly guaranteed one is harmless). The same holds for `anyOf`/`oneOf`
  members.
"""

from dataclasses import dataclass, field
from typing import Any

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
REQUEST = "request"
RESPONSE = "response"

Json = dict[str, Any]


@dataclass
class Comparison:
    failures: list[str] = field(default_factory=list)
    paths: int = 0
    operations: int = 0
    parameters: int = 0
    properties: int = 0


def compare(snapshot: Json, served: Json) -> Comparison:
    result = Comparison()
    walker = _Walker(snapshot, served, result)
    served_paths = served.get("paths", {})
    for path, snap_item in snapshot.get("paths", {}).items():
        loc = f"paths.{path}"
        if path not in served_paths:
            result.failures.append(f"{loc}: path removed")
            continue
        result.paths += 1
        served_item = served_paths[path]
        for method in HTTP_METHODS:
            if method not in snap_item:
                continue
            if method not in served_item:
                result.failures.append(f"{loc}.{method}: operation removed")
                continue
            result.operations += 1
            walker.operation(
                f"{loc}.{method}", snap_item, snap_item[method], served_item, served_item[method]
            )
    return result


def _types(node: Json) -> list[str] | None:
    value = node.get("type")
    if value is None:
        return None
    return sorted([value] if isinstance(value, str) else value)


def _members(node: Json) -> set[Any] | None:
    """The values a schema admits by enumeration; None when it admits any value of its type."""
    if "enum" in node:
        return {json_key(v) for v in node["enum"]}
    if "const" in node:
        return {json_key(node["const"])}
    return None


def json_key(value: Any) -> Any:
    return value if isinstance(value, str | int | float | bool | type(None)) else repr(value)


def _conjoin(a: Json, b: Json) -> Json:
    """Both constraints at once — what `allOf: [a, b]` or a `$ref` with siblings means. Plain
    annotations (title, description, default) are taken from `b`."""
    out = {**a, **b}
    if "properties" in a and "properties" in b:
        properties = dict(a["properties"])
        for name, schema in b["properties"].items():
            properties[name] = (
                {"allOf": [properties[name], schema]} if name in properties else schema
            )
        out["properties"] = properties
    if "required" in a or "required" in b:
        out["required"] = list(dict.fromkeys([*a.get("required", []), *b.get("required", [])]))
    a_types, b_types = _types(a), _types(b)
    if a_types is not None and b_types is not None:
        out["type"] = sorted(set(a_types) & set(b_types))
    a_members, b_members = _members(a), _members(b)
    if a_members is not None or b_members is not None:
        if a_members is None:
            members = b_members or set()
        elif b_members is None:
            members = a_members
        else:
            members = a_members & b_members
        out.pop("const", None)
        out["enum"] = sorted(members, key=repr)
    for keyword in ("items", "additionalProperties"):
        if isinstance(a.get(keyword), dict) and isinstance(b.get(keyword), dict):
            out[keyword] = {"allOf": [a[keyword], b[keyword]]}
    for keyword in ("anyOf", "oneOf"):
        if keyword in a and keyword in b:
            raise ValueError(f"conjoining two {keyword} lists is not supported: extend the walker")
    return out


class _Walker:
    def __init__(self, snapshot: Json, served: Json, result: Comparison):
        self.snapshot = snapshot
        self.served = served
        self.result = result

    @staticmethod
    def _lookup(doc: Json, ref: str) -> Json:
        target: Any = doc
        for part in ref.removeprefix("#/").split("/"):
            target = target[part]  # a dangling reference raises: it must never compare clean
        return target

    def _resolve(self, doc: Json, node: Json) -> tuple[Json, str | None]:
        """A parameter, request body or response object behind its `$ref`, siblings winning."""
        name = None
        seen: set[str] = set()
        while "$ref" in node and node["$ref"] not in seen:
            ref = node["$ref"]
            seen.add(ref)
            siblings = {k: v for k, v in node.items() if k != "$ref"}
            node, name = {**self._lookup(doc, ref), **siblings}, ref.rsplit("/", 1)[-1]
        return node, name

    def _normalise(
        self, doc: Json, node: Json, active: frozenset[str] = frozenset()
    ) -> tuple[Json, str | None]:
        """One schema node as a single flat constraint, before any rule looks at it.

        OAS 3.1 lets keywords sit beside `$ref`, and `allOf` spreads one object over several
        members: read literally, either hides a constraint from the rules. So a `$ref` is replaced
        by its target CONJOINED with its siblings (JSON Schema 2020-12 semantics — for an enum
        beside a `$ref` that is the sibling's values whenever they are a subset of the target's),
        and every `allOf` member is conjoined into the node. `active` stops a `$ref`/`allOf` chain
        that leads back to itself; recursion through properties is the caller's cycle guard.
        """
        if "$ref" in node:
            ref = node["$ref"]
            name = ref.rsplit("/", 1)[-1]
            siblings = {k: v for k, v in node.items() if k != "$ref"}
            if ref in active:
                return siblings, name
            target, inner = self._normalise(doc, self._lookup(doc, ref), active | {ref})
            if siblings:
                target = _conjoin(target, self._normalise(doc, siblings, active)[0])
            return target, inner or name
        if "allOf" in node:
            flat = {k: v for k, v in node.items() if k != "allOf"}
            for member in node["allOf"]:
                flat = _conjoin(flat, self._normalise(doc, member, active)[0])
            return flat, None
        return node, None

    def _alternatives(self, doc: Json, node: Json) -> tuple[str | None, list[Json]]:
        """`anyOf`/`oneOf` distributed over the node's other constraints: `base ∧ (m1 ∨ m2)` is
        `(base ∧ m1) ∨ (base ∧ m2)`, so a requirement written inside one alternative is judged
        like one written on the node. A node without either is its own single alternative."""
        present = [k for k in ("oneOf", "anyOf") if k in node]
        if not present:
            return None, [node]
        if len(present) > 1:
            raise ValueError(
                "a schema with both oneOf and anyOf is not supported: extend the walker"
            )
        keyword = present[0]
        base = {k: v for k, v in node.items() if k != keyword}
        return keyword, [
            self._normalise(doc, _conjoin(base, self._normalise(doc, m)[0]))[0]
            for m in node[keyword]
        ]

    def operation(
        self, loc: str, snap_item: Json, snap_op: Json, served_item: Json, served_op: Json
    ) -> None:
        self._parameters(loc, snap_item, snap_op, served_item, served_op)
        self._request_body(loc, snap_op, served_op)
        served_responses = served_op.get("responses", {})
        for status, snap_resp in snap_op.get("responses", {}).items():
            rloc = f"{loc}.responses.{status}"
            if status not in served_responses:
                self._fail(rloc, "response removed")
                continue
            snap_resp, _ = self._resolve(self.snapshot, snap_resp)
            served_resp, _ = self._resolve(self.served, served_responses[status])
            self._content(rloc, snap_resp, served_resp, RESPONSE)
            served_headers = served_resp.get("headers", {})
            for header, snap_header in snap_resp.get("headers", {}).items():
                hloc = f"{rloc}.headers.{header}"
                if header not in served_headers:
                    self._fail(hloc, "header removed")
                    continue
                self._schema(
                    hloc,
                    snap_header.get("schema", {}),
                    served_headers[header].get("schema", {}),
                    RESPONSE,
                    (),
                )

    def _parameter_map(self, doc: Json, item: Json, op: Json) -> dict[tuple[str, str], Json]:
        params = {}
        for raw in [*item.get("parameters", []), *op.get("parameters", [])]:
            param, _ = self._resolve(doc, raw)
            params[(param["name"], param["in"])] = param
        return params

    def _parameters(
        self, loc: str, snap_item: Json, snap_op: Json, served_item: Json, served_op: Json
    ) -> None:
        snap_params = self._parameter_map(self.snapshot, snap_item, snap_op)
        served_params = self._parameter_map(self.served, served_item, served_op)
        for key, snap_param in snap_params.items():
            ploc = f"{loc}.parameters.{key[0]}({key[1]})"
            served_param = served_params.get(key)
            if served_param is None:
                self._fail(ploc, "parameter removed")
                continue
            self.result.parameters += 1
            if served_param.get("required", False) and not snap_param.get("required", False):
                self._fail(ploc, "parameter became required")
            self._schema(
                ploc, snap_param.get("schema", {}), served_param.get("schema", {}), REQUEST, ()
            )
        for key, served_param in served_params.items():
            if key not in snap_params and served_param.get("required", False):
                self._fail(f"{loc}.parameters.{key[0]}({key[1]})", "new required parameter")

    def _request_body(self, loc: str, snap_op: Json, served_op: Json) -> None:
        bloc = f"{loc}.requestBody"
        if "requestBody" not in snap_op:
            if "requestBody" in served_op:
                served_body, _ = self._resolve(self.served, served_op["requestBody"])
                if served_body.get("required", False):
                    self._fail(bloc, "new required request body")
            return
        if "requestBody" not in served_op:
            self._fail(bloc, "request body removed")
            return
        snap_body, _ = self._resolve(self.snapshot, snap_op["requestBody"])
        served_body, _ = self._resolve(self.served, served_op["requestBody"])
        if served_body.get("required", False) and not snap_body.get("required", False):
            self._fail(bloc, "request body became required")
        self._content(bloc, snap_body, served_body, REQUEST)

    def _content(self, loc: str, snap: Json, served: Json, direction: str) -> None:
        served_content = served.get("content", {})
        for media, snap_media in snap.get("content", {}).items():
            mloc = loc if media == "application/json" else f"{loc}.content[{media}]"
            if media not in served_content:
                self._fail(mloc, f"media type {media} removed")
                continue
            if "schema" in snap_media:
                self._schema(
                    mloc,
                    snap_media["schema"],
                    served_content[media].get("schema", {}),
                    direction,
                    (),
                )

    def _schema(self, loc: str, snap: Json, served: Json, direction: str, stack: tuple) -> None:
        snap, snap_name = self._normalise(self.snapshot, snap)
        served, served_name = self._normalise(self.served, served)
        if snap_name is not None:
            loc = f"{loc} → {snap_name}"
        frame = (snap_name, served_name)
        if snap_name is not None and frame in stack:
            return  # a reference cycle: this pair is already being compared further up
        stack = (*stack, frame)

        snap_keyword, snap_alternatives = self._alternatives(self.snapshot, snap)
        served_keyword, served_alternatives = self._alternatives(self.served, served)
        if snap_keyword is not None or served_keyword is not None:
            self._match(
                loc,
                (snap_keyword, snap_alternatives),
                (served_keyword, served_alternatives),
                direction,
                stack,
            )
            return

        snap_types, served_types = _types(snap), _types(served)
        if snap_types is not None and snap_types != served_types:
            self._fail(loc, f"type changed from {snap_types} to {served_types}")

        self._enum(loc, snap, served, direction)
        self._object(loc, snap, served, direction, stack)

        if isinstance(snap.get("items"), dict):
            if not isinstance(served.get("items"), dict):
                self._fail(loc, "items removed")
            else:
                self._schema(f"{loc}[]", snap["items"], served["items"], direction, stack)
        if isinstance(snap.get("additionalProperties"), dict):
            if not isinstance(served.get("additionalProperties"), dict):
                self._fail(loc, "additionalProperties removed")
            else:
                self._schema(
                    f"{loc}{{}}",
                    snap["additionalProperties"],
                    served["additionalProperties"],
                    direction,
                    stack,
                )

    def _enum(self, loc: str, snap: Json, served: Json, direction: str) -> None:
        snap_members, served_members = _members(snap), _members(served)
        if snap_members is None:
            return
        if direction == REQUEST:
            if served_members is not None and not snap_members <= served_members:
                self._fail(
                    loc, f"enum no longer accepts {sorted(snap_members - served_members, key=repr)}"
                )
        elif served_members is None:
            self._fail(loc, "enum may now return any value")
        elif not served_members <= snap_members:
            self._fail(
                loc, f"enum may now return {sorted(served_members - snap_members, key=repr)}"
            )

    def _object(self, loc: str, snap: Json, served: Json, direction: str, stack: tuple) -> None:
        snap_props = snap.get("properties", {})
        served_props = served.get("properties", {})
        for name, snap_prop in snap_props.items():
            if name not in served_props:
                self._fail(f"{loc}.{name}", "property removed")
                continue
            self.result.properties += 1
            self._schema(f"{loc}.{name}", snap_prop, served_props[name], direction, stack)
        if "properties" not in snap and "required" not in snap:
            return
        snap_required = set(snap.get("required", []))
        served_required = set(served.get("required", []))
        if direction == RESPONSE:
            for name in snap.get("required", []):
                if name in served_props and name not in served_required:
                    self._fail(f"{loc}.{name}", "response property no longer required")
        else:
            for name in served.get("required", []):
                if name not in snap_required:
                    self._fail(f"{loc}.{name}", "request property became required")

    def _match(
        self,
        loc: str,
        snap: tuple[str | None, list[Json]],
        served: tuple[str | None, list[Json]],
        direction: str,
        stack: tuple,
    ) -> None:
        """Alternatives, judged like enum members: on a REQUEST every snapshot alternative must
        still be accepted by some served one (a served member may be added, not dropped); on a
        RESPONSE every served alternative must still decode as some snapshot one (a served member
        may be dropped, not added)."""
        (snap_keyword, snap_alts), (served_keyword, served_alts) = snap, served
        if direction == REQUEST:
            for i, alt in enumerate(snap_alts):
                if not any(self._clean(alt, other, direction, stack) for other in served_alts):
                    if snap_keyword is None:
                        self._fail(loc, "no served alternative accepts it")
                    else:
                        self._fail(f"{loc}.{snap_keyword}[{i}]", "member no longer served")
        else:
            for j, other in enumerate(served_alts):
                if not any(self._clean(alt, other, direction, stack) for alt in snap_alts):
                    if served_keyword is None:
                        self._fail(loc, "served shape the client cannot decode")
                    else:
                        self._fail(
                            f"{loc}.{served_keyword}[{j}]", "new member the client cannot decode"
                        )

    def _clean(self, snap: Json, served: Json, direction: str, stack: tuple) -> bool:
        """Whether one pair compares without a failure — asked without counting or reporting."""
        trial = _Walker(self.snapshot, self.served, Comparison())
        trial._schema("", snap, served, direction, stack)
        return not trial.result.failures

    def _fail(self, loc: str, message: str) -> None:
        self.result.failures.append(f"{loc}: {message}")
