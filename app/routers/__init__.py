"""Shared request-schema pieces for the routers."""

from typing import Annotated, Any

from pydantic import BeforeValidator
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
