<!-- nerya-skill-frontmatter-start -->
---
name: finance-creators
description: "Hub for the distilled finance-creator lenses. Use only when the user explicitly asks for Serenity / @aleabitoreddit, Unusual Whales / @unusual_whales, or The Kobeissi Letter / @KobeissiLetter as a lens or comparison. Load one creator sub-skill (finance-creators.serenity / .unusual_whales / .kobeissi) instead of all three — each lens lives in its own sub-skill to keep context small."
version: 0.2.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Finance Creators (Hub)

Apply social-first finance frameworks as a research overlay. Treat reach as a
distribution fact, never as evidence of accuracy or investability.

This hub is an index. Each creator lens is a separate sub-skill so loading
one lens does not pull the other two into context.

## Lens Router

| Decision | Primary lens | Useful challenger | Load |
|---|---|---|---|
| AI infrastructure, semiconductors, networking, or supplier transitions | Serenity | Kobeissi for macro timing | `skill_view("finance-creators.serenity")` |
| Options activity, gamma exposure, dark pools, or public-official trading | Unusual Whales | Kobeissi for the event path | `skill_view("finance-creators.unusual_whales")` |
| Macro releases, policy, commodities, or cross-asset stress | The Kobeissi Letter | Serenity for industry evidence | `skill_view("finance-creators.kobeissi")` |

Use one lens by default and no more than two unless requested. Audience size
is not a selection criterion.

## Activation Flow

1. CLASSIFY the request as `pure framework`, `fact-dependent`,
   `content review`, or `action-oriented`, then ROUTE with the table
   above. For a single-lens question, load that one sub-skill inline.
2. For two or more lenses, do NOT `skill_view` the lens sub-skills into
   your own context — dispatch one subagent lane per creator with
   ``team_run`` using the matching roles (`serenity_lens`,
   `unusual_whales_lens`, `kobeissi_lens`); never seat a generic
   analyst lane to simulate a named creator. For every `fact-dependent`
   or `action-oriented` request, load the evidence-labeling rules via
   ``skill_view("finance-creators", file="references/full-playbook.md")``
   (builtin assets are not reachable with read_file).
3. For fact-dependent or action-oriented work, DEFINE the instrument,
   decision, horizon, and evidence cutoff. GATHER current facts with
   `market_research`, `equity_research`, `sec_filings`, `markets`, or
   `trading`. Give each material fact a source, `as_of` date, and stable ID.
   Label unverified user-supplied data `unverified user input`; never attach
   a creator source ID to it or use it alone for position guidance.
4. LABEL every material statement as `current fact`, `creator statement`,
   `framework inference`, `commercial context`, or `entertainment framing`.
5. RUN lenses independently and preserve disagreement; do not average
   conclusions or vote.

## Disagreement Matrix

| Tension | Route by |
|---|---|
| Serenity's private industry evidence vs. Unusual Whales' observable flow | Evidence grade, horizon, and reproducibility |
| Company transition thesis vs. Kobeissi macro reaction | Product-cycle horizon and financing or demand sensitivity |
| Options anomaly vs. macro catalyst | Contract structure, event timing, and alternative hedging explanations |

## Shared Rules (apply to every lens)

- Use reasoning patterns, not first-person impersonation or endorsement;
  applying a method to a new asset must be labelled `framework inference`.
- Never use followers, engagement, urgency, or virality as an expertise
  score.
- Creator source IDs support methods and past statements, not current
  prices, holdings, disclosures, flows, fundamentals, or macro releases.
- Surface subscriptions, products, affiliations, gifts, sponsorships, and
  position disclosures when they could shape the framing.
- Never fill a current-data gap from model memory; if critical evidence is
  missing, stop at `Watchlist`.
- A recommendation remains a proposal, not an order: Risk Gate and Approval
  Gate always apply.
- Deep provenance lives under `references/research/`; the research cutoff
  is 2026-07-27.
