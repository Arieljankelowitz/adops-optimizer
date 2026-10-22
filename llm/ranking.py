"""Reference-only final selection: bounded repairs, stable input and no evidence rewriting."""

from pydantic import ValidationError

from contracts.config import RunBudget
from contracts.enums import Decision
from contracts.ids import canonical_json
from contracts.models import RankingIR, ValidatedRecommendation
from contracts.runtime import ChatMessage, FactRegistry, ModelClient
from llm.analysis import generate, reasoning_stats
from llm.client import ModelOutputError
from llm.prompts import RANKING_PROMPT, facts_view


class RankingError(ValueError):
    """Ranking is incomplete/invalid; the run must not be labelled final."""


def _refs(rec):
    r = rec.recommendation
    return set(r.counter_evidence_fact_ids).union(
        *(set(s.fact_ids) for s in r.observation_statements)
    )


def _scope_fact(rec, registry):
    return next(
        (
            f
            for f in registry.values()
            if f.segment.segment_id == rec.segment_id
            and f.period.period_id == rec.period_id
        ),
        None,
    )


def _scopes_overlap(scope, previous) -> bool:
    compatible = all(
        previous.segment.filters.get(d, label) == label
        for d, label in scope.segment.filters.items()
    )
    return (
        compatible
        and scope.segment.cohort_id == previous.segment.cohort_id
        and bool(set(scope.period.included_dates) & set(previous.period.included_dates))
    )


def conflicting_pairs(validated, registry) -> list[list[str]]:
    """Recommendation pairs a ranking may not select together (``check_ranking``'s rule)."""
    scoped = [
        (r.recommendation_id, _scope_fact(r.recommendation, registry), set(r.recommendation.overlap_group_ids))
        for r in sorted(validated, key=lambda r: r.recommendation_id)
    ]
    pairs = []
    for i, (left, left_scope, left_groups) in enumerate(scoped):
        for right, right_scope, right_groups in scoped[i + 1 :]:
            if left_groups & right_groups or (
                left_scope is not None
                and right_scope is not None
                and _scopes_overlap(right_scope, left_scope)
            ):
                pairs.append([left, right])
    return pairs


def ranking_schema(validated) -> dict:
    """The ranking schema over these validated IDs and their evidence only."""
    schema = RankingIR.model_json_schema()
    ids = [r.recommendation_id for r in validated]
    facts = sorted(set().union(*(_refs(r) for r in validated)))
    one_id = {"enum": ids}
    schema["properties"]["ranked_recommendation_ids"] = {
        "type": "array",
        "items": one_id,
        "minItems": 1,
        "maxItems": min(10, len(ids)),
    }
    merge = schema["$defs"]["MergeInstruction"]["properties"]
    merge["target_recommendation_id"] = one_id
    merge["source_recommendation_ids"]["items"] = one_id
    theme = schema["$defs"]["SummaryTheme"]["properties"]
    theme["recommendation_ids"]["items"] = one_id
    theme["fact_ids"]["items"] = {"enum": facts} if facts else {}
    if not facts:
        theme["fact_ids"]["maxItems"] = 0
    return schema


def check_ranking(ranking, validated, registry):
    by_id = {r.recommendation_id: r for r in validated}
    ids = ranking.ranked_recommendation_ids
    if len(ids) != len(set(ids)) or not set(ids) <= by_id.keys():
        raise RankingError("unknown_or_duplicate_ranked_id")
    if by_id and not ids:
        raise RankingError("empty_actionable_ranking")
    # An identical scope is its own union. Wider unions cannot be represented
    # by this IR and fail closed. Every supporting/cautionary ref must already
    # be in the validated target; ranking cannot invent their union.
    absorbed = set()
    for merge in ranking.merge_instructions:
        target = by_id.get(merge.target_recommendation_id)
        if target is None or merge.target_recommendation_id not in ids:
            raise RankingError("unknown_merge_target")
        sources = merge.source_recommendation_ids
        if len(sources) != len(set(sources)):
            raise RankingError("duplicate_merge_source")
        t = target.recommendation
        for rid in sources:
            source = by_id.get(rid)
            if source is None or rid in ids or rid in absorbed:
                raise RankingError("invalid_merge_source")
            s = source.recommendation
            if (s.segment_id, s.period_id, s.test_template_id, s.origin) != (
                t.segment_id,
                t.period_id,
                t.test_template_id,
                t.origin,
            ):
                raise RankingError("merge_requires_validated_union_scope")
            source_statements = {
                (o.template_id, tuple(o.fact_ids)) for o in s.observation_statements
            }
            target_statements = {
                (o.template_id, tuple(o.fact_ids)) for o in t.observation_statements
            }
            if (
                not _refs(source) <= _refs(target)
                or not source_statements <= target_statements
            ):
                raise RankingError("merge_loses_evidence")
            for field in (
                "counter_evidence_fact_ids",
                "assumption_ids",
                "scenario_ids",
                "hypothesis_codes",
                "prerequisite_codes",
                "risk_codes",
                "success_metric_keys",
                "overlap_group_ids",
            ):
                if not set(getattr(s, field)) <= set(getattr(t, field)):
                    raise RankingError("merge_loses_caveats")
            absorbed.add(rid)
    overlaps, scopes = set(), []
    for rid in ids:
        rec = by_id[rid].recommendation
        scope = _scope_fact(rec, registry)
        if scope is not None:
            for previous in scopes:
                if _scopes_overlap(scope, previous):
                    raise RankingError("overlapping_ranked_scopes")
            scopes.append(scope)
        groups = set(rec.overlap_group_ids)
        if overlaps & groups:
            raise RankingError("overlapping_recommendations_require_resolution")
        overlaps.update(groups)
    allowed_facts = set().union(*(_refs(r) for r in by_id.values())) if by_id else set()
    for theme in ranking.summary_themes:
        if not set(theme.recommendation_ids) <= set(ids):
            raise RankingError("summary_unselected_recommendation")
        if not set(theme.fact_ids) <= allowed_facts:
            raise RankingError("summary_unvalidated_fact")
        if not theme.fact_ids and not theme.recommendation_ids:
            raise RankingError("unreferenced_summary")
        if any(f not in registry for f in theme.fact_ids):
            raise RankingError("summary_unknown_fact")


