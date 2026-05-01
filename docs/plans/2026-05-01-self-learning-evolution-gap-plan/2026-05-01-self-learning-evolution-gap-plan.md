# Nerya 自学习、自反思、自进化能力提升方案

日期：2026-05-01  
范围：`Nerya/` 当前实现，对比 `../hermes-agent/` 与 `../evolver/`  
目标：把 Nerya 从“有反思和提案能力”升级为“可审计、可验证、可持续沉淀经验的自进化系统”。

## 1. 结论

Nerya 不是从零开始。当前代码已经具备：

- 长期记忆：`nerya/agent/memory.py` 的 global/strategy memory、压缩、白名单写入。
- 会话检索：`nerya/agent/session_search.py` 和 `session_search_fts.py` 已经引入 Hermes-style FTS session recall。
- 反思：`nerya/evolution/reflection_engine.py` 会从 journal 和 strategy history 中找亏损、坏触发器、滑点、陈旧数据、subagent 分歧、归因和 paper/live divergence。
- 安全提案边界：`nerya/evolution/patch_proposal.py` 定义受保护 scope，所有 agent-authored 改动以 proposal 形式落地。
- 策略调优：`nerya/strategies/evolution.py` 已实现 per-strategy tuning runner，通过 `strategy_tuner` subagent 生成 `strategy_tuning_proposal`，不自动应用。
- API/UI：`routes_evolution.py`、`routes_strategies_runtime.py`、dashboard client API 已有 evolution/tuning/proposal surfaces。

主要缺口不是“没有自进化”，而是自进化闭环还不够完整：

1. 记忆和反思还偏“记录/摘要”，不是可复用的经验资产。
2. 信号提取面不够广，缺少对用户纠错、工具失败、proposal outcome、validation failure、技能脚本摩擦、UI 操作决策的统一采集。
3. proposal 安全，但 proposal 的通过、拒绝、回滚、验证结果还没有反哺成下一轮选择和调优依据。
4. strategy tuning 已有，但缺少标准化 validation plan、shadow/canary/backtest gate 和“成功后固化为 capsule”的机制。
5. dashboard 暴露了零散入口，但缺少一个面向 operator 的 Self-Evolution Workbench。

本方案建议保留 Nerya 的 skill-first 和 proposal-first 安全边界，吸收 Hermes 的 memory/session/tool lifecycle，吸收 Evolver 的 Gene/Capsule/Event 资产模型，但不要直接把 Evolver 作为运行时依赖。

## 2. 对比矩阵

| 能力面 | Nerya 当前 | Hermes 参考 | Evolver 参考 | Nerya 目标 |
|---|---|---|---|---|
| 长期记忆 | Markdown memory、strategy learnings、memory index、session search | `MemoryProvider` 生命周期、外部 provider、FTS session recall、cheap summarization | `memory/` 扫描，演化事件沉淀 | 统一 Memory/Evolution hooks，记忆写入前打分，session-end 自动提炼 |
| 反思 | 固定 finder 扫交易/日志，写 learning note 和 proposal | session insight、trajectory、cron session 入库 | 多层 signal extractor、去重、饱和检测 | `SignalExtractor` 统一处理交易、工具、用户、proposal、验证、技能事件 |
| 自我学习 | `learning_update` proposal，after-turn no-op detector | `sync_turn`、`on_session_end`、`on_memory_write` | Gene/Capsule/Event，成功经验复用 | Nerya-native `EvolutionAsset`：Gene-like rule、Capsule-like validated case、Event audit |
| 自我进化 | PatchProposal、安全 scope、strategy tuning proposal | 工具可用性剪枝，profile/session persistence | GEP prompt + validation + review + rollback | 所有 evolution 输出先 proposal，再验证，再 operator promotion，再固化经验 |
| 策略优化 | `StrategyEvolutionRunner` 已有，调 tuner subagent | 无 trading-native 模型 | gene/capsule 可复用策略 | 每个 strategy 有独立信号、资产、验证、shadow/canary 和 outcome learning |
| 工具/技能演化 | skill/script proposal 存在，但验证闭环弱 | registry `check_fn` / `requires_env` 避免暴露不可用工具 | signal detects tool bypass / repeated tool usage | skill/script failure -> patch proposal -> tests/examples -> approved -> capsule |
| Operator UX | proposals、settings memory、strategy tuning 分散 | CLI/gateway/session tooling 更成熟 | review mode 明确 | `/self-evolution` workbench 展示 signals/assets/events/proposals/validation |

