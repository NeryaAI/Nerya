"""Built-in skills, Anthropic Skill spec.

Each subfolder contains a single ``SKILL.md`` (frontmatter + markdown
playbook). Optional ``scripts/`` directories hold *standalone* Python
modules invoked by the agent via ``run_shell``; the registry never
imports them and never auto-discovers an action surface.

Loading is intentionally minimal — name, description, version,
license, author, and the markdown body. Everything else the runtime
needs comes from the agent's native tools, not from this package.
"""
