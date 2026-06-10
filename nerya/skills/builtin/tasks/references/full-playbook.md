# Tasks Playbook

Tasks are operator-facing units of work. Use them when the user says "create a
task", "run this every day", "keep monitoring", "send me a report", or asks to
inspect task progress.

## Shapes

- One-off background task: use native `subagent_run_async`.
- Recurring agent task: create a schedule with `session_kind="agent"`.
- Recurring script task: create a schedule with `session_kind="script"` and an
  approved script id.
- Progress inspection: use `task_list`, `task_summary`, `task_get`,
  `task_output`, or `scripts/list_tasks.py`.

## Prompt Contract

For recurring agent tasks, the creating agent should write a detailed
process-style `generated_prompt` instead of relying on a terse user sentence.
The script still accepts `prompt` or the same sentence as a fallback, but the
expected workflow is to provide `source_request` plus a generated execution
brief that includes:

- objective and scope,
- schedule/frequency context,
- required data sources or skills,
- output format and language,
- delivery expectations,
- explicit safety/verification constraints.

The script stores `generated_prompt` as `payload.prompt` and keeps
`source_request` in the payload for audit. If no generated prompt is supplied,
it falls back to `prompt` or `source_request`.

## Recurring Agent Example

```json
{
  "id": "daily_blockchain_digest",
  "title": "Daily blockchain digest",
  "cron": "0 11 * * *",
  "timezone": "Asia/Shanghai",
  "task_type": "agent",
  "source_request": "每天早上11点生成区块链相关新闻和行情分析，输出中文摘要",
  "generated_prompt": "You are running the daily blockchain digest task. Every scheduled run, gather recent blockchain and crypto-market news, summarize the most important developments in Chinese, include a concise market analysis section, cite or name the sources used, and end with clear risk notes. Deliver the final answer as a compact Chinese briefing suitable for Telegram.",
  "session_mode": "reuse",
  "session_id": "daily-blockchain-digest",
  "delivery_targets": [
    {"kind": "gateway", "platform": "telegram"}
  ]
}
```

Run with:

```bash
python -m nerya.skills.builtin.tasks.scripts.create_task --json '<payload>'
```

## Recurring Script Example

```json
{
  "id": "daily_chain_digest_script",
  "cron": "0 11 * * *",
  "timezone": "Asia/Shanghai",
  "task_type": "script",
  "script_id": "blockchain_digest",
  "script_args": {"topic": "区块链相关新闻 和行情分析"},
  "delivery_targets": [
    {"kind": "gateway", "platform": "telegram"}
  ]
}
```

## Guardrails

- Do not create a strategy when the user asked for a plain operator task.
- Prefer `session_mode="reuse"` for daily recurring agent reports so the task
  keeps useful context in one stable session.
- Use `session_mode="fanout"` only when the user asks to run the same prompt in
  multiple sessions.
- Use `delivery_targets` for output routing; do not hand-code platform sends in
  the prompt.
- For scripts, only reference approved script ids. This skill does not create
  arbitrary executable code.
- If the user is only asking for status, list/inspect tasks instead of creating
  new schedules.