## 3. 目标架构

核心闭环：

```text
Runtime Evidence
  -> Evolution Signal
  -> Evidence Bundle
  -> Evolution Asset Selection
  -> Proposal Draft
  -> Validation Plan
  -> Operator Approval / Promotion
  -> Outcome Event
  -> Memory + Asset Update
```

### 3.1 Runtime Evidence

输入来源：

- Agent turns：`nerya/agent/kernel.py` 的 turn result、tool calls、no-op、errors。
- Session logs：`nerya/agent/session_search.py`、gateway/session transcript。
- Trading history：`nerya/strategy_history/*`、`nerya/strategies/performance.py`。
- Proposals：`nerya/evolution/patch_proposal.py`、`promotion.py`、`rollback.py`。
- Strategy tuning：`nerya/strategies/evolution.py` 的 tuning run、accepted/dropped changes、review doc。
- Skills/scripts：skill invocation journal、script failures、examples/tests。
- Operator actions：approval/reject/rollback、manual edits、dashboard state transitions。

### 3.2 Evolution Signal

新增 `nerya/evolution/signals.py`，提供统一输出：

```python
@dataclass(frozen=True)
class EvolutionSignal:
    id: str
    ts: str
    source: Literal[
        "turn", "session", "tool", "memory", "proposal",
        "strategy", "trading", "skill", "script", "operator"
    ]
    kind: str
    severity: Literal["info", "warn", "critical"]
    strategy_id: str | None
    evidence_refs: list[str]
    summary: str
    dedupe_key: str
    confidence: float
```

首批 signal kind：

- `repeated_noop`
- `tool_failure_cluster`
- `user_correction`
- `proposal_rejected`
- `proposal_rolled_back`
- `validation_failed`
- `strategy_drawdown`
- `high_slippage`
- `paper_live_divergence`
- `subagent_disagreement`
- `skill_example_failed`
- `script_error`
- `memory_low_value_write`
- `evolution_saturation`

设计原则：

- 先 deterministic，再 LLM summarization。
- 对相同 `dedupe_key` 做窗口去重，避免重复生成同一个 proposal。
- 每个 signal 必须带 evidence refs，不能只带自由文本。

### 3.3 Evolution Asset

新增 `nerya/evolution/assets.py`，实现 Nerya-native asset store。

建议文件布局：

```text
workspace/evolution/assets/
├── genes.json              # 可复用规则，人工/提案批准后写入
├── capsules.jsonl          # 验证成功案例，append-only
├── events.jsonl            # 每次演化尝试，append-only
├── candidates.jsonl        # 未批准候选资产
├── rejected.jsonl          # 拒绝/失败资产
└── locks/                  # 文件锁
```

资产类型：

```python
@dataclass(frozen=True)
class EvolutionGene:
    id: str
    category: Literal["repair", "optimize", "harden", "research", "strategy", "skill"]
    signals_match: list[str]
    preconditions: list[str]
    strategy: list[str]
    validation: list[str]
    forbidden_scopes: list[str]
    max_files: int
    confidence: float

@dataclass(frozen=True)
class EvolutionCapsule:
    id: str
    gene_id: str
    source_event_id: str
    summary: str
    evidence_refs: list[str]
    validation_results: list[dict]
    outcome_score: float
    promotion_ref: str | None

@dataclass(frozen=True)
class EvolutionEvent:
    id: str
    parent_id: str | None
    signals: list[str]
    genes_used: list[str]
    proposal_id: str | None
    mutation_scope: list[str]
    validation_status: Literal["not_run", "passed", "failed", "skipped"]
    outcome: Literal["candidate", "proposed", "approved", "applied", "rejected", "rolled_back"]
    outcome_score: float
```

