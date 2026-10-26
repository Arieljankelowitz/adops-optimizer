"""Per-candidate answer form: the narrowed response schema and exact repair errors.

Both derive from the pack's code-computed answer guide. The schema only
narrows the contract's own schema (constants, enums and bounds over the IDs
and codes the guide allows), so every answer it admits is still a
``CandidateRecommendation`` and the strict validator still decides. Repair
errors carry codes, paths, IDs and static text only, never model or data text.
"""

import re
from collections.abc import Iterable
from copy import deepcopy

from pydantic import ValidationError

from contracts.enums import Dimension
from pipeline.scope import FULL_INPUT_COHORT
from contracts.enums import ValidationErrorCode as Code
from contracts.models import (
    BreakdownArgs,
    CandidatePack,
    CandidateRecommendation,
    CompareToPeersArgs,
    DailyTrendArgs,
    ExploreStep,
    Fact,
    NextStep,
    ObservationStatement,
    Segment,
    ValidationIssue,
)
from llm.tools import peer_definition
from pipeline.evidence import TEST_SUCCESS_METRICS

REPAIR_INSTRUCTION = (
    "Your previous answer was refused. Return a corrected final recommendation, "
    "no tools. Fix every listed error; keep everything else."
)
# Repair turns list at most this many errors; the bundle keeps all of them.
MAX_REPAIR_ERRORS = 12
_TOOL_ARGS = (BreakdownArgs, DailyTrendArgs, CompareToPeersArgs)
# Field names a schema error path may show; anything else is masked.
_FIELDS = frozenset(
    name
    for model in (NextStep, CandidateRecommendation, ObservationStatement, *_TOOL_ARGS)
    for name in model.model_fields
)


def _fixed_list(values: list[str]) -> dict:
    """Exactly these IDs, each once: a fixed length over exactly these values."""
    if not values:
        return {"type": "array", "maxItems": 0}
    return {
        "type": "array",
        "items": {"enum": list(values)},
        "minItems": len(values),
        "maxItems": len(values),
    }


def _choice_list(values: list, limit: int) -> dict:
    if not values or limit <= 0:
        return {"type": "array", "maxItems": 0}
    return {
        "type": "array",
        "items": {"enum": list(values)},
        "maxItems": min(limit, len(values)),
    }


def _narrow_recommendation(
    schema: dict, pack: CandidatePack, options: list[ObservationStatement]
) -> None:
    """Restrict one CandidateRecommendation schema, in place, to this candidate."""
    c, guide = pack.candidate, pack.answer_guide
    props = schema["properties"]
    props.update(
        origin={"const": c.origin},
        candidate_id={"const": c.candidate_id},
        segment_id={"const": c.segment_id},
        period_id={"const": c.period_id},
        decision={"enum": [d.value for d in guide.allowed_decisions]},
        observation_statements=_choice_list(
            [o.model_dump(mode="json") for o in options], 8
        )
        | ({"minItems": 1} if options else {}),
        counter_evidence_fact_ids=_fixed_list(c.counter_evidence_fact_ids),
        # Code attaches every overlap group (``attach_code_fields``); a slice
        # in a busy export overlaps far more candidates than the model should echo.
        overlap_group_ids={"type": "array", "maxItems": 0},
        merge_source_candidate_ids={"type": "array", "maxItems": 0},
        test_template_id={
            "enum": [t.value for t in pack.permitted_test_templates] + [None]
        },
        assumption_ids=_choice_list([a.assumption_id for a in pack.assumptions], 8),
        # One scoped experiment cites at most one conditional impact.
        scenario_ids=_choice_list([s.scenario_id for s in pack.scenarios], 1),
    )
    schema["required"] = sorted(props)


def attach_code_fields(
    rec: CandidateRecommendation, pack: CandidatePack
) -> CandidateRecommendation:
    """The answer with the fields code owns filled from the candidate.

    ``overlap_group_ids`` is bookkeeping, not judgment: the candidate's own
    groups, which the validator still requires exactly and ranking still uses
    to keep overlapping recommendations apart.
    """
    return CandidateRecommendation.model_validate(
        {
            **rec.model_dump(mode="json"),
            "overlap_group_ids": list(pack.candidate.overlap_group_ids),
        }
    )


def final_schema(pack: CandidatePack, options: list[ObservationStatement]) -> dict:
    """Schema of a final-only turn; the contract's own schema without a guide."""
    schema = CandidateRecommendation.model_json_schema()
    if pack.answer_guide is not None:
        _narrow_recommendation(schema, pack, options)
    return schema


