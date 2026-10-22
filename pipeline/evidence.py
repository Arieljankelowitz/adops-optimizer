"""Namespace-bound evidence, immutable registries and bounded model payloads.

The full private bundle retains provenance. ``serialize_pack`` is the compact
model-facing view: no rows, no source-line handles, and labels are JSON data,
never interpolated instructions. Exact tokenizer counting also happens at the
transport boundary. A pack that cannot retain its warnings/counter-evidence
within budget is refused rather than silently weakening the evidence.
"""

from collections.abc import Callable, Iterable
from decimal import Decimal
from types import MappingProxyType

from contracts.config import EvidenceConfig
from contracts.enums import (
    AssumptionSelector,
    BudgetReason,
    DetectorFamily,
    EligibilityStatus,
    FactKind,
    MetricKey,
    ScenarioTemplate,
    ToolName,
    Unit,
)
from contracts.ids import IdPrefix, canonical_json, make_id
from contracts.models import (
    Assumption,
    AssumptionBounds,
    Candidate,
    CandidatePack,
    Fact,
    QualityReport,
    ScenarioRequest,
)
from contracts.runtime import (
    AssumptionRegistry,
    EvidenceBundle,
    FactRegistry,
    MetricsStore,
    ScenarioRegistry,
)
from contracts.templates import TestTemplateId as Test
from pipeline.detectors import candidate_evidence
from pipeline.metrics import ADDITIVE, FactBuilder, IssueIndex, stats_row
from pipeline.scenarios import PARAMETERS, REQUIRED_METRICS, evaluate_scenarios

TESTS = {
    DetectorFamily.SCALE_WINNER: Test.MATCHED_ALLOCATION_HOLDOUT,
    DetectorFamily.WASTED_REQUESTS: Test.TARGETED_THROTTLE_HOLDOUT,
    DetectorFamily.MARGIN_LEAK: Test.COMMERCIAL_TERMS_REVIEW,
    DetectorFamily.FLOOR_HIGH_BID: Test.FLOOR_INCREASE_RANDOMIZED,
    DetectorFamily.FLOOR_LOW_BID: Test.FLOOR_DECREASE_RANDOMIZED,
    DetectorFamily.WINS_IMPRESSIONS_DISCREPANCY: Test.COUNTER_RECONCILIATION,
    DetectorFamily.TREND: Test.INSTRUMENTATION_FEASIBILITY,
    DetectorFamily.COMBINATION_OUTLIER: Test.MATCHED_ALLOCATION_HOLDOUT,
}
SCENARIOS = {
    DetectorFamily.SCALE_WINNER: ScenarioTemplate.ADDITIONAL_ELIGIBLE_REQUESTS,
    DetectorFamily.WASTED_REQUESTS: ScenarioTemplate.THROTTLE,
    DetectorFamily.MARGIN_LEAK: ScenarioTemplate.MARGIN_NEGOTIATION_SENSITIVITY,
}


class EvidenceBudgetError(ValueError):
    """Mandatory evidence cannot fit; no unsafe partial pack was emitted."""


# Fact rows carry ``None`` for the overwhelmingly common validity.
DEFAULT_VALIDITY = {"reason": None, "status": "valid"}


