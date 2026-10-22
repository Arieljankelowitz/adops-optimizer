"""Bounded judgment, repair and optional exploration through the self-hosted client."""

import json
import time
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from pydantic import TypeAdapter, ValidationError

from contracts.config import RunBudget
from contracts.enums import (
    AnalysisStatus,
    BudgetReason,
    Dimension,
    EligibilityStatus,
    MetricKey,
    NextStepKind,
    ToolName,
    ValidityStatus,
)
from contracts.ids import IdPrefix, canonical_json, make_id
from contracts.models import (
    BreakdownArgs,
    Candidate,
    CandidateAnalysis,
    CandidatePack,
    CandidateRecommendation,
    CompareToPeersArgs,
    DailyTrendArgs,
    ExploreStep,
    NextStep,
    RejectedAttempt,
    ToolCall,
)
from contracts.runtime import (
    ChatMessage,
    EvidenceBundle,
    ModelClient,
    ModelRequest,
    ToolRouter,
)
from llm.client import (
    ModelBudgetError,
    ModelClientError,
    ModelOutputError,
    ModelToolProtocolError,
)
from llm.answer import (
    REPAIR_INSTRUCTION,
    exception_issues,
    explore_schema,
    final_schema,
    output_issues,
    repair_errors,
    step_schema,
)
from llm.prompts import EXPLORE_PROMPT, build_candidate_messages, tool_view
from llm.tokens import estimate_prompt_tokens
from llm.tools import extend_bundle, peer_definition
from pipeline.evidence import (
    answer_guide,
    issue_fact_ids,
    observation_options,
    scoped_issues,
    split_issues,
)

# Room kept free after a tool result for the next turn's instruction.
TURN_RESERVE_TOKENS = 256
# Corrections per candidate; the validator refuses an analysis claiming more.
MAX_REPAIRS = 2


@dataclass
class ReasoningStats:
    started: float = field(default_factory=time.monotonic)
    generations: int = 0
    invalid_outputs: int = 0
    candidate_ids: set[str] = field(default_factory=set)
    ranking_calls: int = 0
    incomplete_candidates: list[str] = field(default_factory=list)
    explore_status: str = "disabled"


def reasoning_stats(client: ModelClient) -> ReasoningStats:
    """One client belongs to one run, matching the transport's lifecycle."""
    state = getattr(client, "_reasoning_stats", None)
    if state is None:
        state = ReasoningStats()
        setattr(client, "_reasoning_stats", state)
    return state


def prompt_limit(client) -> int:
    """Largest prompt the profile allows: its context window minus the output reserve."""
    profile = getattr(client, "profile", None)
    return getattr(profile, "max_prompt_tokens", 12288)


def count_prompt(client, messages, tools=()) -> int:
    """Tokens of a request: the deployed tokenizer's count when the client has one.

    Never bytes: a client without a counter gets the calibrated estimate, which
    errs high, so an unknown tokenizer cannot overrun the context.
    """
    counter = getattr(client, "count_prompt_tokens", None)
    if counter is not None:
        return counter(messages, list(tools))
    return estimate_prompt_tokens(messages, tools)


def generate(client, budget, messages, model, name, *, tools=(), schema=None):
    """No transport retries here: llm.client owns those and its independent cap.

    ``schema`` narrows ``model``'s own schema for this request (see llm.answer).
    Raises ``ModelBudgetError`` carrying which limit was hit, before any request.
    """
    stats = reasoning_stats(client)
    # 15 candidates x (4 turns + 2 repairs) + ranking and its repair, plus at
    # most 4 discovery turns and 3 x 6 judgments of the proposals.
    generation_cap = 92 + (22 if budget.explore_enabled else 0)
    if stats.generations >= min(generation_cap, budget.max_network_attempts, 100):
        raise ModelBudgetError(
            "generation_budget_exhausted", BudgetReason.GENERATION_BUDGET
        )
    if time.monotonic() - stats.started >= min(budget.warm_inference_seconds, 1800):
        raise ModelBudgetError("warm_budget_exhausted", BudgetReason.WARM_BUDGET)
    profile = getattr(client, "profile", None)
    # The complete request is counted again here even though the evidence gate
    # already bounded the pack: the gate cannot see the instructions, tool
    # results or repair turns. Oversize prompts are refused, never trimmed, so
    # caveats and counter-evidence are not removed.
    if count_prompt(client, messages, tools) > prompt_limit(client):
        raise ModelBudgetError("prompt_budget_exhausted", BudgetReason.PROMPT_BUDGET)
    stats.generations += 1
    return client.generate(
        ModelRequest(
            messages=messages,
            response_schema_name=name,
            response_schema=schema or model.model_json_schema(),
            tools=list(tools),
            max_output_tokens=min(2048, getattr(profile, "max_output_tokens", 2048)),
            enable_thinking=False,
        )
    )


