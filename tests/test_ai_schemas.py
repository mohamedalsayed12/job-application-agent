"""Every structured-output schema in app/ must use only keywords the Anthropic API accepts.

The API rejects a request whose ``output_config.format.schema`` uses an
unsupported keyword with a 400, so one stray ``maxItems`` breaks the feature
for every user. Enforce length and count limits in code after parsing.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

import pytest

import app

SUPPORTED_FORMATS = {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}
UNSUPPORTED_KEYWORDS = {
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "maxItems",
    "uniqueItems",
    "contains",
    "minContains",
    "maxContains",
    "prefixItems",
    "minProperties",
    "maxProperties",
    "patternProperties",
    "propertyNames",
    "dependentRequired",
    "dependentSchemas",
    "if",
    "then",
    "else",
    "not",
}


def schema_problems(schema: Any, path: str = "$") -> list[str]:
    problems: list[str] = []
    if isinstance(schema, list):
        for i, item in enumerate(schema):
            problems += schema_problems(item, f"{path}[{i}]")
        return problems
    if not isinstance(schema, dict):
        return problems
    for key in UNSUPPORTED_KEYWORDS & schema.keys():
        problems.append(f"{path}.{key}")
    if schema.get("minItems", 0) not in (0, 1):
        problems.append(f"{path}.minItems={schema['minItems']}")
    if "format" in schema and isinstance(schema["format"], str) and schema["format"] not in SUPPORTED_FORMATS:
        problems.append(f"{path}.format={schema['format']}")
    if schema.get("type") == "object" and schema.get("additionalProperties") is not False:
        problems.append(f"{path}.additionalProperties must be false")
    for key, value in schema.items():
        if key == "properties" and isinstance(value, dict):
            for name, sub in value.items():
                problems += schema_problems(sub, f"{path}.properties.{name}")
        elif key in {"items", "anyOf", "allOf", "$defs", "definitions"}:
            subs = value.values() if isinstance(value, dict) and key in {"$defs", "definitions"} else [value]
            for sub in subs:
                problems += schema_problems(sub, f"{path}.{key}")
    return problems


def _is_output_schema(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") == "object" and isinstance(value.get("properties"), dict)


def output_schemas() -> dict[str, dict]:
    found: dict[str, dict] = {}
    seen: set[int] = set()
    for module_info in pkgutil.walk_packages(app.__path__, prefix="app."):
        module = importlib.import_module(module_info.name)
        for name, value in vars(module).items():
            if name.isupper() and _is_output_schema(value) and id(value) not in seen:
                seen.add(id(value))
                found[f"{module.__name__}.{name}"] = value
    return found


SCHEMAS = output_schemas()


def test_walk_finds_every_known_output_schema() -> None:
    from app.applicants.resume_parse import CV_READ_SCHEMA
    from app.interview.analyse import ANALYSE_SCHEMA
    from app.interview.plan import PACK_SCHEMA
    from app.matching.judge import VERDICT_SCHEMA
    from app.matching.report import REPORT_SCHEMA
    from app.outreach.draft import SCHEMA as DRAFT_SCHEMA
    from app.summaries.generate import SUMMARY_SCHEMA
    from app.tailor.ai import KEYWORDS_SCHEMA
    from app.tailor.improver import CHANGE_SCHEMA
    from app.tailor.resume import RESUME_SCHEMA

    known = [CV_READ_SCHEMA, ANALYSE_SCHEMA, PACK_SCHEMA, VERDICT_SCHEMA, REPORT_SCHEMA, DRAFT_SCHEMA,
             SUMMARY_SCHEMA, KEYWORDS_SCHEMA, CHANGE_SCHEMA, RESUME_SCHEMA]
    walked = {id(schema) for schema in SCHEMAS.values()}
    assert all(id(schema) in walked for schema in known)


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_output_schema_uses_only_supported_keywords(name: str) -> None:
    assert schema_problems(SCHEMAS[name]) == []


def test_checker_flags_the_keyword_that_broke_outreach() -> None:
    bad = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"pattern_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 6}},
    }
    assert schema_problems(bad) == ["$.properties.pattern_ids.maxItems"]
