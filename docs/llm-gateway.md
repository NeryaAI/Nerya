# LLM Gateway

The LLM Gateway is the only way for Nerya code to reach a language model.
It owns provider keys, tier policy, per-day budget and structured output
validation. Scripts never receive a provider client.

## Tiers

Configured in `workspace/nerya.yml`:

```yaml
llm:
  default_tier: medium
  tiers:
    light:
      provider: openai
      model: light-model
      max_tokens: 2048
      temperature: 0.1
      daily_budget_usd: 3
      allowed_tasks: [news_filtering, compress, classify, trigger_triage]
    medium:
      provider: openai
      model: medium-model
      max_tokens: 8192
      temperature: 0.2
      daily_budget_usd: 15
      allowed_tasks: [normal_agent_loop, subagent_analysis, strategy_review, trade_explanation]
    high:
      provider: openai
      model: high-model
      max_tokens: 32768
      temperature: 0.2
      daily_budget_usd: 50
      allowed_tasks: [script_generation, skill_generation, complex_signal_analysis, large_loss_postmortem, strategy_evolution]
```

## Call path

```
caller (agent/skill/script)
   │ task, prompt, expected schema
   ▼
LLMGateway.call()
   │ redact prompt (no secrets, no raw .env, no raw private keys)
   ▼
TierPolicy.resolve(task)
   │ picks tier, enforces allowed_tasks
   ▼
BudgetPolicy.check(tier)
   │ rejects if daily budget blown
   ▼
ModelRouter.dispatch(tier)
   │ calls a native provider adapter in `nerya/llm/adapters/`
   │ (`openai.py`, `anthropic.py`, `gemini.py`, `ollama.py`, …)
   ▼
StructuredOutputValidator.parse(raw, schema)
   ▼
LLMUsageJournal.write(tokens, usd, tier, task)
   ▼
returns parsed result
```

## SDK surface

```python
client.llm.classify(task="news_filtering", text=..., labels=...)
client.llm.compress(task="compress", text=..., max_tokens=512)
client.llm.extract_json(task="analyze_signal", text=..., schema=...)
client.llm.analyze_signal(task="complex_signal_analysis", context=...)
client.llm.generate_script_proposal(requirements=..., tier="high")
```

The `tier` argument is *advisory*; the gateway may demote but never promote
unless the caller holds `llm.high_allowed` permission. Scripts never hold
`llm.high_allowed` unless their manifest was approved with it.

## Script LLM policy

Scripts must declare in their manifest:

```yaml
llm_policy:
  allowed_tiers: [light, medium]
  allowed_tasks: [news_filtering, classify]
  max_calls_per_run: 50
  max_tokens_per_run: 20000
  max_cost_usd_per_day: 1.00
  high_tier_requires_approval: true
```

The gateway enforces every field. Unapproved scripts default to
`allowed_tiers: [light]`, `max_cost_usd_per_day: 0.25`.

## Security

- Every call writes `journals/llm.jsonl` with tier, task, tokens, USD cost,
  caller (`agent`, `subagent:<name>`, `skill:<id>.<action>`,
  `script:<script_id>`) and an opaque request hash. Prompt content is
  *redacted* in the journal (prefix + length + sha256) unless
  `debug_full_prompt_journal: true` is explicitly set.
- Provider API keys live in the SecretVault. `nerya/llm/model_router.py`
  resolves a `secret_ref` into a short-lived header inside the native
  adapter call and never exposes the raw key to the caller's scope.
- Every LLM call is also written to `journals/security.jsonl` with the
  redaction summary, so it is auditable alongside SecretVault access.
