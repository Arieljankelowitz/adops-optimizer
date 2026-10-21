"""Contract models: committed schemas/examples, and the invariants models enforce."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from contracts import SCHEMA_VERSION
from contracts.enums import Decision, FactKind, NextStepKind, ValidityStatus
from contracts.models import (
    Assumption,
    Candidate,
    CandidateRecommendation,
    Fact,
    NextStep,
    QualityReport,
    Scenario,
    ValidationResult,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = REPO_ROOT / "examples" / "synthetic"


def _export_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "export_contracts", REPO_ROOT / "scripts" / "export_contracts.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_schemas_and_examples_match_models() -> None:
    stale = [
        str(path.relative_to(REPO_ROOT))
        for path, text in _export_module().rendered_files().items()
        if not path.exists() or path.read_text(encoding="utf-8") != text
    ]
    assert not stale, f"rerun scripts/export_contracts.py: {stale}"


def test_schemas_are_versioned_and_closed() -> None:
    schema_dir = REPO_ROOT / "contracts" / "schemas" / f"v{SCHEMA_VERSION}"
    for path in schema_dir.glob("*.schema.json"):
        schema = json.loads(path.read_text())
        assert schema["x-schema-version"] == SCHEMA_VERSION
        assert f"/v{SCHEMA_VERSION}/" in schema["$id"]
        objects = [schema, *schema.get("$defs", {}).values()]
        for obj in objects:
            if obj.get("type") == "object" and "properties" in obj:
                assert obj.get("additionalProperties") is False, (path.name, obj.get("title"))


EXAMPLE_MODELS = {
    "assumption": Assumption,
    "scenario": Scenario,
    "candidate": Candidate,
    "candidate_recommendation": CandidateRecommendation,
    "next_step_tool_call": NextStep,
    "quality_report": QualityReport,
}


@pytest.mark.parametrize("name,model", EXAMPLE_MODELS.items())
def test_examples_round_trip(name: str, model: type) -> None:
    raw = (EXAMPLES / f"{name}.example.json").read_text()
    parsed = model.model_validate_json(raw)
    assert json.loads(parsed.model_dump_json()) == json.loads(raw)


def test_fact_examples_round_trip_with_decimal_strings() -> None:
    raw = json.loads((EXAMPLES / "facts.example.json").read_text())
    facts = [Fact.model_validate(item) for item in raw]
    assert all(f.value is None or isinstance(f.value, Decimal) for f in facts)
    for item in raw:
        assert item["value"] is None or isinstance(item["value"], str)
    namespaces = {f.run_namespace for f in facts}
    assert len(namespaces) == 1


def test_example_references_resolve() -> None:
    facts = {f["fact_id"] for f in json.loads((EXAMPLES / "facts.example.json").read_text())}
    rec = json.loads((EXAMPLES / "candidate_recommendation.example.json").read_text())
    for statement in rec["observation_statements"]:
        assert set(statement["fact_ids"]) <= facts
    scenario = json.loads((EXAMPLES / "scenario.example.json").read_text())
    assumption = json.loads((EXAMPLES / "assumption.example.json").read_text())
    assert rec["scenario_ids"] == [scenario["scenario_id"]]
    assert rec["assumption_ids"] == [assumption["assumption_id"]]
    assert set(scenario["historical_fact_ids"]) <= facts


def _fact_payload() -> dict:
    return json.loads((EXAMPLES / "facts.example.json").read_text())[0]


def test_fact_rejects_float_values() -> None:
    payload = _fact_payload() | {"value": 2.5}
    with pytest.raises(ValidationError, match="decimal strings"):
        Fact.model_validate(payload)


def test_fact_rejects_non_finite_values() -> None:
    payload = _fact_payload() | {"value": "NaN"}
    with pytest.raises(ValidationError):
        Fact.model_validate(payload)


def test_null_value_needs_undefined_or_invalid_status() -> None:
    payload = _fact_payload() | {"value": None}
    with pytest.raises(ValidationError, match="null value"):
        Fact.model_validate(payload)
    payload["validity"] = {"status": ValidityStatus.UNDEFINED, "reason": "zero requests"}
    assert Fact.model_validate(payload).value is None


def test_non_valid_fact_needs_reason() -> None:
    payload = _fact_payload() | {"value": None, "validity": {"status": "undefined"}}
    with pytest.raises(ValidationError, match="reason"):
        Fact.model_validate(payload)


def test_comparison_detail_only_on_comparison_facts() -> None:
    payload = _fact_payload() | {"kind": FactKind.COMPARISON}
    with pytest.raises(ValidationError, match="comparison detail"):
        Fact.model_validate(payload)


def test_ids_must_match_prefix_pattern() -> None:
    payload = _fact_payload() | {"fact_id": "G_0123456789abcdef"}
    with pytest.raises(ValidationError):
        Fact.model_validate(payload)
    payload = _fact_payload() | {"fact_id": "F_supply-41022"}
    with pytest.raises(ValidationError):
        Fact.model_validate(payload)


def test_unknown_fields_rejected() -> None:
    payload = _fact_payload() | {"note": "free text"}
    with pytest.raises(ValidationError, match="Extra inputs"):
        Fact.model_validate(payload)


def test_assumption_must_stay_within_bounds_and_unobserved() -> None:
    raw = json.loads((EXAMPLES / "assumption.example.json").read_text())
    with pytest.raises(ValidationError, match="bounds"):
        Assumption.model_validate(raw | {"value": "0.90"})
    with pytest.raises(ValidationError):
        Assumption.model_validate(raw | {"not_observed": False})


def test_scenario_is_always_conditional() -> None:
    raw = json.loads((EXAMPLES / "scenario.example.json").read_text())
    with pytest.raises(ValidationError):
        Scenario.model_validate(raw | {"conditional": False})


def test_recommendation_decision_shapes() -> None:
    raw = json.loads((EXAMPLES / "candidate_recommendation.example.json").read_text())
    with pytest.raises(ValidationError, match="test template"):
        CandidateRecommendation.model_validate(raw | {"test_template_id": None})
    with pytest.raises(ValidationError, match="merge"):
        CandidateRecommendation.model_validate(raw | {"decision": Decision.MERGE})
    merged = CandidateRecommendation.model_validate(
        raw | {"decision": Decision.MERGE, "merge_source_candidate_ids": [raw["candidate_id"]]}
    )
    assert merged.decision is Decision.MERGE


def test_recommendation_rejects_typed_numbers() -> None:
    raw = json.loads((EXAMPLES / "candidate_recommendation.example.json").read_text())
    with pytest.raises(ValidationError):
        CandidateRecommendation.model_validate(raw | {"expected_uplift": "12%"})


def test_next_step_carries_exactly_one_payload() -> None:
    raw = json.loads((EXAMPLES / "next_step_tool_call.example.json").read_text())
    assert NextStep.model_validate(raw).kind is NextStepKind.CALL_TOOL
    with pytest.raises(ValidationError, match="payload"):
        NextStep.model_validate(raw | {"kind": NextStepKind.FINAL})
    bad_tool = raw | {"tool_call": raw["tool_call"] | {"tool": "run_sql"}}
    with pytest.raises(ValidationError):
        NextStep.model_validate(bad_tool)


def test_validation_result_is_accepted_object_or_errors() -> None:
    adapter = TypeAdapter(ValidationResult)
    rejected = adapter.validate_python(
        {"accepted": False, "errors": [
            {"code": "wrong_unit", "path": "observation_statements[0]", "detail": "x"}]}
    )
    assert rejected.accepted is False
    with pytest.raises(ValidationError):
        adapter.validate_python({"accepted": False, "errors": []})
    with pytest.raises(ValidationError):
        adapter.validate_python({"accepted": True})


def test_quality_report_has_fatal() -> None:
    raw = json.loads((EXAMPLES / "quality_report.example.json").read_text())
    report = QualityReport.model_validate(raw)
    assert not report.has_fatal
    raw["issues"][0]["severity"] = "fatal"
    assert QualityReport.model_validate(raw).has_fatal
