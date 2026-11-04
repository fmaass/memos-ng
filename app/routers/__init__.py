"""Shared request-schema pieces for the routers."""

from typing import Annotated, Any

from pydantic import BeforeValidator, StrictBool
from pydantic.json_schema import SkipJsonSchema

from app.models import Visibility


def _reject_explicit_null(value: Any) -> Any:
    if value is None:
        raise ValueError("must be a visibility value; omit the field to leave it unset")
    return value


# Apple's OpenAPI generator cannot turn `anyOf: [$ref, {type: null}]` into a usable typed optional,
# so these fields must reach the schema as a plain `$ref` that is simply not required
# (SkipJsonSchema drops the null branch). SkipJsonSchema is a schema-only annotation and would
# still ACCEPT an explicit JSON null, leaving the schema saying one thing and the server another —
# the BeforeValidator closes that gap: absent is the only way to say "unset". Pydantic does not
# validate defaults, so an absent field keeps its `None` default and never reaches the validator.
RequestVisibility = Annotated[
    Visibility | SkipJsonSchema[None], BeforeValidator(_reject_explicit_null)
]


def _reject_explicit_null_flag(value: Any) -> Any:
    if value is None:
        raise ValueError("must be true or false; omit the field to leave it unchanged")
    return value


# The same contract for an optional boolean: a plain `boolean` that is simply not required, an
# explicit JSON null refused. Strict, so only a JSON true/false sets it — "yes" or 1 is a 422, not a
# silently coerced opt-in.
RequestFlag = Annotated[
    StrictBool | SkipJsonSchema[None], BeforeValidator(_reject_explicit_null_flag)
]
