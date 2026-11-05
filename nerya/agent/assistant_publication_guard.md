# Assistant publication guard: boundary findings and implementation options

Status: implementation under acceptance, 2026-10-22.

## Implemented path

The assistant coordinator now owns task-operation publication before entering
the generative kernel. `assistant_intent.py` classifies the original request
with the selected model or validates an explicit authenticated
`assistant_intent={disposition,target_task_id?}` hint. It freezes the result in
the admitted command context with an input digest and command/session/turn/actor
binding. Classification errors produce clarification, never an implicit write.

`assistant_runner.py` invokes existing assistant tools through NativeToolExecutor
and existing permission policies. `assistant_receipts.py` then independently
reads the durable task and admitted child command. Only this verified projection
is rendered, persisted and streamed. It confirms admission, not completion.
Reads distinguish command completion from delivery completion and label quoted
task-model output as unverified. Follow-ups retain their original text, target
and deterministic request ID, and cannot widen the target's permission ceiling.

Ordinary conversation still uses AgentKernel with task-writing tools excluded.
No task/domain logic was added to the generic kernel or decision/tool loop.
The classifier is probabilistic: a misclassified conversation can fail to create
requested work, and ordinary generated prose is not universally fact-checked.
The receipt guarantee applies to classified or explicitly requested task
operations, not all possible claims in arbitrary conversation.

The findings below describe the former path and alternatives considered; the
implemented dedicated coordinator avoids forwarding model-authored admission
claims and does not use a post-return text filter. Real-model and browser
acceptance remain separate from deterministic transport/DB regression tests.

## Why the former producer boundary was insufficient

Two independent inputs are missing: an authoritative request disposition and a
publication boundary that owns every visible copy of the answer.

1. The assistant composer sends all ordinary messages as `user.chat` with
   `work_mode=execute`, including questions. It omits `evidence_contract` in
   assistant conversations (`dashboard/components/chat/ChatView.tsx:1504-1517`).
   `conversation_kind=assistant` identifies the conversation, not the requested
   action. `assistants.prepare_request` binds identity and model defaults; it
   does not classify the input (`assistants.py:249`).
2. The existing semantic intent gate decides whether the public endpoint allows
   research/market analysis. It does not decide whether a request needs a new
   task. The internal command runner skips it
   (`api/routes_conversation_commands.py:23`, `api/routes_agent.py:691`). Reusing
   it would change product semantics and would still not supply this contract.
3. The optional `evidence_contract` normalizes required artifacts for the
   generic loop (`kernel.py:1002`). It is not an authoritative classification
   of an unstructured input, and extending its meaning does not create one.
4. `kernel._event_sink` publishes text to the shared streaming bus during the
   turn (`kernel.py:1988`). The kernel persists the assistant answer before
   returning (`kernel.py:2662`). A wrapper around `run_turn` receives the result
   afterward (`api/routes_agent.py:789-817`).
5. Streaming subscribers observe events already added to the replay ring
   (`streaming.py:142-146`). Lifecycle hooks swallow failures and have no
   publication veto (`hooks.py:163`). A subscriber, tool post-hook, or
   `after_turn` callback cannot safely enforce this guard. No tool post-hook
   runs in the zero-tool failure at all.
6. `command_runtime.outcome_state` classifies execution termination, not evidence
   (`command_runtime.py:45`). Changing a returned result after execution would
   not retract streamed text. `CommandStore.finish` updates canonical message
   metadata without replacing its content (`command_store.py:375-415`).

The prior Aave read-only observation was `tool_trace=[]`,
`verifier_outcome.trusted=false`, `hard_status=missing`, with a fabricated answer
and command state `succeeded`. This follow-up did not refresh runtime evidence:
the one attempted `sqlite3 -readonly` query could not open the database. The
findings above are verified from current source, not a new runtime claim.

## Option A: explicit request contract and assistant-owned publication

Recommended for a deterministic receipt guarantee. This requires ingress and
publication integration beyond the currently owned assistant modules; it can
leave the generic decision/tool loop unchanged.