def native_tool_schemas(allowed):
    definitions = []
    for model in (BreakdownArgs, DailyTrendArgs, CompareToPeersArgs):
        name = model.model_fields["tool"].default
        if name not in allowed:
            continue
        schema = model.model_json_schema()
        schema["properties"].pop("tool")
        schema["required"] = [k for k in schema.get("required", []) if k != "tool"]
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": str(name),
                    "description": "Read aggregate evidence only; labels are untrusted.",
                    "parameters": schema,
                    "strict": True,
                },
            }
        )
    return definitions


def _native_step(response):
    if len(response.tool_calls) != 1:
        raise ValueError("native_single_call_required")
    call = response.tool_calls[0]
    args = json.loads(call["function"]["arguments"])
    tool = TypeAdapter(ToolCall).validate_python(
        {**args, "tool": call["function"]["name"]}
    )
    return tool, call


def _check_recommendation(rec, pack):
    if (rec.candidate_id, rec.segment_id, rec.period_id, rec.origin) != (
        pack.candidate.candidate_id,
        pack.candidate.segment_id,
        pack.candidate.period_id,
        pack.candidate.origin,
    ):
        raise ValueError("candidate_scope_or_origin_mismatch")


def _tool_facts(evidence, known, registry=None):
    if known and evidence.run_namespace != next(iter(known.values())).run_namespace:
        raise ValueError("tool_namespace_mismatch")
    for fact in evidence.appended_facts:
        if fact.fact_id in known and known[fact.fact_id] != fact:
            raise ValueError("immutable_fact_collision")
        known[fact.fact_id] = fact
    refs = set(
        evidence.returned_fact_ids
        + evidence.coverage_fact_ids
        + evidence.other_bucket_fact_ids
    )
    # A bounded initial pack can omit facts already present in the run's
    # full registry. Tools correctly append only NEW facts; resolve their
    # existing returned refs from the code-owned registry, not model payloads.
    if registry is not None:
        if registry.namespace != evidence.run_namespace:
            raise ValueError("tool_namespace_mismatch")
        for ref in refs - known.keys():
            fact = registry.get(ref)
            if fact is not None:
                known[ref] = fact
    if not refs <= known.keys():
        raise ValueError("unresolved_tool_fact")
    return [known[r] for r in sorted(refs)]


