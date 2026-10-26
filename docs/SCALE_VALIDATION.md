# Full-size synthetic validation

Only independently generated synthetic data was used. No private CSV was opened.
`scripts/generate_fullsize.py` uses seed `20310417`: 115,933 rows, seven days,
140 supply IDs, 45 countries, 14 sizes, two placement types and three device types
(plus deliberately planted label and accounting problems). Sizes are approximate;
intermittent grains do not appear every day. CSVs are gitignored.

## Fixes and deliberate simplifications

- **Overlap contract:** the model returns an empty overlap list. Code attaches
  every group of the candidate before validation. The persisted recommendation
  contract no longer caps that list at eight; exported schemas are regenerated.
  Ranking still rejects shared groups and geometrically overlapping scopes.
- **Invalid breakdowns:** each slice's free dimensions are enumerated in the
  response schema, with its candidate dimensions listed in the prompt. Fully fixed
  slices cannot request breakdowns. A deliberately invalid scripted call still
  produces `schema_invalid` at `/tool_call`, with
  `tool refused: breakdown_dimension_already_fixed`, then can recover.
- **Evidence size:** bookkeeping IDs become counts in prompts. Relevant quality
  issues are bounded per rule; each detailed issue shows at most four affected
  slices and their measurements. Other issues are counted with their restrictions
  intact. Daily/peer additions must fit the pack budget; full facts, issues and
  lineage remain in the audit bundle and strict validator.
- **Deterministic runtime:** scan observed slices, but on exports above 2,000
  full-period slices build expensive evidence for only the largest 40 hits by
  request exposure (eligible first), before quality refinement/overlap generation.
  Judge ten candidates rather than fifteen when the evidence store exceeds 2,000
  segments. This is intentionally not exhaustive discovery. Parent quality scope
  lookup is indexed; metric lookups and issue intersections are cached/indexed.
- **Inference runtime:** large exports allow one drill-down then a final judgment;
  genuinely new validation errors still get repairs. Repeating the same grounding
  error stops further wasted repairs. Optional explore remains enabled and checked.
- **Final selection:** large exports no longer spend model calls on final ranking.
  Code orders already-validated judgments by measured request exposure and selects
  disjoint scopes. The ordinary ranking checker AND strict validator still apply.
  No predicted uplift, merged evidence or caveats are invented. Reports explicitly
  label this code-only selection; unselected judgments remain in the audit.

## Recorded results

The task's external proof directory contains generated CSVs, full audit bundles,
progress timings and JSON summaries; none is committed here.

| Measurement | Scripted model, final implementation | Sole live run |
| --- | --- | --- |
| Synthetic rows / days | 115,933 / 7 | 115,933 / 7 |
| Overall status | `final` | `partial` (final model-ranking failure only) |
| Candidate analyses | 11 complete | 10 complete |
| Validated | 11 (10 rule + 1 explore) | 10 (7 tests + 3 investigations) |
| Explore proposals accepted into judgment | 1 | 0 (explore completed) |
| Candidate repairs / rejected attempts | 0 / 0 | 0 / 0 |
| `evidence_too_large` skips | 0 | 0 |
| Turns / tools | 22 / 11 | 40 / 30 |
| Model generations / ranking calls | 24 / 0 | 43 / 2 |
| End-to-end wall time | 117.34 seconds | 1,227.61 seconds (20m28s) |

The single authorized live run used the self-hosted endpoint on 2026-10-26.
It measured the candidate-contract/evidence/shortlist fixes **before** the final
one-tool and code-selection simplification. It spent about 95 seconds in
analytics, 166 seconds waiting for cold readiness, and 171 seconds in unsuccessful
model ranking. All ten recommendations passed strict validation with no repairs,
no tool refusals and no oversized packs. Its status is not reported as `final`.

After that observation, redundant tool rounds and model ranking were removed for
large exports. No second live run was made. Offline replay of the ten saved live
recommendations through the final code-only selector AND strict validator passed;
three disjoint recommendations were selected, with all ten retained in the audit.
The final tool/selection policy was also exercised end to end by the full-size
scripted run. Its live wall time has **not** been remeasured; no faster live result
is claimed.

## Reproduce offline

```sh
uv run python scripts/generate_fullsize.py --out /tmp/fullsize-synthetic.csv
uv run python scripts/benchmark_fullsize.py --out /tmp/adops-scale
uv run pytest -m 'not live'
```

The regression suite replays the old eight-group contract inside the real analysis
loop with a scripted response, reproducing `schema_invalid`, `/overlap_group_ids`,
`contract check failed: too_long`. It also verifies fixed-dimension refusal and
recovery, per-slice schema enums, bounded prompt bookkeeping, non-additive final
selection, and a full-size end-to-end scripted run with no skips or repairs.

`--live` on the benchmark is explicit opt-in and requires runtime environment
credentials. Do not use it as part of ordinary offline tests. The benchmark accepts
no input path: it always generates the file it reads.