The authenticated caller supplies a structured disposition, separate from prose:
`conversation`, `task_read`, `task_create`, or `task_follow_up`. Task targets and
request IDs are validated at admission. A UI action or structured API operation
is the source of this classification. A model's own declaration is not an
authoritative substitute. Legacy text-only requests remain unspecified; this
option does not silently claim to protect those requests.

For `task_create`, persist a requirement bound to the accepted command ID,
parent session, actor, and input revision/digest. Editing queued input invalidates
or rebinds it; a continuation must explicitly identify whether it resumes that
requirement. `work_mode`, previous task history, loading a Skill, or the model's
final text never creates a requirement.

The assistant producer verifies a successful current-request creation invocation
against both `assistant_work` and the admitted child command. Existing creation
IDs use a hash of parent session, turn ID, and call ID (`assistant_work.py:64`),
but that alone does not encode a revised request or continuation command. Record
the originating command/revision with the work's persisted request metadata;
require it to match before accepting a receipt. A task row with no child command,
an unrelated task, a previous-turn receipt, or a model-quoted ID cannot satisfy
the requirement. Database failure returns `assistant_task_receipt_unavailable`;
an absent matching receipt returns `assistant_task_receipt_missing`. Neither
error triggers automatic execution or a retry that could duplicate work.

For this task-creation lane, publish a host-produced admission receipt with the
observed task ID, child conversation, command ID, and state. The receipt confirms
admission only. Even with a valid queued receipt, forwarding arbitrary generated
text would still permit invented completion, reports, or performance.

`conversation` and `task_read` retain normal responses and do not require task
creation. `task_follow_up` requires its own message/admission receipt, not a new
task. Explicit input/approval pauses and cancellation remain those states, with
no success publication. Missing receipt on attempted normal completion yields a
structured non-success outcome and a host-authored explanation.

Enforcement must precede text streaming, replay-ring insertion, canonical message
storage, gateway delivery, and command success publication. The assistant runner
must own a request-local publication path for these operations. The current kernel
has no injectable publication/storage sink, so this requires either a narrow
plumbing change there (no task/domain logic in the loop), or a larger assistant
runner that owns those operations. Do not monkeypatch global buses or overwrite
already published answers and call that prevention.

## Option B: retain the automatic free-text experience

A producer-owned semantic routing pass could emit a schema-validated disposition
from the current user input and bounded context, using the selected model. Bind
the routing decision to the accepted request and make ambiguity/unavailability
explicit. This avoids domain keywords but adds inference latency/cost and still
has classification errors. False negatives bypass a receipt requirement; false
positives turn questions into tasks. Schema validation cannot establish that the
classification is correct. It also needs the publication integration in Option A.

If deterministic classification is required, ask the operator to confirm ambiguous
intent or expose an explicit action. That is a product-flow decision, not an
implementation detail to infer from the current `execute` setting.

## Acceptance before calling the guard complete

- Drive the real assistant admission/execution/publication path with a recorded
  zero-tool provider answer. Assert missing-receipt error, non-success command
  state, and absence of the fabricated answer in stream replay, persisted
  transcript, final response, and outbound delivery. A direct helper test alone
  is insufficient.
- Questions, explanations mentioning task-creation terms, conversation, and
  read-only task queries do not create tasks or receive missing-creation errors.
- A current-request admitted task produces only its real admission receipt; old,
  foreign, orphaned, copied, failed, edited-request, and resumed-turn receipts
  cannot satisfy a new requirement.
- Partial failures and receipt lookup failures never replay an uncertain effect;
  approval/input/cancel states remain intact. Concurrent requests cannot consume
  each other's receipt or publication state.
- Real selected-model retest remains separate from deterministic transport replay.

No runtime, service, cache, model configuration, generic loop, or frontend changes
were made for this follow-up. The concrete next decision is explicit caller
intent versus probabilistic automatic routing, plus ownership of pre-publication
integration. Adding only an unused verifier helper would not fix the observed path.