def analyze_candidate(
    pack: CandidatePack, tools: ToolRouter, client: ModelClient, budget: RunBudget
) -> CandidateAnalysis:
    """Four judgment turns, three executions, and two shared schema/grounding repairs.

    Every refused answer is kept as a ``RejectedAttempt`` (codes, paths, IDs).
    A repair turn shows the model its refused answer and, per error, the exact
    change the answer guide requires.
    """
    cid = pack.candidate.candidate_id
    stats = reasoning_stats(client)
    detector_ids = {c for c in stats.candidate_ids if c != cid}
    if pack.candidate.origin == "detector" and len(detector_ids) >= min(
        budget.max_candidates, 15
    ):
        return CandidateAnalysis(
            candidate_id=cid,
            status=AnalysisStatus.BUDGET_EXHAUSTED,
            recommendation=None,
            tool_evidence=[],
            generation_turns=0,
            repairs=0,
            budget_reason=BudgetReason.CANDIDATE_CAP,
        )
    stats.candidate_ids.add(cid)
    messages = build_candidate_messages(pack)
    evidence, cache, known = [], {}, {f.fact_id: f for f in pack.facts}
    turns = repairs = executions = 0
    native = budget.native_tools_enabled
    repair_issues = None
    rejected: list[RejectedAttempt] = []
    # Set once a tool result cannot fit the prompt: judge on what the model has.
    force_final = False
    guide = pack.answer_guide
    # Statements the model may cite; tool results the model is shown add more.
    options = list(guide.observation_options) if guide is not None else []
    period = next(
        (p for p in pack.periods if p.period_id == pack.candidate.period_id), None
    )
    thresholds = {r for i in pack.quality_issues for r in i.threshold_fact_ids}
    tool_cap = min(budget.max_tool_calls_per_candidate, 3)

    def scopes():
        segments = {s.segment_id for s in pack.segments} | {
            f.segment.segment_id for f in known.values()
        }
        periods = {p.period_id for p in pack.periods} | {
            f.period.period_id for f in known.values()
        }
        return segments, periods

    def result(status, rec=None, reason=None):
        if status is not AnalysisStatus.COMPLETE:
            stats.incomplete_candidates.append(cid)
        return CandidateAnalysis(
            candidate_id=cid,
            status=status,
            recommendation=rec,
            tool_evidence=evidence,
            generation_turns=turns,
            repairs=repairs,
            budget_reason=reason
            if status is AnalysisStatus.BUDGET_EXHAUSTED
            else None,
            rejected_attempts=list(rejected),
        )

    def refuse(issues, answer=None):
        nonlocal repair_issues
        stats.invalid_outputs += 1
        rejected.append(RejectedAttempt(attempt=turns + repairs, errors=issues[:32]))
        # The model sees what it answered; a cut-off or unparsable answer is
        # not echoed back.
        if answer is not None:
            messages.append(ChatMessage(role="assistant", content=answer))
        repair_issues = issues

    while turns < min(budget.max_turns_per_candidate, 4) or repair_issues is not None:
        repair = repair_issues is not None
        final_only = (
            repair or force_final or turns + 1 >= min(budget.max_turns_per_candidate, 4)
        )
        if repair:
            if repairs >= min(budget.max_repairs_per_candidate, MAX_REPAIRS):
                return result(AnalysisStatus.REJECTED_INVALID)
            repairs += 1
            messages.append(
                ChatMessage(
                    role="user",
                    content=canonical_json(
                        {
                            "repair": REPAIR_INSTRUCTION,
                            "errors": repair_errors(repair_issues, pack),
                        }
                    ),
                )
            )
            repair_issues = None
        else:
            turns += 1
        messages.append(
            ChatMessage(
                role="user",
                content=canonical_json(
                    {
                        "response": "final_recommendation"
                        if final_only
                        else "next_step",
                        "remaining_tool_executions": max(0, tool_cap - executions),
                        "peer_definition_ids": [
                            peer_definition(pack.run_namespace, d).peer_definition_id
                            for d in Dimension
                        ],
                    }
                ),
            )
        )
        try:
            response = generate(
                client,
                budget,
                messages,
                CandidateRecommendation if final_only else NextStep,
                "candidate_final" if final_only else "candidate_next_step",
                tools=native_tool_schemas(
                    set(pack.tool_allowlist) & tools.allowed_tools
                )
                if native and not final_only
                else (),
                schema=final_schema(pack, options)
                if final_only
                else step_schema(pack, options, *scopes(), executions < tool_cap),
            )
            call = native_call = None
            if response.tool_calls:
                if final_only or not native:
                    raise ValueError("unexpected_native_tool")
                call, native_call = _native_step(response)
            elif final_only:
                rec = CandidateRecommendation.model_validate_json(response.content)
            else:
                step = NextStep.model_validate_json(response.content)
                if step.kind is NextStepKind.CALL_TOOL:
                    call = step.tool_call
                else:
                    rec = step.recommendation
            if call is not None:
                if (
                    call.tool not in pack.tool_allowlist
                    or call.tool not in tools.allowed_tools
                ):
                    raise ValueError("tool_not_allowed")
                allowed_segments, allowed_periods = scopes()
                if (
                    call.segment_id not in allowed_segments
                    or call.period_id not in allowed_periods
                ):
                    raise ValueError("tool_outside_candidate_scope")
                key = canonical_json(call.model_dump(mode="json"))
                if key not in cache:
                    if executions >= tool_cap:
                        return result(
                            AnalysisStatus.BUDGET_EXHAUSTED, reason=BudgetReason.TOOL_BUDGET
                        )
                    executions += 1
                    ev = tools.dispatch(cid, call)
                    if ev.run_namespace != pack.run_namespace or ev.tool_call != call:
                        raise ValueError("tool_response_mismatch")
                    cache[key] = ev
                    evidence.append(ev)
                ev = cache[key]
                tool_ctx = getattr(tools, "ctx", None)
                tool_bundle = getattr(tool_ctx, "bundle", None)
                facts = _tool_facts(
                    ev, known, tool_bundle.facts if tool_bundle is not None else None
                )
                view = tool_view(ev, facts)
                new_options = []
                if guide is not None and period is not None:
                    shown = {**known, **{f.fact_id: f for f in facts}}
                    new_options = [
                        o
                        for o in observation_options(
                            pack.candidate,
                            period,
                            shown,
                            guide.effective_restrictions,
                            thresholds,
                        )
                        if o not in options
                    ]
                    if new_options:
                        view["new_observation_options"] = [
                            o.model_dump(mode="json") for o in new_options
                        ]
                content = canonical_json(view)
                if native_call:
                    exchange = [
                        ChatMessage(
                            role="assistant", content="", tool_calls=[native_call]
                        ),
                        ChatMessage(
                            role="tool",
                            content=content,
                            tool_call_id=native_call["id"],
                        ),
                    ]
                else:
                    exchange = [
                        ChatMessage(role="assistant", content=response.content),
                        ChatMessage(role="user", content=content),
                    ]
                # Only a result the next turn can still carry goes into the
                # prompt. Otherwise the model gets one final-only turn on the
                # pack it already has; the execution stays in the audit trail.
                if (
                    count_prompt(client, messages + exchange)
                    + TURN_RESERVE_TOKENS
                    > prompt_limit(client)
                ):
                    force_final = True
                else:
                    messages.extend(exchange)
                    options.extend(new_options)
                continue
            _check_recommendation(rec, pack)
            completed = result(AnalysisStatus.COMPLETE, rec)
            # Runner supplies lane C's validator. Its rejected result gets the
            # very same repair allowance as malformed/schema-invalid output.
            validate = getattr(tools, "validate_analysis", None)
            if validate is not None:
                verdict = validate(completed)
                if not verdict.accepted:
                    refuse(list(verdict.errors), response.content or None)
                    continue
            return completed
        except ModelBudgetError as exc:
            return result(AnalysisStatus.BUDGET_EXHAUSTED, reason=exc.reason)
        except ModelToolProtocolError:
            if not native or repair:
                return result(AnalysisStatus.TRANSPORT_FAILED)
            native = False
        except ModelOutputError as exc:
            if native and not repair:
                stats.invalid_outputs += 1
                native = False
                continue
            refuse(output_issues(exc.errors))
        except (ValidationError, ValueError, TypeError, KeyError) as exc:
            refuse(exception_issues(exc))
        except ModelClientError:
            return result(AnalysisStatus.TRANSPORT_FAILED)
    return result(AnalysisStatus.BUDGET_EXHAUSTED, reason=BudgetReason.TURN_BUDGET)