def step_schema(
    pack: CandidatePack,
    options: list[ObservationStatement],
    segments: Iterable[Segment],
    period_ids: set[str],
    tools_left: bool,
) -> dict:
    """Schema of a tool-or-final turn: tool scopes are the ones the tools answer."""
    schema = NextStep.model_json_schema()
    if pack.answer_guide is None:
        return schema
    defs = schema["$defs"]
    _narrow_recommendation(defs["CandidateRecommendation"], pack, options)
    _narrow_tools(schema, pack.run_namespace, segments, period_ids)
    if not tools_left:
        schema["properties"]["kind"] = {"const": "final"}
    return schema


def _drop_tool(schema: dict, model) -> None:
    """Remove one tool from the call union: no slice in scope can use it."""
    union = schema["properties"]["tool_call"]["anyOf"][0]
    ref = f"#/$defs/{model.__name__}"
    union["oneOf"] = [r for r in union["oneOf"] if r["$ref"] != ref]
    name = model.model_fields["tool"].default
    union.get("discriminator", {}).get("mapping", {}).pop(str(name), None)
    del schema["$defs"][model.__name__]


def _narrow_tools(schema: dict, namespace: str, segments, period_ids) -> None:
    """Only calls the deterministic tools answer, so none is refused for its scope.

    Every tool reads full-input slices; a breakdown varies a dimension the
    slice does not fix (one variant per set of free dimensions); peers need a
    slice fixing every dimension.
    """
    segments = list(segments)
    defs = schema["$defs"]
    tool_segments = sorted(
        {s.segment_id for s in segments if s.cohort_id == FULL_INPUT_COHORT}
    )
    for model in _TOOL_ARGS:
        props = defs[model.__name__]["properties"]
        props["segment_id"] = {"enum": tool_segments}
        props["period_id"] = {"enum": sorted(period_ids)}
    by_dims: dict[tuple[str, ...], list[str]] = {}
    for sid, dims in breakdown_dimensions(segments).items():
        by_dims.setdefault(tuple(dims), []).append(sid)
    if by_dims:
        base = defs[BreakdownArgs.__name__]
        variants = []
        for dims, sids in sorted(by_dims.items()):
            variant = deepcopy(base)
            variant["properties"]["segment_id"] = {"enum": sorted(sids)}
            variant["properties"]["by"] = {"enum": list(dims)}
            variants.append(variant)
        defs[BreakdownArgs.__name__] = (
            variants[0] if len(variants) == 1 else {"anyOf": variants}
        )
    else:
        _drop_tool(schema, BreakdownArgs)
    peers = defs[CompareToPeersArgs.__name__]["properties"]
    peers["peer_definition_id"] = {
        "enum": [peer_definition(namespace, d).peer_definition_id for d in Dimension]
    }
    peer_ids = peer_segments(segments)
    if peer_ids:
        peers["segment_id"] = {"enum": sorted(peer_ids)}
    else:
        _drop_tool(schema, CompareToPeersArgs)


def peer_segments(segments) -> set[str]:
    """Slices ``compare_to_peers`` can answer: every dimension fixed, full input."""
    return {
        s.segment_id
        for s in segments
        if s.cohort_id == FULL_INPUT_COHORT and all(d in s.filters for d in Dimension)
    }


def breakdown_dimensions(segments) -> dict[str, list[str]]:
    """Per full-input slice, the dimensions ``breakdown`` may split it by.

    A dimension the slice already fixes has one value only; the tool refuses
    it. A slice fixing every dimension cannot be broken down at all.
    """
    return {
        s.segment_id: [d.value for d in Dimension if d not in s.filters]
        for s in segments
        if s.cohort_id == FULL_INPUT_COHORT and len(s.filters) < len(Dimension)
    }


def explore_schema(
    namespace: str,
    returned: list[Fact],
    segments: Iterable[Segment],
    period_ids: set[str],
    final: bool,
) -> dict:
    """Discovery turn schema: proposals cite only facts the tools returned.

    A proposal's facts must be full-input facts of its own scope; the schema
    lists exactly the returned ones, and ``explore_candidates`` still drops a
    proposal whose facts belong to another scope.
    """
    schema = ExploreStep.model_json_schema()
    defs = schema["$defs"]
    _narrow_tools(schema, namespace, segments, period_ids)
    usable = sorted(
        (f for f in returned if f.segment.cohort_id == "full_input"),
        key=lambda f: f.fact_id,
    )
    if usable:
        props = defs["ExploreProposal"]["properties"]
        props["segment_id"] = {"enum": sorted({f.segment.segment_id for f in usable})}
        props["period_id"] = {"enum": sorted({f.period.period_id for f in usable})}
        props["fact_ids"] = {
            "type": "array",
            "items": {"enum": [f.fact_id for f in usable]},
            "minItems": 1,
            "maxItems": min(8, len(usable)),
        }
    else:
        schema["properties"]["proposals"] = {"type": "array", "maxItems": 0}
    if final:
        schema["properties"]["kind"] = {"const": "final"}
    return schema


