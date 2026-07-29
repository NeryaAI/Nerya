# Source-Backed Finance Creator Lenses (moved)

The per-creator lenses now live in their own sub-skills so loading one
lens does not pull all three into context:

| Lens | Load |
|---|---|
| Serenity — supply-chain signal | `skill_view("finance-creators.serenity")` |
| Unusual Whales — flow and disclosure anomaly | `skill_view("finance-creators.unusual_whales")` |
| The Kobeissi Letter — macro event-compression | `skill_view("finance-creators.kobeissi")` |

Routing, the disagreement matrix, and the shared labeling rules live in the
hub `../SKILL.md`. Each sub-skill carries its own source register; the deep
evidence record stays in `research/01-writings.md` through
`research/06-timeline.md` (research cutoff 2026-07-27).