和 Evolver 的区别：

- Nerya asset 不输出裸 GEP prompt 让外部 agent 执行，而是进入 `PatchProposal`。
- 所有可变更内容仍受 `PROTECTED_SCOPES` 和 Approval Gate 约束。
- trading strategy 的 asset 必须绑定 strategy id、performance snapshot、回测/shadow/canary 证据。

### 3.4 Memory/Evolution Hooks

新增 `nerya/evolution/hooks.py`，在 `AgentKernel` 中集中调用，借鉴 Hermes `MemoryProvider` 的生命周期。

建议 hook：

```python
class EvolutionHookBus:
    def on_turn_start(self, turn): ...
    def pre_turn_recall(self, query, strategy_id=None): ...
    def after_tool_result(self, tool_call, result): ...
    def after_turn(self, turn, result): ...
    def on_pre_compress(self, transcript): ...
    def on_session_end(self, session): ...
    def on_memory_write(self, target, content, source): ...
    def on_delegation(self, task, result): ...
    def on_proposal_state_change(self, proposal, old, new): ...
    def on_strategy_tuning_run(self, run): ...
```

这些 hook 做三件事：

1. 采集 signal。
2. 选择已有 Gene/Capsule 作为上下文。
3. 只在明确达到阈值时创建 proposal，不直接 mutate runtime。

## 4. 分阶段实施计划

### P0：把现有自进化能力变成可观测基线

目标：不改变行为，先让当前能力可查、可测、可解释。

改动文件：

- `nerya/evolution/events.py`：定义 `EvolutionSignal`、`EvolutionEvent` schema。
- `nerya/evolution/event_store.py`：append-only JSONL store，写入 `workspace/evolution/events.jsonl`。
- `nerya/evolution/signals.py`：从现有 journals/session/proposals/strategy history 提取首批 deterministic signals。
- `nerya/evolution/runner.py`：在 `evolve()` 输出中附带 `signals` 和 `evidence_refs`。
- `nerya/evolution/patch_proposal.py`：proposal metadata 增加 `evidence_refs`、`validation_plan_id`、`source_event_id` 字段，保持向后兼容。
- `nerya/api/routes_evolution.py`：新增 `/evolution/signals`、`/evolution/events`。
- `tests/test_evolution_events.py`、`tests/test_evolution_signals.py`。

验收：

```bash
python -m pytest tests/test_evolution_events.py tests/test_evolution_signals.py -q
python -m pytest tests/test_memory_memsearch_ops.py -q
python -m nerya.cli.app evolution reflect
```

完成标准：

- 现有 reflection/tuning/proposal 行为不变。
- 每次 `/evolution/reflect` 能返回 signal list 和 evidence refs。
- signal 去重能防止同一个 no-op/tool failure 连续刷 proposal。

### P1：引入 EvolutionAsset store

目标：把“经验”从 markdown 摘要升级为可复用、可审计、可验证资产。

改动文件：

- `nerya/evolution/assets.py`：实现 Gene/Capsule/Event 的 load/search/upsert/append。
- `nerya/evolution/selector.py`：根据 signal 匹配已有 gene/capsule。
- `nerya/evolution/asset_policy.py`：校验 forbidden scopes、max files、validation command allowlist。
- `nerya/evolution/patch_proposal.py`：新增 `evolution_asset_proposal` kind，或者复用 `learning_update` 并增加 `asset_candidate` metadata。推荐新增 kind，审计更清楚。
- `nerya/evolution/promotion.py`：proposal apply/reject/rollback 后写入 `EvolutionEvent`。
- `nerya/agent/memory_index.py`：把高质量 capsule 摘要作为可检索 fact，但不把完整资产塞进 always-on prompt。
- `tests/test_evolution_assets.py`、`tests/test_evolution_asset_policy.py`。

默认内置 genes：

- `gene_nerya_repair_from_tool_failures`
- `gene_nerya_harden_repeated_noop`
- `gene_nerya_strategy_drawdown_review`
- `gene_nerya_skill_failure_patch`
- `gene_nerya_memory_quality_filter`
- `gene_nerya_proposal_outcome_learning`