def _fix(issue: ValidationIssue, pack: CandidatePack) -> dict | None:
    """What exactly to change, from the guide; None when the detail says it all."""
    guide, c = pack.answer_guide, pack.candidate
    path = issue.path
    if issue.code is Code.SCHEMA_INVALID:
        return {"instruction": "Return JSON that matches the response schema exactly."}
    if guide is None:
        return None
    if path.startswith("observation_statements"):
        return {
            "instruction": "Copy each observation statement whole from "
            "answer_guide.observation_options; never change its fact_ids or their order."
        }
    if path == "counter_evidence_fact_ids":
        return {
            "instruction": "List every one of these counter-evidence fact IDs.",
            "use": c.counter_evidence_fact_ids,
        }
    if path == "overlap_group_ids":
        return {
            "instruction": "overlap_group_ids must be exactly these.",
            "use": c.overlap_group_ids,
        }
    if path == "decision":
        return {
            "instruction": "Choose a decision from this list.",
            "use": [d.value for d in guide.allowed_decisions],
        }
    if path.startswith("scenario_ids"):
        labels = [
            s.model_dump(mode="json")
            for s in guide.scenario_labels
            if issue.ref in (None, s.scenario_id)
        ]
        return {
            "instruction": "Either remove the scenario from scenario_ids, or add all "
            "of its assumption_ids, prerequisite_codes and risk_codes.",
            "use": labels,
        }
    if path in ("assumption_ids", "success_metric_keys", "test_template_id"):
        test = guide.recommend_test
        if test is None:
            return {
                "instruction": "recommend_test is not allowed here; use investigate_first "
                "or reject, and remove test-only assumptions."
            }
        return {
            "instruction": "recommend_test needs a listed test template and all of "
            "these assumption_ids and success_metric_keys.",
            "use": {
                "test_template_ids": [t.value for t in test.test_template_ids],
                "assumption_ids": test.assumption_ids,
                "success_metric_keys": [m.value for m in TEST_SUCCESS_METRICS],
            },
        }
    if issue.code is Code.UNKNOWN_REFERENCE:
        return {"instruction": "Use only IDs supplied in the evidence."}
    return None


def repair_errors(issues: list[ValidationIssue], pack: CandidatePack) -> list[dict]:
    """The errors a repair turn shows, each with the exact change it needs."""
    out = []
    for issue in issues[:MAX_REPAIR_ERRORS]:
        item = issue.model_dump(mode="json", exclude_none=True)
        fix = _fix(issue, pack)
        if fix is not None:
            item["fix"] = fix
        out.append(item)
    return out


def _safe_path(parts) -> str:
    """A JSON path over known field names and indices; anything else is masked."""
    shown = [
        str(p) if isinstance(p, int) or p in _FIELDS else "*" for p in parts
    ]
    return ("/" + "/".join(shown))[:200] if shown else "$"


_OUTPUT_DETAILS = {
    "finish_reason": "the answer did not finish within the output limit",
    "empty_content": "the answer was empty",
    "invalid_json": "the answer was not JSON",
    "thinking_trace_present": "the answer contained a thinking trace",
    "unexpected_tool_calls": "the answer called a tool where none was allowed",
    "invalid_native_tool_call": "the native tool call was invalid",
}


def output_issues(errors: list[str]) -> list[ValidationIssue]:
    """Transport-level refusals as issues; jsonschema messages are not kept."""
    out = []
    for error in errors[:32]:
        head = error.split(":", 1)[0]
        if head.startswith("/"):
            path = _safe_path(
                int(p) if p.isdigit() else p for p in head.strip("/").split("/") if p
            )
            detail = "value does not match the response schema"
        else:
            path = "$"
            detail = _OUTPUT_DETAILS.get(head, "model output failed the response schema")
        out.append(ValidationIssue(code=Code.SCHEMA_INVALID, path=path, detail=detail))
    return out or [
        ValidationIssue(
            code=Code.SCHEMA_INVALID, path="$", detail="model output failed the response schema"
        )
    ]


def exception_issues(exc: Exception) -> list[ValidationIssue]:
    """Parse/scope failures as issues: field paths and static codes only."""
    if isinstance(exc, ValidationError):
        return [
            ValidationIssue(
                code=Code.SCHEMA_INVALID,
                path=_safe_path(e["loc"]),
                detail=f"contract check failed: {e['type']}"[:300],
            )
            for e in exc.errors()[:32]
        ]
    text = str(exc)
    detail = text if re.fullmatch(r"[a-z_]{1,80}", text) else type(exc).__name__
    return [ValidationIssue(code=Code.SCHEMA_INVALID, path="$", detail=detail)]