@dataclass(frozen=True)
class ExploreResult:
    bundle: EvidenceBundle
    packs: tuple[CandidatePack, ...] = ()
    status: str = "disabled"


def explore_candidates(bundle, tools, client, budget, judged=()) -> ExploreResult:
    """Optional, separately bounded discovery. Failure never replaces rule results.

    Returns packs for the *ordinary* analyze/validate loop, never recommendations
    or validation exemptions. Only this stage's tool-returned facts may seed them.
    """
    stats = reasoning_stats(client)
    if (
        not budget.explore_enabled
        or budget.max_explore_proposals == 0
        or bundle.quality.has_fatal
    ):
        return ExploreResult(bundle)
    original = bundle
    stats.explore_status = "started"
    try:
        offered_segments = sorted(
            (s for s in bundle.segments.values() if s.cohort_id == "full_input"),
            key=lambda s: (len(s.filters), s.segment_id),
        )[:12]
        offered_periods = sorted(
            bundle.periods.values(),
            key=lambda p: (-len(p.included_dates), p.period_id),
        )[:3]
        messages = [
            ChatMessage(role="system", content=EXPLORE_PROMPT),
            ChatMessage(
                role="user",
                content=canonical_json(
                    {
                        "proposal_cap": min(budget.max_explore_proposals, 3),
                        "rule_judgments": [
                            {
                                "candidate_id": a.candidate_id,
                                "status": a.status,
                                "decision": a.recommendation.decision
                                if a.recommendation
                                else None,
                            }
                            for a in judged
                        ],
                        "judged_candidates": [
                            {
                                "candidate_id": c.candidate_id,
                                "family": c.family,
                                "segment_id": c.segment_id,
                                "period_id": c.period_id,
                            }
                            for c in bundle.candidates.values()
                        ],
                        "segments": [s.model_dump(mode="json") for s in offered_segments],
                        "periods": [p.model_dump(mode="json") for p in offered_periods],
                    }
                ),
            ),
        ]
        known, returned, executions, cache = {}, set(), 0, {}
        proposals = []
        # Set once a tool result cannot fit the prompt: discovery ends with what
        # the model has actually been shown.
        force_final = False
        for turn in range(min(budget.max_explore_turns, 4)):
            final = turn + 1 == min(budget.max_explore_turns, 4) or force_final
            messages.append(
                ChatMessage(
                    role="user",
                    content=canonical_json(
                        {
                            "final_only": final,
                            "remaining_tool_executions": max(
                                0, min(budget.max_explore_tool_calls, 3) - executions
                            ),
                        }
                    ),
                )
            )
            response = generate(
                client,
                budget,
                messages,
                ExploreStep,
                "explore_next_step",
                schema=explore_schema(
                    bundle.run_namespace,
                    [known[r] for r in sorted(returned)],
                    {s.segment_id for s in offered_segments}
                    | {f.segment.segment_id for f in known.values()},
                    {p.period_id for p in offered_periods}
                    | {f.period.period_id for f in known.values()},
                    final
                    or executions >= min(budget.max_explore_tool_calls, 3),
                ),
            )
            step = ExploreStep.model_validate_json(response.content)
            if step.kind == "final":
                if step.tool_call is not None:
                    raise ValueError("explore_mixed_payload")
                proposals = step.proposals[: min(budget.max_explore_proposals, 3)]
                break
            if final or step.tool_call is None or step.proposals:
                raise ValueError("explore_invalid_tool_turn")
            call = step.tool_call
            key = canonical_json(call.model_dump(mode="json"))
            if call.tool not in tools.allowed_tools:
                raise ValueError("explore_tool_not_allowed")
            if key not in cache:
                if executions >= min(budget.max_explore_tool_calls, 3):
                    raise ValueError("explore_tool_budget")
                executions += 1
                # Discovery has no candidate yet; runner provides a namespace-
                # checked dispatch_explore, not a fake detector candidate ID.
                ev = tools.dispatch_explore(call)
                if ev.tool_call != call:
                    raise ValueError("explore_tool_response_mismatch")
                bundle = extend_bundle(bundle, ev)
                cache[key] = ev
            ev = cache[key]
            seen = dict(known)
            for ref in (
                ev.returned_fact_ids + ev.coverage_fact_ids + ev.other_bucket_fact_ids
            ):
                seen[ref] = bundle.facts[ref]
            result_message = ChatMessage(
                role="user",
                content=canonical_json(tool_view(ev, [seen[r] for r in sorted(seen)])),
            )
            # A result the model is not shown can neither be cited nor seed a pack.
            if (
                count_prompt(client, messages + [result_message]) + TURN_RESERVE_TOKENS
                > prompt_limit(client)
            ):
                force_final = True
                continue
            known = seen
            returned.update(ev.returned_fact_ids)
            messages.append(result_message)
        packs = []
        candidates, pack_map = dict(bundle.candidates), dict(bundle.packs)
        # Same budget as the run's rule packs; the request gate still counts.
        token_budget = max(
            (p.token_budget for p in bundle.packs.values()),
            default=prompt_limit(client) // 2,
        )
        for proposal in proposals:
            # An unsupported proposal is dropped on its own; the others still
            # go to the ordinary judgment and strict validation.
            if not set(proposal.fact_ids) <= returned:
                continue
            facts = [bundle.facts[r] for r in proposal.fact_ids]
            if any(
                (f.segment.segment_id, f.period.period_id, f.segment.cohort_id)
                != (proposal.segment_id, proposal.period_id, "full_input")
                for f in facts
            ):
                continue
            if any(
                c.segment_id == proposal.segment_id
                and c.period_id == proposal.period_id
                and c.family == proposal.family
                for c in candidates.values()
            ):
                continue
            cid = make_id(
                IdPrefix.CANDIDATE,
                bundle.run_namespace,
                {"origin": "model_proposed", **proposal.model_dump(mode="json")},
            )
            target_facts = [
                f for f in known.values() if f.segment.segment_id == proposal.segment_id
            ]
            contrary = sorted(
                f.fact_id
                for f in target_facts
                if f.validity.status is not ValidityStatus.VALID
                or f.metric
                in (
                    MetricKey.OBSERVED_DAYS,
                    MetricKey.REVENUE_POSITIVE_DAYS,
                    MetricKey.MAX_DAY_REVENUE_SHARE,
                    MetricKey.TOP_TWO_DAY_REVENUE_SHARE,
                    MetricKey.MAX_DAY_PROFIT_SHARE,
                    MetricKey.TOP_TWO_DAY_PROFIT_SHARE,
                )
            )
            if len(contrary) > 12:
                continue
            segment = bundle.segments[proposal.segment_id]
            period = bundle.periods[proposal.period_id]
            issues = scoped_issues(
                bundle.quality.issues,
                bundle.segments,
                bundle.periods,
                proposal.segment_id,
                proposal.period_id,
            )
            candidate = Candidate(
                origin="model_proposed",
                candidate_id=cid,
                family=proposal.family,
                segment_id=proposal.segment_id,
                period_id=proposal.period_id,
                eligibility=EligibilityStatus.EXPLORATORY,
                trigger_fact_ids=proposal.fact_ids,
                daily_fact_ids=[
                    f.fact_id for f in target_facts if len(f.period.included_dates) == 1
                ],
                peer_fact_ids=[
                    f.fact_id for f in target_facts if f.comparison is not None
                ],
                counter_evidence_fact_ids=contrary,
                quality_issue_ids=[q.issue_id for q in issues],
                overlap_group_ids=[],
            )
            # The same issue shrink and answer guide as a rule candidate's pack.
            detailed, summarised = split_issues(
                candidate, segment, issues, bundle.segments
            )
            pack_facts = dict(known)
            for ref in issue_fact_ids(detailed):
                pack_facts[ref] = bundle.facts[ref]
            segment_ids = {f.segment.segment_id for f in pack_facts.values()}
            segment_ids.add(proposal.segment_id)
            period_ids = {f.period.period_id for f in pack_facts.values()}
            period_ids.add(proposal.period_id)
            for issue in detailed:
                segment_ids.update(issue.segment_ids)
                period_ids.update(issue.period_ids)
            # Keep the complete discovery evidence, including contrary facts;
            # no assumptions/scenarios are minted by the model.
            pack = CandidatePack(
                run_namespace=bundle.run_namespace,
                candidate=candidate,
                quality_issues=detailed,
                segments=[
                    bundle.segments[r] for r in sorted(segment_ids) if r in bundle.segments
                ],
                periods=[
                    bundle.periods[r] for r in sorted(period_ids) if r in bundle.periods
                ],
                facts=list(pack_facts.values()),
                assumptions=[],
                scenarios=[],
                permitted_test_templates=[],
                tool_allowlist=list(ToolName),
                token_budget=token_budget,
                answer_guide=answer_guide(
                    candidate, segment, period, issues, summarised, pack_facts, [], [], []
                ),
            )
            candidates[cid], pack_map[cid] = candidate, pack
            packs.append(pack)
        bundle = replace(
            bundle,
            candidates=MappingProxyType(candidates),
            packs=MappingProxyType(pack_map),
        )
        stats.explore_status = "complete"
        return ExploreResult(bundle, tuple(packs), "complete")
    except Exception:
        # Private evidence and arbitrary model text never enter error logs.
        stats.explore_status = "failed_optional"
        return ExploreResult(original, status="failed_optional")
