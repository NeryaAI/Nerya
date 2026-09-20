# Collection Tools, Fallbacks and Helpers

Use exposed native tools first: `web_search` for discovery, `web_fetch` for an
exact URL, and `web_search_fetch` for a bounded search-plus-capture. Analysts
normally use `research_run`; the dedicated collector must use direct tools
rather than recursively delegating to itself. Runtime policy persists raw
captures; return actual saved paths and source metadata to the analyst.

A page shell, CAPTCHA, login wall or navigation index is not source evidence.
Follow a more specific primary-document link or run one targeted search for
that document. Native fetch handles its configured extraction fallbacks;
do not retry the same blocker indefinitely or invent a bypass. PDFs should be
fetched directly and inspected using the available extraction/visual tools
when a figure or table matters. Report extraction limitations explicitly.

Existing helpers under this skill are `scripts/web_search.py`,
`scripts/fetch_url.py`, `scripts/search_fetch.py`, `scripts/news_search.py`
and `scripts/social_search.py`. They cover the search chain and progressive
fetch/PDF extraction. Use `Skill(action="inspect", skill="research",
script="fetch_url.py")` to inspect the actual interface when a helper is
needed. Execution remains with `script_run` and its approval gate; loading or
inspecting a Skill never executes it or grants network access.

Read `references/full-playbook.md` for historical details and
`references/libraries.md` for dependencies. Prefer current exposed schemas
when an older example conflicts with the installed runtime. Never request or
print raw credentials; missing setup is an explicit terminal gap.
