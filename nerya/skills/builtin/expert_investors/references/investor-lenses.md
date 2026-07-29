# Source-Backed Investor Lenses (moved)

The per-expert lenses now live in their own sub-skills so loading one
lens does not pull all five into context:

| Lens | Load |
|---|---|
| Warren Buffett — business owner | `skill_view("expert_investors.buffett")` |
| Aswath Damodaran — narrative valuer | `skill_view("expert_investors.damodaran")` |
| Howard Marks — cycle and risk | `skill_view("expert_investors.marks")` |
| Michael Mauboussin — expectations and base rates | `skill_view("expert_investors.mauboussin")` |
| Stanley Druckenmiller — adaptive macro | `skill_view("expert_investors.druckenmiller")` |

Routing, the disagreement matrix, and the shared attribution rules live in
the hub `../SKILL.md`. Each sub-skill carries its own source register; the
deep evidence record stays in `research/01-writings.md` through
`research/06-timeline.md` (research cutoff 2026-07-27).

> Distilled with [Nuwa](https://github.com/alchaincyf/nuwa-skill) by
> [Huashu](https://x.com/AlchainHust).