def rank_recommendations(
    validated: list[ValidatedRecommendation],
    registry: FactRegistry,
    client: ModelClient,
    budget: RunBudget,
) -> RankingIR:
    """One selection call plus one repair; no fallback masquerades as model ranking."""
    if any(r.run_namespace != registry.namespace for r in validated):
        raise RankingError("ranking_namespace_mismatch")
    actionable = sorted(
        (
            r
            for r in validated
            if r.recommendation.decision
            in (Decision.RECOMMEND_TEST, Decision.INVESTIGATE_FIRST)
        ),
        key=lambda r: r.recommendation_id,
    )
    if len({r.recommendation_id for r in actionable}) != len(actionable):
        raise RankingError("duplicate_validated_id")
    if not actionable:
        return RankingIR(
            ranked_recommendation_ids=[], merge_instructions=[], summary_themes=[]
        )
    refs = set().union(*(_refs(r) for r in actionable))
    if any(ref not in registry for ref in refs):
        raise RankingError("unresolved_validated_fact")
    messages = [
        ChatMessage(role="system", content=RANKING_PROMPT),
        ChatMessage(
            role="user",
            content=canonical_json(
                {
                    "validated_recommendations": [
                        r.model_dump(mode="json") for r in actionable
                    ],
                    "evidence": facts_view([registry[r] for r in sorted(refs)]),
                    # Computed by the same rule the ranking check applies.
                    "conflicting_pairs": conflicting_pairs(actionable, registry),
                    "merge_policy": "Identical scopes only, with every source fact and caveat already in the validated target; otherwise do not merge.",
                    "omission_policy": "Unselected IDs remain in the audit trail; never rewrite them.",
                }
            ),
        ),
    ]
    state = reasoning_stats(client)
    if budget.max_ranking_calls < 1:
        raise RankingError("ranking_budget_exhausted")
    for attempt in range(1 + min(max(budget.max_ranking_repairs, 0), 1)):
        if state.ranking_calls >= 2:
            raise RankingError("ranking_budget_exhausted")
        state.ranking_calls += 1
        try:
            response = generate(
                client,
                budget,
                messages,
                RankingIR,
                "recommendation_ranking",
                schema=ranking_schema(actionable),
            )
            ranking = RankingIR.model_validate_json(response.content)
            check_ranking(ranking, actionable, registry)
            validate = getattr(client, "_validate_ranking", None)
            if validate:
                verdict = validate(ranking)
                if not verdict.accepted:
                    raise RankingError("strict_validator_rejected_ranking")
            return ranking
        except (ModelOutputError, ValidationError, RankingError) as exc:
            state.invalid_outputs += 1
            if attempt >= min(max(budget.max_ranking_repairs, 0), 1):
                raise RankingError("ranking_rejected_after_repair") from exc
            # Only reviewed machine codes, never raw model text in the repair.
            code = (
                str(exc) if isinstance(exc, RankingError) else "ranking_schema_invalid"
            )
            messages.append(
                ChatMessage(
                    role="user",
                    content=canonical_json(
                        {
                            "repair_error_code": code,
                            "instruction": "Return corrected ranking JSON only.",
                        }
                    ),
                )
            )
    raise RankingError("ranking_incomplete")