验收：

```bash
python -m pytest tests/test_evolution_assets.py tests/test_evolution_asset_policy.py -q
python -m nerya.cli.app evolution evolve
```

完成标准：

- asset store 是 append-only/atomic/file-lock safe。
- 不安全 validation command 会被拒绝。
- 被拒绝 proposal 会沉淀为 negative event，不会生成成功 capsule。
- 成功验证和批准后的 proposal 可以生成 capsule。

### P2：扩展 Kernel 生命周期 hooks

目标：把 Hermes 的 memory lifecycle 迁移成 Nerya-native hooks，让学习发生在正确时机。

改动文件：

- `nerya/evolution/hooks.py`：实现 `EvolutionHookBus`。
- `nerya/agent/kernel.py`：
  - turn start：检索相关 memory/assets。
  - after tool：记录 tool failures / repeated usage / bypass signals。
  - after turn：沿用 `maybe_propose_from_turn`，但改为 signal-driven。
  - pre compress：提炼高价值 lesson/candidate event。
  - session end：总结 session outcome，写 signals/events。
  - delegation end：记录 subagent outcome。
- `nerya/tools/native/memory.py`：system prompt memory block 改成“memory summary + selected assets”，避免把资产库全塞进 prompt。
- `nerya/agent/session_search.py`：提供 evidence bundle API，返回摘要和 event refs。
- `tests/test_evolution_hooks.py`、`tests/test_kernel_evolution_hooks.py`。

验收：

```bash
python -m pytest tests/test_evolution_hooks.py tests/test_kernel_evolution_hooks.py -q
python -m pytest tests/test_memory_isolation.py -q
```

完成标准：

- turn/session/delegation/proposal 的事件都能进入 event store。
- hook 失败不影响主 agent turn。
- memory write 有质量 gate，低价值摘要不会污染 global memory。
- selected asset 在 prompt 中有 token budget 和 source refs。

### P3：强化 strategy self-evolution

目标：让每个策略的调优从“subagent 建议”升级为“证据 -> proposal -> validation -> shadow/canary -> outcome learning”。

改动文件：

- `nerya/strategies/evolution.py`：
  - tuning run 写 `EvolutionSignal` 和 `EvolutionEvent`。
  - tuner 输出必须包含 validation plan。
  - dropped changes 写入 event，成为 future negative learning。
- `nerya/strategies/performance.py`：
  - 增加 drawdown、win/loss streak、slippage、latency、paper/live divergence 的标准字段。
- `nerya/evolution/validation_plan.py`：
  - 生成 per-change validation plan。
  - 支持 `unit_test`、`static_check`、`backtest`、`shadow_run`、`canary`、`manual_review`。
- `nerya/trading/strategy_versions.py`：
  - promotion outcome 写回 strategy asset/capsule。
- `nerya/api/routes_strategies_runtime.py`：
  - tuning status 返回 event/capsule/proposal 链路。
- `dashboard/app/strategies/[id]/page.tsx`：
  - strategy detail 中展示 tuning evidence、validation、event timeline。
- `tests/test_strategy_evolution_validation.py`。

验收：

```bash
python -m pytest tests/test_strategy_evolution_validation.py -q
python -m pytest tests/test_strategy_runtime_*.py -q
cd dashboard && npx tsc --noEmit
```

完成标准：

- tuning proposal 没有 validation plan 时不能进入 `pending_review`。
- 回测/shadow/canary gate 未通过时不能标记为 promoted。
- live trading、账户、vault、signer、global risk limits 仍不能被 tuning proposal 触碰。
- 策略维度可以看到“为什么调、调了什么、验证了什么、结果如何、下次如何复用”。

### P4：建设 Self-Evolution Workbench

目标：把 operator 看到的自进化从零散 proposal 页面升级为可操作控制台。

改动文件：

- `nerya/api/routes_evolution.py`：
  - `/evolution/signals`
  - `/evolution/assets`
  - `/evolution/events`
  - `/evolution/candidates/promote`
  - `/evolution/candidates/reject`
  - `/evolution/validation/run`
