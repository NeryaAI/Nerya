# Delegation and Coordination

For a small or tightly coupled task, keep the work in the main agent. Use
`subagent_run` for one genuinely independent, bounded assignment. Use
`team_run` for multiple independent lanes or an explicit request for an Agent
Team. For an explicit team request, the first call after loading `team` is
`team_run`; do not spend calls reading this reference or enumerating roles.

Pass the question, already collected evidence, allowed write scope, expected
result and stop condition. Send both analysis and output languages when the
operator specifies them separately. Parallel workers must not edit the same
mutable files. Keep authoring, independent review and risk decisions distinct;
merge their shared contracts, not their accountability.

Use `role_list` / `role_get` only for an ambiguous requested role, and persist
reusable role definitions only when requested or clearly part of the task.
Use the actual exposed schemas; a role or Skill name is not itself a tool.
Expert frameworks are methods, not real people's current recommendations.

`team_run` is synchronous: synthesize its returned findings in the same turn.
If another tool returns an actual asynchronous task ID, reuse that exact ID
with `task_get` for state, progress, output and errors. `task_output` remains
a compatibility convenience; do not request both views of the same result.
`task_stop` is an explicit cancellation action, never an inspection shortcut.
Do not poll a synchronous completed run or relaunch successful work.

Finish with verified conclusions, member findings and material disagreements,
real artifact paths, gaps and the requested next decision. A launch receipt
is not a finished deliverable. State failed or missing evidence precisely;
never substitute a different account/provider or raise worker privileges to
hide a failure. The parent owns the final answer and authorized execution.

The old `agents` name and its detailed reference remain available for exact
compatibility calls; new work should start with this shared `team` workflow.