def pack_payload(pack: CandidatePack) -> dict:
    """The compact JSON document the model reads for one candidate.

    This is the single serialization of a pack: ``build_evidence`` measures it to
    fit the token budget and ``llm.prompts`` sends it, so what is gated is what
    is sent. The private pack stays fully typed.

    Scope/period tables, fact types and the shared namespace avoid repeating
    them per fact. Provenance needed for reasoning (formula, parents, weighting)
    stays visible and quality issues carry their own scope. Source
    hashes/columns/line handles remain in the private registry. Numeric-looking
    labels live only in the explicitly untrusted segment table.
    """
    payload = pack.model_dump(mode="json")
    payload["untrusted_segment_labels"] = payload.pop("segments")
    # Dictionary-encoded metadata avoids repeating long scope/unit/formula
    # strings. Table indices are metadata, never claim quantities. Stable fact
    # IDs and all decimal values stay verbatim; no numeric rounding/compression.
    segment_indices = {
        s["segment_id"]: n for n, s in enumerate(payload["untrusted_segment_labels"])
    }
    period_indices = {p["period_id"]: n for n, p in enumerate(payload["periods"])}
    # build_evidence always lists every scope its facts use; a hand-built pack
    # that does not still gets the missing ones appended rather than dropped.
    for f in payload["facts"]:
        if f["segment"]["segment_id"] not in segment_indices:
            segment_indices[f["segment"]["segment_id"]] = len(segment_indices)
            payload["untrusted_segment_labels"].append(f["segment"])
        if f["period"]["period_id"] not in period_indices:
            period_indices[f["period"]["period_id"]] = len(period_indices)
            payload["periods"].append(f["period"])
    fact_types = sorted(
        {
            (
                f["kind"],
                f["metric"],
                f["unit"],
                f["provenance"]["formula_id"],
                f["provenance"]["weighting"],
            )
            for f in payload["facts"]
        }
    )
    payload["fact_type_fields"] = ["kind", "metric", "unit", "formula_id", "weighting"]
    payload["fact_types"] = [list(t) for t in fact_types]
    payload["fact_fields"] = [
        "fact_id",
        "segment_index",
        "period_index",
        "type_index",
        "value",
        "validity",
        "parent_fact_ids",
        "comparison",
    ]
    payload["null_validity_means"] = DEFAULT_VALIDITY
    payload["facts"] = [
        [
            f["fact_id"],
            segment_indices[f["segment"]["segment_id"]],
            period_indices[f["period"]["period_id"]],
            fact_types.index(
                (
                    f["kind"],
                    f["metric"],
                    f["unit"],
                    f["provenance"]["formula_id"],
                    f["provenance"]["weighting"],
                )
            ),
            f["value"],
            None if f["validity"] == DEFAULT_VALIDITY else f["validity"],
            f["provenance"]["parent_fact_ids"],
            f["comparison"],
        ]
        for f in payload["facts"]
    ]
    # Issues are rows over one shared field list; every restriction and fact ref
    # is kept.
    issues = payload["quality_issues"]
    fields = sorted(issues[0]) if issues else []
    payload["quality_issue_fields"] = fields
    payload["quality_issues"] = [[i[k] for k in fields] for i in issues]
    # These tables retain every restriction and fact ref. Presentation-only
    # prose and redundant namespace fields stay in the private typed objects.
    payload["assumptions"] = [
        {
            k: a[k]
            for k in (
                "assumption_id",
                "parameter_key",
                "value",
                "unit",
                "bounds",
                "segment_id",
                "period_id",
                "dependency_ids",
                "not_observed",
            )
        }
        for a in payload["assumptions"]
    ]
    return payload


def serialize_pack(pack: CandidatePack) -> str:
    """Canonical text of ``pack_payload``; the size the token gate measures."""
    return canonical_json(pack_payload(pack))


def _closure(ids: Iterable[str], facts: dict[str, Fact]) -> set[str]:
    seen: set[str] = set()
    pending = list(ids)
    while pending:
        ref = pending.pop()
        if ref in seen:
            continue
        if ref not in facts:
            raise ValueError("evidence contains an unresolved fact reference")
        seen.add(ref)
        fact = facts[ref]
        pending.extend(fact.provenance.parent_fact_ids)
        if fact.comparison:
            pending.extend(
                (fact.comparison.left_fact_id, fact.comparison.right_fact_id)
            )
    return seen


def _assumption(ns, segment, period, key, value, unit, low, high) -> Assumption:
    payload = {
        "parameter_key": key,
        "value": format(Decimal(value), "f"),
        "unit": unit.value,
        "segment_id": segment,
        "period_id": period,
    }
    return Assumption(
        assumption_id=make_id(IdPrefix.ASSUMPTION, ns, payload),
        run_namespace=ns,
        label=key.replace("_", " "),
        parameter_key=key,
        value=value,
        unit=unit,
        bounds=AssumptionBounds(minimum=low, maximum=high),
        rationale="Planning parameter only; not observed and not a statistical guarantee.",
        selector=AssumptionSelector.DEFAULT_TEST_TEMPLATE,
        segment_id=segment,
        period_id=period,
        dependency_ids=[],
    )


