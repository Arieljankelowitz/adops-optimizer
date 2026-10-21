"""Stable identifier rules.

An ID is ``<prefix>_<digest>`` where the digest is the first 16 hex characters
of SHA-256 over the canonical JSON of ``[namespace, payload]``. IDs therefore:

* never contain raw labels (supply IDs, countries, dates appear only hashed);
* are reproducible for the same input, configuration and schema version;
* differ across run namespaces, so references from another run cannot resolve.

The run namespace itself is ``N_<digest>`` over the input SHA-256, the
configuration hash and the schema version.
"""

import hashlib
import json
import re
from enum import StrEnum
from typing import Any

DIGEST_LENGTH = 16


class IdPrefix(StrEnum):
    NAMESPACE = "N"
    FACT = "F"
    SEGMENT = "G"
    PERIOD = "P"
    CANDIDATE = "C"
    ASSUMPTION = "A"
    SCENARIO = "S"
    OVERLAP_GROUP = "O"
    QUALITY_ISSUE = "Q"
    RECOMMENDATION = "R"
    TOOL_INVOCATION = "T"
    PEER_DEFINITION = "D"


def id_pattern(prefix: IdPrefix) -> str:
    """Regular expression (anchored) that a valid ID with ``prefix`` matches."""
    return rf"^{prefix.value}_[0-9a-f]{{{DIGEST_LENGTH}}}$"


ID_PATTERNS: dict[IdPrefix, re.Pattern[str]] = {p: re.compile(id_pattern(p)) for p in IdPrefix}


def canonical_json(payload: Any) -> str:
    """UTF-8 canonical JSON: sorted keys, no whitespace, no NaN/Infinity."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    )


def stable_digest(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:DIGEST_LENGTH]


def make_namespace(input_sha256: str, config_hash: str, schema_version: str) -> str:
    return f"{IdPrefix.NAMESPACE.value}_{stable_digest([input_sha256, config_hash, schema_version])}"


def make_id(prefix: IdPrefix, namespace: str, payload: Any) -> str:
    """Return the stable ID for ``payload`` inside ``namespace``."""
    if prefix is IdPrefix.NAMESPACE:
        raise ValueError("use make_namespace for run namespaces")
    if not ID_PATTERNS[IdPrefix.NAMESPACE].match(namespace):
        raise ValueError(f"not a run namespace: {namespace!r}")
    return f"{prefix.value}_{stable_digest([namespace, payload])}"


def has_prefix(value: str, prefix: IdPrefix) -> bool:
    return bool(ID_PATTERNS[prefix].match(value))
