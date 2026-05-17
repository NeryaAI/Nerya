<!-- nerya-skill-frontmatter-start -->
---
name: llm
description: "Use for a separate one-shot LLM helper task such as summarizing, translating, rewriting, labeling, or drafting text."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# LLM Helper

Use this only when a separate model call transforms supplied content.
Do not use it for the main agent's own reasoning.

## Flow

DEFINE the transform: summarize, translate, rewrite, classify, or draft.
BOUND the input and output size.
PASS only the needed source text.
CHECK the result for instruction drift and missing constraints.
RETURN the transformed artifact.

## Lazy References

- `references/full-playbook.md` for detailed helper-call patterns.
