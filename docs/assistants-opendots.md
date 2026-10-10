# Assistant conversations and delegated work

Nerya starts with one assistant, using its existing mascot. Assistants appear
above task conversations in the sidebar. A personal assistant conversation is
persistent; complex work opens a separate conversation and uses the existing
command runtime for execution, approvals, cancellation and recovery.

## Conversation ownership

`AssistantStore` owns revisioned profiles and binds a session to an assistant.
Profiles may be renamed or archived. Existing sessions retain their identity;
new assistants are created only through an explicit action. Gateway channels
store an `assistant_id`, defaulting to the main assistant. Legacy main Telegram
session keys are preserved. Scheduled agent sessions can carry the assistant ID
and the creating actor in their persisted schedule entry.

`AssistantWork` stores the parent assistant session, task session, stable task
and command IDs, idempotency fingerprint, frozen request and originating actor.
`assistant_task_create` returns the task receipt immediately. The task executes
in its own conversation; nested delegation is rejected. Its ordinary execution
UI and approval controls remain available there.

Terminal command outcomes create durable results and conclusion messages in the
originating assistant conversation exactly once per command. Failed outcomes
retain their evidence; follow-up execution stays in the same task conversation.
Unconfirmed executions remain unconfirmed and pause their queue. Recovery may
submit a never-started durable request, but does not replay uncertain commands
or override operator holds. Result polling also repairs missed delivery.

## Presentation and customization

The assistant conversation shows user/reply bubbles and a pending indicator.
Intermediate execution belongs to task conversations. Top-right controls expose
recent activity and outputs without appending a growing task list beneath the
transcript. Results use the existing workspace preview with task links. Opening
and closing these panels preserves the URL, reading position and composer draft.

Basic personalization contains avatar, name and optional instructions. The avatar
picker reuses the twenty local portraits in `dashboard/public/branding/agents`
and the canonical `roleAvatars` mapping; the mascot remains the default. Preset
values use validated `role:<id>` identifiers. Uploaded PNG/JPG/WebP images are
center-cropped and normalized to bounded PNGs. Server validation checks PNG
structure, CRC, dimensions and bounded decompression. Saving these three fields
preserves hidden model, tool-policy and capability configuration.

Workspace policy remains the permission ceiling. Assistant restrictions apply to
web, gateway and delegated execution, including memory access. The permission
engine rechecks the current assistant policy before each tool call so a revoked
capability cannot continue under the previous turn's cached profile.

## Validation and PR port

The feature was developed against the local preview and then ported onto main
at `126f2e0`. This PR excludes the local financial and task-workbench refactors.
Schedule association uses main's existing `ScheduleEntry`, `TriggerAPI` and
`ScheduledSessionRunner`; it does not require the unpublished task-run SDK.

Run deterministic backend checks from the repository root:

```sh
python -m pytest -m '' tests/test_assistant_avatar.py tests/test_assistants.py tests/test_assistant_work.py tests/test_scheduled_tasks.py tests/test_gateway_commands_sessions.py -q
```

From `dashboard`, run the TypeScript and i18n checks. The standalone assistant
Playwright configuration accepts `NERYA_ASSISTANTS_QA_URL` and an optional
`NERYA_ASSISTANTS_QA_AUTH_FILE`. Avatar tests create and archive their own profile;
activity/result scenarios require the isolated QA workspace's persisted task
fixtures. They do not submit new LLM work. Counts and evidence boundaries are
recorded in `assistant-acceptance.json`.

Real workspace LLM exercises were performed on the earlier preview branch. The
clean-main PR port was validated with deterministic backend checks and actual
browser interactions, without rerunning those LLM requests. Voice and independent
assistant computers are outside this change.
