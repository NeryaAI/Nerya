# Expert Investor Committee Playbook

Use this playbook only after loading `../SKILL.md`. Load
`investor-lenses.md` for persona knowledge and citations.

## 1. Intake

Record:

- asset, portfolio, or capital-allocation decision;
- instrument and venue;
- time horizon;
- user objective and maximum tolerable loss;
- evidence cutoff and missing data;
- whether the request is research, a paper proposal, or a live action.

Gather current facts before applying a persona. Framework sources explain how
to reason; they do not establish today's market price, filings, liquidity, or
positioning.

## 2. Route the Lenses

Select the smallest useful set:

- Buffett for owner economics, durability, and capital allocation.
- Damodaran for intrinsic value and narrative consistency.
- Marks for cycle position, price, leverage, and downside.
- Mauboussin for market-implied expectations, base rates, and process.
- Druckenmiller for tactical macro, liquidity, expression, and invalidation.

Use one lens for a focused question, two for a challenge, and three for a real
committee. Run all five only when the user explicitly requests the full panel.

## 3. Research Before Judgment

For a fact-dependent question:

1. Fetch primary filings, company disclosures, prices, rates, instrument
   specifications, and portfolio state with the appropriate Nerya skills.
2. Build a fact register before invoking any lens. Give every material fact a
   stable `fact_id` and record its claim, kind (`reported_fact`,
   `normalized_estimate`, or `disputed_claim`), exact source, publication date
   when available, `as_of`, `retrieved_at`, and calculation inputs.
3. Use a framework source ID such as `B1`, `D3`, or `H2` only for an
   attributed framework claim, never for a current fact.
4. Require every lens to cite the `fact_id` values it used separately from the
   persona source IDs supporting its reasoning pattern.
5. If a material fact cannot be verified, set the decision to `watch`, set the
   rating to `Watchlist`, withhold position guidance, and list the exact missing
   evidence. Do not fill gaps from memory.

For a pure framework question, current research may be unnecessary. Cite the
persona source IDs that support the framework and label every application to a
new case as `framework inference`.

## 4. Run Each Lens Independently

Each lens returns:

```json
{
  "lens": "marks",
  "diagnosis": "...",
  "facts_used": ["fact_id"],
  "framework_inferences": ["..."],
  "decision_implication": "...",
  "invalidation": ["..."],
  "failure_modes": ["..."],
  "source_ids": ["H2", "H6"],
  "confidence": 0.0
}
```

Do not let one lens see another lens's conclusion before producing its own.
This prevents polite convergence.

## 5. Synthesize Disagreement

Build a compact disagreement table:

| Lens | Conclusion | Governing assumption | Resolving evidence |
|---|---|---|---|

Do not average incompatible horizons. A Buffett-style ten-year owner thesis and
a Druckenmiller-style three-month liquidity thesis can both be coherent. State
which decision each one addresses.

## 6. Decision and Final Memo

Before returning a recommendation:

1. Assign one explicit horizon to each decision. Return separate decisions for
   incompatible horizons instead of averaging them.
2. Populate position guidance only when the evidence includes portfolio NAV,
   maximum tolerable loss, current exposures and material correlations,
   instrument liquidity, expected slippage, and a price-linked invalidation.
3. If any sizing prerequisite is missing, keep the analytical rating but set
   `position_guidance.status` to `withheld` and enumerate the missing inputs.
4. End a live request only as a `live_proposal`; it remains subject to the Risk
   Gate and Approval Gate and must not place an order.

Return:

```json
{
  "asset": "symbol or portfolio",
  "decision": "research|watch|paper_proposal|live_proposal",
  "horizon": "...",
  "as_of": "ISO-8601",
  "lenses_used": ["damodaran", "marks"],
  "facts": [
    {
      "fact_id": "F1",
      "claim": "...",
      "kind": "reported_fact|normalized_estimate|disputed_claim",
      "source": "exact URL or document identifier",
      "published_at": "ISO-8601 or null",
      "as_of": "ISO-8601",
      "retrieved_at": "ISO-8601",
      "calculation_inputs": []
    }
  ],
  "lens_findings": [],
  "disagreements": [
    {
      "lenses": ["..."],
      "governing_assumption": "...",
      "resolving_evidence": "...",
      "decision_if_confirmed": "...",
      "decision_if_rejected": "..."
    }
  ],
  "base_case": "...",
  "adverse_case": "...",
  "tail_case": "...",
  "rating": "Buy|Overweight|Hold|Underweight|Sell|Watchlist",
  "position_guidance": {
    "status": "withheld|research_only|paper_only|eligible_for_live_proposal",
    "range": null,
    "basis": "...",
    "maximum_modeled_loss": null,
    "required_inputs_missing": []
  },
  "invalidation": ["observable condition and resulting action"],
  "data_gaps": ["..."],
  "framework_citations": [
    {"source_id": "D1", "supports": "the attributed framework claim only"}
  ],
  "confidence": 0.0
}
```

## 7. Safety Boundary

- A recommendation is not an order.
- Keep paper and live state separate.
- Run portfolio and risk checks before proposing size.
- Require the Risk Gate and Approval Gate before live submission.
- Never convert an expert's historical concentration into a generic permission
  to use leverage.

## Attribution Discipline

- Use exact quotations only when directly verified in the cited primary source.
- Otherwise paraphrase and mark the application as `framework inference`.
- Never claim access to an expert's private reasoning or current holdings.
- Never infer a complete portfolio from Form 13F.
- Preserve documented errors and tensions; they are part of the model.
