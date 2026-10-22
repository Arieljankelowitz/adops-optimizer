# Architecture

The tool turns a daily ad-performance CSV into a short report of opportunities
worth testing. Streamlit (`app.py`) and the CLI (`cli.py`) share the same runner
(`runner.py`). Neither front end implements its own analysis.

```text
1. Daily CSV
      ↓
2. Python: check quality, measure, find candidate opportunities
      ↓
3. Self-hosted LLM: judge evidence, explore with tools, prioritize tests
      ↓
4. Code: fact-check decisions → short HTML / Markdown report
```

## The four stages

1. **Data in.** `pipeline/load.py` parses the CSV without silently replacing
   missing values with zero. Quality checks flag issues such as incomplete days,
   suspicious counters and inconsistent money. Fatal issues stop recommendations.
2. **Code measures.** `pipeline/` computes totals, traffic-weighted rates,
   daily trends and comparisons across observed segments. Detectors suggest
   candidates such as wasted requests, margin leaks and promising slices.
   Code also calculates conditional estimates under explicitly labelled test
   assumptions; an estimate is not a prediction.
3. **AI judges and explores.** The self-hosted model receives compact evidence,
   not raw CSV rows. It can accept or reject candidates, consider contrary
   evidence, choose a controlled test and rank the results. Bounded tools
   (`breakdown`, `daily_trend`, `compare_to_peers`) let it investigate other
   patterns rather than only repeat detector rules. Tool handlers run Python;
   the model cannot execute arbitrary code. Accepted discovery findings are
   marked **found by AI**.
4. **Fact-checked report.** Validation checks the model's decisions against the
   current run's evidence. `report/summary.py` renders a short account summary,
   caveats, recommendation cards and charts, with HTML and Markdown downloads.
   Details and the full audit trail remain available separately.

## Why split the work this way?

**Code does arithmetic; the model does judgment.** Totals, rates and estimates
need reproducible definitions, not language-model arithmetic. Judgment is useful
for weighing competing signals, asking follow-up questions and deciding which
test is worth attention. The model selects evidence and structured explanation
codes; code turns those choices into readable report wording.

Each measured value has an evidence ID. `llm/validator.py` checks that references
exist and match the segment, period, metric, unit and intended role. Invalid
answers get up to two bounded repair attempts or are omitted. The renderer inserts numbers
from those measured objects, not model-written numerical prose, and audits the
rendered output. This keeps reported numbers traceable; it does **not** establish
that the model's interpretation is correct or that a proposed test will work.

Without a model, the same pipeline produces a clearly labelled deterministic-only
diagnostic draft. A cached demo is labelled as previous output, not new inference.

## Deliberately out of scope

- Automatic bidding, traffic allocation or production account changes.
- Causal attribution or guaranteed uplift from observational daily aggregates.
- Auction-level floor-response forecasts, hourly completeness checks and costs
  absent from the export.
- A production scheduling or multi-tenant service; this is an interactive analysis
  tool, not an autonomous campaign manager.

See [MODEL.md](MODEL.md) for serving details and the [README](../README.md) for
synthetic quick starts.
