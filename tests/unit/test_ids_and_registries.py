"""ID rules and append-only, namespace-bound registries."""

import json
from pathlib import Path

import pytest

from contracts.config import QualityConfig, RunConfig, config_hash
from contracts.ids import (
    IdPrefix,
    canonical_json,
    has_prefix,
    make_id,
    make_namespace,
    stable_digest,
)
from contracts.models import Fact
from contracts.runtime import FactRegistry

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "synthetic"
INPUT_A = "a" * 64
INPUT_B = "b" * 64


def test_canonical_json_is_order_independent() -> None:
    assert canonical_json({"b": 1, "a": [2, "é"]}) == canonical_json({"a": [2, "é"], "b": 1})
    with pytest.raises(ValueError):
        canonical_json(float("nan"))


def test_namespace_depends_on_input_config_and_schema() -> None:
    base = make_namespace(INPUT_A, "c" * 64, "1")
    assert has_prefix(base, IdPrefix.NAMESPACE)
    assert base == make_namespace(INPUT_A, "c" * 64, "1")
    assert base != make_namespace(INPUT_B, "c" * 64, "1")
    assert base != make_namespace(INPUT_A, "d" * 64, "1")
    assert base != make_namespace(INPUT_A, "c" * 64, "2")


def test_ids_are_stable_scoped_and_label_free() -> None:
    ns_a = make_namespace(INPUT_A, "c" * 64, "1")
    ns_b = make_namespace(INPUT_B, "c" * 64, "1")
    payload = {"supply_id": "41022", "country": "DE", "metric": "revenue"}
    fact_id = make_id(IdPrefix.FACT, ns_a, payload)
    assert has_prefix(fact_id, IdPrefix.FACT)
    assert fact_id == make_id(IdPrefix.FACT, ns_a, payload)
    assert fact_id != make_id(IdPrefix.FACT, ns_b, payload)
    assert "41022" not in fact_id and "DE" not in fact_id


def test_make_id_requires_a_namespace() -> None:
    with pytest.raises(ValueError):
        make_id(IdPrefix.FACT, "not-a-namespace", {})
    with pytest.raises(ValueError):
        make_id(IdPrefix.NAMESPACE, make_namespace(INPUT_A, "c" * 64, "1"), {})


def test_digest_length() -> None:
    assert len(stable_digest({"x": 1})) == 16


def test_config_hash_tracks_thresholds() -> None:
    assert config_hash(RunConfig()) == config_hash(RunConfig())
    changed = RunConfig(quality=QualityConfig(max_day_share="0.7"))
    assert config_hash(changed) != config_hash(RunConfig())


def _facts() -> list[Fact]:
    return [Fact.model_validate(f) for f in json.loads((EXAMPLES / "facts.example.json").read_text())]


def test_registry_is_append_only() -> None:
    facts = _facts()
    registry = FactRegistry(facts[0].run_namespace, facts[:2])
    grown = registry.extended(facts[2:])
    assert len(registry) == 2 and len(grown) == len(facts)
    assert facts[0].fact_id in grown and grown[facts[0].fact_id] == facts[0]
    with pytest.raises(ValueError, match="immutable"):
        grown.extended([facts[0]])


def test_registry_refuses_other_namespaces() -> None:
    facts = _facts()
    other = make_namespace(INPUT_B, "c" * 64, "1")
    with pytest.raises(ValueError, match="another run namespace"):
        FactRegistry(other, facts)
