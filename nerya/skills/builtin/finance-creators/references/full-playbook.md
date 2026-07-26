# Full Playbook

## Purpose

Use finance creators as explicitly bounded lenses over verified evidence. The
skill is for asking better questions and recognizing framing patterns. It is
not a market-data feed, a performance endorsement, or a substitute for primary
filings, exchange data, portfolio context, Risk Gate, or Approval Gate.

## Contents

- Lens Selection
- Evidence Ledger
- Research Protocol
- Lens-Specific Questions
- Comparison Protocol
- Output Contract

## Evidence Ledger

Maintain these layers separately:

| Label | Meaning | Minimum support |
|---|---|---|
| `current fact` | A present company, market, filing, or macro datum | Primary or high-quality source, stable ID, and `as_of` |
| `creator statement` | What the creator actually published | Direct URL, date, and quotation or close paraphrase |
| `framework inference` | A new application of the distilled lens | Named model, reasoning chain, uncertainty, and invalidation |
| `commercial context` | A product, subscription, affiliation, gift, position, or sponsor | Creator disclosure or reliable external evidence |
| `entertainment framing` | Urgency, humor, outrage, or narrative packaging | Never promoted into evidence |

Never allow a creator statement to silently become a current fact. Never infer
accuracy from audience size. Preserve deleted, amended, contradictory, or
unverifiable calls as uncertainty rather than repairing the record.

## Research Protocol

1. State the decision, instrument, horizon, and evidence cutoff.
2. Retrieve current prices, filings, releases, disclosures, and market data
   from their authoritative sources. Record lag and revision risk.
3. Retrieve the relevant creator material directly. Prefer long-form work and
   complete threads over screenshots, reposts, summaries, or quote cards.
4. Record the creator's commercial incentives and disclosed positions when
   relevant. Absence of a disclosure is not evidence of absence.
5. Apply the selected lens only after the evidence ledger is complete.
6. Search for the strongest contrary evidence and a plausible base-rate view.
7. State what would falsify the inference and what must be checked next.

## Lens-Specific Questions

### Serenity

- Which supplier, customer, product generation, or bottleneck is the claim
  about, and is the relationship verified?
- Is evidence a public source, management statement, named channel, anonymous
  channel check, or inference? Do not merge those grades.
- Is a technology transition commercially shipping, qualifying, sampling, or
  only on a roadmap? What is the adoption and substitution path?
- Could distribution conflict, customer concentration, inventory, capacity,
  or timing reverse the thesis?

### Unusual Whales

- Is the observation options flow, open interest, volume, gamma exposure,
  dark-pool prints, or a delayed public disclosure?
- Can the trade be opening or closing, hedged, spread-linked, customer flow,
  or market-maker inventory? Direction is rarely identified from one print.
- Are size, premium, strike, tenor, liquidity, and event calendar material?
- For public-official trades, verify the original filing, reporting range,
  transaction date, filing date, owner, and amendment status.

### The Kobeissi Letter

- What changed relative to consensus, the previous release, and revisions?
- Which asset moved first, which confirmed, and which diverged?
- Is the post describing a fact, interpreting a regime, or marketing a tactical
  call? Keep those layers separate.
- What is the event path across rates, dollar, equities, credit, commodities,
  and volatility? What would make the initial reaction fade?

## Comparison Protocol

Run each lens in isolation, then compare:

| Field | Required content |
|---|---|
| Horizon | Intraday event, tactical weeks, or industry cycle |
| Governing assumption | The one claim that must hold |
| Best evidence | Stable IDs from the evidence ledger |
| Contrary evidence | Strongest disconfirming source or base rate |
| Incentive risk | Product, position, access, or audience incentives |
| Invalidation | Observable condition and review date |
| Confidence | Low, medium, or high with a reason |

Do not vote or average. Resolve disagreement by naming the assumption and the
future evidence that would discriminate among lenses.

## Output Contract

Return:

1. `Decision and cutoff`
2. `Verified evidence ledger`
3. `Selected creator lens and why`
4. `Framework inference`
5. `Contrary case and incentive conflicts`
6. `Invalidation, next checks, confidence, and risk limits`

For content review, also mark unsupported certainty, selective time windows,
missing denominators, post-hoc narration, engagement bait, and undisclosed or
unclear commercial context. For action-oriented work, stop at `Watchlist` when
critical evidence is missing. Paper proposals must remain explicitly paper;
live actions require the normal Risk Gate and Approval Gate.