def build_evidence(
    metrics: MetricsStore,
    quality: QualityReport,
    candidates: list[Candidate],
    config: EvidenceConfig,
    *,
    count_tokens: Callable[[str], int] | None = None,
) -> EvidenceBundle:
    """``build_evidence_bounded`` where an irreducible pack fails the build."""
    return build_evidence_bounded(
        metrics, quality, candidates, config, count_tokens=count_tokens
    )


def build_evidence_bounded(
    metrics: MetricsStore,
    quality: QualityReport,
    candidates: list[Candidate],
    config: EvidenceConfig,
    *,
    count_tokens: Callable[[str], int] | None = None,
    skip_oversize: bool = False,
) -> EvidenceBundle:
    """Promote real detector/quality facts, compute scenarios, then bound packs.

    Optional ``count_tokens`` must be the deployed tokenizer. Without one only
    the byte-size gate is applied; the client MUST count the complete request
    with its deployed tokenizer before inference. No chars/token guess is used.
    All candidates stay in the private audit registry; only non-rejected first
    ``max_candidates`` get model packs. Redundant daily/peer rows may be removed,
    never triggers, contrary evidence, quality facts or scenario dependencies.
    A candidate whose mandatory evidence alone exceeds the budget raises
    ``EvidenceBudgetError``, or with ``skip_oversize`` gets no pack and is
    listed in the bundle's ``unpacked`` with the reason.
    """
    ns = metrics.run_namespace
    if quality.run_namespace != ns:
        raise ValueError("quality belongs to another run namespace")
    if len({c.candidate_id for c in candidates}) != len(candidates):
        raise ValueError("duplicate candidate IDs")
    detected = candidate_evidence(metrics, quality, candidates)
    facts = {f.fact_id: f for f in detected.facts.values()}
    for fact in quality.facts:
        if fact.fact_id in facts and facts[fact.fact_id] != fact:
            raise ValueError("quality fact conflicts with a registered immutable fact")
        facts[fact.fact_id] = fact

    # Supply account and candidate historical inputs even if a detector never
    # needed that metric. Reuse B's FactBuilder (same IDs, units and lineage).
    builder = FactBuilder(metrics, quality)
    scopes = {(c.segment_id, c.period_id) for c in candidates}
    scopes.update(
        (r["segment_id"], r["period_id"])
        for r in metrics.aggregates.to_dict("records")
        if not metrics.segments[r["segment_id"]].filters
    )
    for sid, pid in sorted(scopes):
        row = stats_row(metrics, sid, pid)
        if row is None:
            raise ValueError("candidate references an unknown aggregate scope")
        segment, period = metrics.segments[sid], metrics.periods[pid]
        for metric in ADDITIVE:
            builder.additive(segment, period, row, metric)
        for metric in (
            MetricKey.REVENUE_PER_MILLION_REQUESTS,
            MetricKey.PROFIT_PER_MILLION_REQUESTS,
            MetricKey.MARGIN,
        ):
            builder.rate(segment, period, row, metric)
    for ref, fact in builder.facts.items():
        if ref in facts and facts[ref] != fact:
            raise ValueError("computed fact conflicts with a registered immutable fact")
        facts[ref] = fact
    historical = FactRegistry(ns, facts.values())

    index = IssueIndex(metrics, quality)
    enriched = []
    for candidate in candidates:
        issue_ids = index.issues_for(
            metrics.segments[candidate.segment_id], metrics.periods[candidate.period_id]
        )
        enriched.append(
            candidate.model_copy(update={"quality_issue_ids": issue_ids}, deep=True)
        )
    chosen = [c for c in enriched if c.eligibility is not EligibilityStatus.REJECTED]
    chosen = [] if quality.has_fatal else chosen[: config.max_candidates]

    assumptions: dict[str, Assumption] = {}
    requests = []
    for candidate in chosen:
        sid, pid = candidate.segment_id, candidate.period_id
        template = SCENARIOS.get(candidate.family, ScenarioTemplate.NOT_IDENTIFIABLE)
        params = [
            ("test_allocation", config.test_allocation, Unit.FRACTION, "0.001", "0.5"),
            ("test_min_days", Decimal(config.test_min_days), Unit.DAYS, "1", "365"),
            (
                "test_profit_stop_loss",
                config.test_profit_stop_loss,
                Unit.FRACTION,
                "0.001",
                "0.5",
            ),
        ]
        if template in PARAMETERS:
            key, unit = PARAMETERS[template]
            value = {
                ScenarioTemplate.ADDITIONAL_ELIGIBLE_REQUESTS: config.default_additional_request_fraction,
                ScenarioTemplate.THROTTLE: config.default_throttle_fraction,
                ScenarioTemplate.MARGIN_NEGOTIATION_SENSITIVITY: config.margin_sensitivity_points,
            }[template]
            params.append((key, value, unit, "0", "1"))
        scoped = [_assumption(ns, sid, pid, *p) for p in params]
        assumptions.update((a.assumption_id, a) for a in scoped)
        own = {
            f.metric: f
            for f in builder.facts.values()
            if f.segment.segment_id == sid and f.period.period_id == pid
        }
        metrics_needed = REQUIRED_METRICS[template] or (MetricKey.REQUESTS,)
        requests.append(
            ScenarioRequest(
                template=template,
                segment_id=sid,
                period_id=pid,
                historical_fact_ids=[own[m].fact_id for m in metrics_needed],
                assumption_ids=[
                    a.assumption_id
                    for a in scoped
                    if template in PARAMETERS
                    and a.parameter_key == PARAMETERS[template][0]
                ],
            )
        )
    assumption_registry = AssumptionRegistry(ns, assumptions.values())
    result = evaluate_scenarios(historical, assumption_registry, requests)
    registry = historical.extended(result.facts)
    scenarios = [
        s.model_copy(
            update={
                "overlap_group_ids": sorted(
                    {
                        g
                        for c in enriched
                        if c.segment_id == s.segment_id and c.period_id == s.period_id
                        for g in c.overlap_group_ids
                    }
                )
            },
            deep=True,
        )
        for s in result.scenarios.values()
    ]
    scenario_registry = ScenarioRegistry(ns, scenarios)
    facts = {f.fact_id: f for f in registry.values()}
    segments = dict(metrics.segments)
    periods = dict(metrics.periods)
    for fact in facts.values():
        for table, key, value in (
            (segments, fact.segment.segment_id, fact.segment),
            (periods, fact.period.period_id, fact.period),
        ):
            if key in table and table[key] != value:
                raise ValueError("scope ID resolves to inconsistent metadata")
            table[key] = value
    _closure(facts, facts)
    packs = {}
    unpacked: dict[str, BudgetReason] = {}
    counter = count_tokens
    for candidate in chosen:
        issues = [
            i for i in quality.issues if i.issue_id in candidate.quality_issue_ids
        ]
        scenario_list = [
            s
            for s in scenarios
            if s.segment_id == candidate.segment_id
            and s.period_id == candidate.period_id
        ]
        assumption_list = [
            a
            for a in assumptions.values()
            if a.segment_id == candidate.segment_id
            and a.period_id == candidate.period_id
        ]
        mandatory = [*candidate.trigger_fact_ids, *candidate.counter_evidence_fact_ids]
        # B computes these for every candidate even when no warning fires.
        # Coverage/concentration are required context, not optional contrary
        # evidence only when a threshold happens to be exceeded.
        context_metrics = {
            MetricKey.OBSERVED_DAYS,
            MetricKey.REVENUE_POSITIVE_DAYS,
            MetricKey.MAX_DAY_REVENUE_SHARE,
            MetricKey.TOP_TWO_DAY_REVENUE_SHARE,
            MetricKey.MAX_DAY_PROFIT_SHARE,
            MetricKey.TOP_TWO_DAY_PROFIT_SHARE,
        }
        mandatory.extend(
            f.fact_id
            for f in facts.values()
            if f.period.period_id == candidate.period_id
            and f.kind in (FactKind.MEASURED, FactKind.CALCULATED)
            and (
                (
                    f.segment.segment_id == candidate.segment_id
                    and f.metric in context_metrics
                )
                or (
                    not f.segment.filters
                    and f.segment.cohort_id == "full_input"
                    and f.metric is MetricKey.OBSERVED_DAYS
                )
            )
        )
        for issue in issues:
            mandatory.extend((*issue.measured_fact_ids, *issue.threshold_fact_ids))
            mandatory.extend(
                r
                for r in (
                    issue.request_exposure_fact_id,
                    issue.revenue_exposure_fact_id,
                )
                if r
            )
        for scenario in scenario_list:
            mandatory.extend((*scenario.historical_fact_ids, *scenario.result_fact_ids))
        required = _closure(mandatory, facts)

        def pack_for(
            ids: set[str],
            candidate=candidate,
            issues=issues,
            assumption_list=assumption_list,
            scenario_list=scenario_list,
        ) -> CandidatePack:
            subset = [facts[r] for r in sorted(ids)]
            sids = {f.segment.segment_id for f in subset} | {candidate.segment_id}
            pids = {f.period.period_id for f in subset} | {candidate.period_id}
            for issue in issues:
                sids.update(issue.segment_ids)
                pids.update(issue.period_ids)
            # The full candidate remains in bundle.candidates. The prompt must
            # not advertise refs it did not receive; omissions are retrievable
            # through the allowlisted tools and never alter contrary evidence.
            view = candidate.model_copy(
                update={
                    "daily_fact_ids": [r for r in candidate.daily_fact_ids if r in ids],
                    "peer_fact_ids": [r for r in candidate.peer_fact_ids if r in ids],
                },
                deep=True,
            )
            return CandidatePack(
                run_namespace=ns,
                candidate=view,
                quality_issues=issues,
                segments=[segments[r] for r in sorted(sids)],
                periods=[periods[r] for r in sorted(pids)],
                facts=subset,
                assumptions=assumption_list,
                scenarios=scenario_list,
                permitted_test_templates=[
                    TESTS[candidate.family],
                    Test.INSTRUMENTATION_FEASIBILITY,
                ],
                tool_allowlist=list(ToolName),
                token_budget=config.max_pack_tokens,
            )

        def fits(pack: CandidatePack) -> bool:
            text = serialize_pack(pack)
            return len(text.encode("utf-8")) <= config.max_pack_bytes and (
                counter is None or counter(text) <= pack.token_budget
            )

        pack = pack_for(required)
        if not fits(pack):
            # Mandatory data is irreducible: never drop caveats or contrary
            # evidence to make it fit. Either fail the build, or leave this one
            # candidate unjudged and say why in ``unpacked``.
            if not skip_oversize:
                raise EvidenceBudgetError(
                    "mandatory evidence exceeds the configured pack budget"
                )
            unpacked[candidate.candidate_id] = BudgetReason.EVIDENCE_TOO_LARGE
            continue
        selected = required
        for ref in (*candidate.peer_fact_ids, *candidate.daily_fact_ids):
            proposal = selected | _closure([ref], facts)
            proposed_pack = pack_for(proposal)
            if fits(proposed_pack):
                selected, pack = proposal, proposed_pack
        packs[candidate.candidate_id] = pack
    return EvidenceBundle(
        run_namespace=ns,
        facts=registry,
        assumptions=assumption_registry,
        scenarios=scenario_registry,
        segments=MappingProxyType(segments),
        periods=MappingProxyType(periods),
        candidates=MappingProxyType({c.candidate_id: c for c in enriched}),
        overlap_groups=MappingProxyType(dict(detected.overlap_groups)),
        quality=quality,
        packs=MappingProxyType(packs),
        unpacked=MappingProxyType(unpacked),
    )