- `dashboard/lib/clientApi.ts`：新增 self-evolution API client。
- `dashboard/lib/evolutionTypes.ts`：新增 typed model。
- `dashboard/app/self-evolution/page.tsx`：新增 workbench。
- `dashboard/app/layout.tsx` 或 sidebar config：增加入口。

Workbench 视图：

1. Signals：按 source/severity/strategy/time 过滤。
2. Assets：Gene/Capsule 列表、命中次数、成功率、最近 outcome。
3. Events：每次 evolution 的链路和 evidence refs。
4. Proposals：proposal state、validation status、protected-scope check。
5. Validation：可以重跑只读 validation，查看 logs。
6. Promotion Review：批准/拒绝候选 asset，不直接应用 runtime 变更。

验收：

```bash
cd dashboard && npx tsc --noEmit
python -m pytest tests/test_routes_evolution_assets.py -q
```

完成标准：

- operator 能从一个页面追溯 signal -> event -> proposal -> validation -> outcome。
- 页面不把 dashboard state 当 source of truth，所有数据来自 API。
- 所有 promotion 操作都打到 Python runtime，不写 localStorage。

### P5：可选 Evolver bridge

目标：只在需要跨系统资产交换时接入 Evolver/GEP；默认不依赖。

改动文件：

- `nerya/evolution/evolver_bridge.py`
- `nerya/cli/commands/evolution.py`
- `scripts/export_evolution_assets.py`
- `scripts/import_evolution_assets.py`

原则：

- Export：Nerya Gene/Capsule/Event -> GEP-compatible JSON。
- Import：外部 Gene/Capsule -> candidate zone -> policy check -> proposal -> approval。
- 不允许外部 asset 覆盖同 ID 本地 asset。
- 不执行 Evolver 输出的 prompt 或 `sessions_spawn` 文本。
- 外部 validation command 必须重新过 Nerya allowlist。

验收：

```bash
python -m pytest tests/test_evolver_bridge.py -q
python scripts/export_evolution_assets.py --dry-run
python scripts/import_evolution_assets.py --path tmp/sample_gene.json --dry-run
```

完成标准：

- 离线可用。
- Hub/网络不可用不影响 Nerya 自进化。
- 外部资产只能进入候选区，不能直接修改 runtime。

## 5. 数据与安全边界

### 必须保持的边界

- 不绕过 `PatchProposal`。
- 不绕过 `RiskGate` / `ApprovalGate`。
- 不让 agent 直接改 `workspace/strategies/*`、skill scripts、triggers、accounts、vault、signer policy。
- 不把 secret、vault ref 内容、账户凭证写入 memory/event/capsule。
- 不把整个 asset store 注入 prompt，只按 query/strategy/source 选择少量资产。

### Protected scopes

继续沿用 `nerya/evolution/patch_proposal.py` 的 `PROTECTED_SCOPES`，并在 `asset_policy.py` 中复用：

- `strategies/*/limits.yml`
- `accounts/accounts.yml`
- `accounts/exchanges.yml`
- `accounts/secrets.refs.yml`
- `vault/*`
- `nerya.yml:runtime.live_trading_enabled`
- `nerya.yml:runtime.kill_switch`
- `approvals/policy.yml`
- `approvals/signer_policy.yml`
- trigger rate/payload limits

### 记忆污染防护

新增 `nerya/evolution/quality.py`：

- 低价值标准：无 evidence ref、纯重复、只描述“完成了”、包含未验证结论、含敏感信息、与已有 memory/capsule 高相似。
- 高价值标准：有明确错误/决策/指标变化/验证结果/回滚原因/用户纠错，且能影响未来选择。
- 低价值内容进入 event 的 raw evidence，但不进入 `memory/global.md`。

## 6. API 草案

