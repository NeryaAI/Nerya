# Task Plan: Prompt Playwright Hardcoding Cleanup

## Goal
Audit and remove brittle regex routing, case-specific hardcoding, committee marker hacks, and oversized forced prompt policy introduced around the prompt Playwright repairs, then rerun the target cases.

## Scope
- Runtime files first: `nerya/agent/loop.py`, `nerya/agent/kernel.py`, tool/runtime helpers touched by the prompt repair.
- Test/harness files may contain case IDs and assertions; those are not product routing unless mirrored into runtime code.
- Preserve structured contracts: `required_artifacts`, tool schemas, tool result status, and public SDK compatibility are allowed.

## Phases
- [x] Phase 1: Search for suspicious runtime patterns
- [x] Phase 2: Classify allowed parser/contract code vs brittle routing
- [x] Phase 3: Remove or shrink brittle prompt/routing code
- [x] Phase 4: Verify with backend tests
- [ ] Phase 5: Clean isolated workspace and rerun Playwright cases
- [ ] Phase 6: Update Chinese report

## Key Questions
1. Is any runtime decision keyed to a Playwright case id, prompt phrase, ticker, or role label?
2. Are regexes used for parsing/validation, or for deciding task intent/routing?
3. Are always-on prompts carrying large hardcoded workflows that should be tool/schema/state driven?

## Status
**Currently in Phase 5** - isolated workspace cleanup and Playwright rerun after runtime hard-route cleanup.
