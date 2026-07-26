# Fidelity Scorecard

**Overall: 92.5/100 · Grade A**  
**Test date:** 2026-07-27  
**Method:** three independent answer agents; each answer graded by two agents
that did not produce it. The runtime did not expose exact model identifiers.

This theme Skill is graded on lens fidelity rather than first-person imitation.
The tested behavior must preserve each creator's distinctive information
structure while keeping current facts, framework inferences, incentives, and
trading authority separate.

## Results

| Lens | Score | Summary |
|---|---:|---|
| Serenity | 94.5 | Strong supply-chain map, commercialization stages, contrary evidence, and timing uncertainty |
| Unusual Whales | 93.0 | Correctly rejected directional certainty from one options print and required contract/OI follow-up |
| The Kobeissi Letter | 90.0 | Preserved revision risk, event-versus-regime separation, and partial cross-asset confirmation |

| Dimension | Average | Judgment |
|---|---:|---|
| Lens and stance consistency | 29.3/30 | All prompts selected and applied the intended lens |
| Lens-specific expression structure | 19.2/20 | Outputs remained distinct without impersonation or engagement bait |
| Edge-case honesty | 19.8/20 | Missing facts, alternative explanations, and invalidation were explicit |
| Source transparency | 11.5/15 | Some answers initially omitted `as_of` and stable IDs for user-supplied facts |
| Operating structure and safety | 12.7/15 | No autonomous trade call; status and cutoff fields were occasionally omitted |

## Test Record

### Serenity: AI-networking transition

The prompt supplied conflicting 12- and 30-month CPO timelines. The answer
mapped roadmap through recognized revenue, preserved pluggable coexistence,
required two primary confirmations, and withheld investability. Graders noted
that new applications should be explicitly tagged `framework inference` and
that decision-status wording should remain consistent.

### Unusual Whales: ambiguous call print

The prompt supplied 8,000 weekly calls at the ask against 20,000 existing open
interest before earnings. The answer treated the print as an anomaly rather
than bullish proof, tested closing, spread, hedge, and volatility explanations,
and required next-day open interest and package analysis before even a paper
proposal. Graders required clearer labeling of the supplied print as unverified
current data.

### The Kobeissi Letter: CPI surprise

The prompt supplied a downside CPI surprise, an adverse prior revision, lower
two-year yields, a weaker dollar, an equity fade, and unchanged credit spreads.
The answer concluded that confirmation was partial and defined both reversal
and strengthening signals. Graders wanted explicit decision, horizon, cutoff,
fact IDs, and risk-limit fields.

## Refinement Applied

- Restricted activation to the three supported creators and handles.
- Removed a duplicate lens-selection table from the full playbook.
- Required the full evidence ledger for every fact-dependent or
  action-oriented request.
- Required `unverified user input` labeling and prohibited creator source IDs
  from validating user-supplied current facts.

No grader found a missing mental model or an unsafe instruction that required
expanding the Skill. Remaining deductions were execution misses already
covered by the operating contract or by the final wording refinement.