```text
GET  /evolution/signals?source=&strategy_id=&severity=&limit=
GET  /evolution/events?strategy_id=&proposal_id=&limit=
GET  /evolution/assets?kind=gene|capsule&query=&strategy_id=
POST /evolution/assets/candidate
POST /evolution/assets/promote
POST /evolution/assets/reject
POST /evolution/validation/run
POST /evolution/reflect
POST /evolution/evolve
POST /evolution/proposals
```

返回结构必须带：

- `evidence_refs`
- `source_event_id`
- `proposal_id`
- `validation_status`
- `safe_to_promote`
- `blocked_reasons`

## 7. CLI 草案

```bash
python -m nerya.cli.app evolution signals --strategy btc_momentum
python -m nerya.cli.app evolution assets search "slippage"
python -m nerya.cli.app evolution assets promote <candidate_id>
python -m nerya.cli.app evolution validate <proposal_id>
python -m nerya.cli.app evolution events --proposal <proposal_id>
python -m nerya.cli.app evolution export --format gep --dry-run
```

CLI 和 dashboard 调同一组 runtime API，避免两套 truth。

## 8. 测试计划

新增测试：

- `tests/test_evolution_events.py`
- `tests/test_evolution_signals.py`
- `tests/test_evolution_assets.py`
- `tests/test_evolution_asset_policy.py`
- `tests/test_evolution_hooks.py`
- `tests/test_kernel_evolution_hooks.py`
- `tests/test_strategy_evolution_validation.py`
- `tests/test_routes_evolution_assets.py`
- `tests/test_evolver_bridge.py`（P5 可选）

回归测试：

```bash
python -m pytest tests/ -q
cd dashboard && npx tsc --noEmit
python -m nerya.cli.app doctor
```

重点场景：

1. 连续 no-op turn：只生成一次 deduped signal，达到阈值后生成 `learning_update` proposal。
2. 工具连续失败：生成 `tool_failure_cluster`，匹配 repair gene，创建 proposal，并附 validation plan。
3. proposal 被拒绝：写 negative `EvolutionEvent`，下次 selector 降低同类 gene 权重。
4. strategy tuning 建议改 live risk limit：被 protected-scope policy 拒绝，写 rejected event。
5. strategy tuning 建议改非保护配置：生成 proposal，validation pass 后进入 review，不自动 apply。
6. 用户纠错：生成 `user_correction` signal，写 memory/capsule 候选，但需要质量 gate。
7. session end：提炼高价值事件，低价值 turn summary 不写 global memory。
8. dashboard promotion：API 写入 asset/event，localStorage 不成为 source of truth。

## 9. 迁移顺序

推荐顺序：

1. P0 先落 event/signal store，不改变行为。
2. P1 引入 asset store，但只读/候选，不接入自动 proposal。
3. P2 接 kernel hooks，先只采集，再逐步启用 proposal generation。
4. P3 强化 strategy tuning validation，优先保护 live trading。
5. P4 上 dashboard workbench。
6. P5 视需求做 Evolver bridge。

每一步都必须能独立回滚：

- P0 回滚只删除新 event/signal 文件与 API。
- P1 回滚只停用 asset selector，保留原 reflection proposal。
- P2 回滚通过 config 关闭 hooks：`agent.native.evolution_hooks_enabled: false`。
- P3 回滚保留原 `StrategyEvolutionRunner` 的 proposal-only 行为。
- P4 回滚只移除 dashboard/API 新 surface。
- P5 回滚完全不影响 Nerya-native evolution。

## 10. Definition of Done

这轮提升真正完成时，应满足：

- Nerya 每次自进化都有 event 链路，能追溯 signal、证据、选择、proposal、validation、outcome。
- 记忆不再只是 markdown 摘要，高价值经验会固化为 Gene/Capsule-like asset。
- 失败、拒绝、回滚同样进入学习闭环，避免重复踩坑。
- strategy tuning 不再只依赖 subagent 文本建议，而是强制 validation plan 和 evidence。
- operator 能在 dashboard 看到自进化全貌，并批准/拒绝候选资产。
- 所有实际变更仍走 `PatchProposal`，不突破 trading/security protected scopes。
- 外部 Evolver/GEP 只作为可选资产交换，不成为 Nerya 的硬运行时依赖。
