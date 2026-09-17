import type { WorkflowKind, WorkflowNode } from "./workflowTypes";
import { asObject, type WorkflowText } from "./workflowPresentation";

// Guidance is about resource contracts, never strategy-specific code generation.
const GUIDES: Record<WorkflowKind, [string, string, string, string]> = {
  strategy: ["填写目标和说明；改变执行规则请编辑脚本或让主 Agent 修改。", "Edit the objective and description. Change execution rules in code or ask the main Agent.", "名称和说明只影响展示，不会自动改写策略。", "The name and description do not rewrite strategy behavior."],
  source: ["同一个源内选择多个品种和周期，设置每组条数；不同读取配置在节点内切换。", "Choose markets, timeframes and rows per series inside one source. Switch existing subscriptions inside the same node.", "只有脚本或 Agent 实际读取的配置才会影响数据请求；连线不是读取证明。", "Only settings consumed by the script or Agent affect requests. A connection alone is not proof of a read."],
  script: ["改参数先打开关联数据源；改判断逻辑可直接编辑代码，或用一句话交给主 Agent。", "Change parameters in a connected data source. Edit code or describe a logic change to the main Agent.", "名称和用途是说明文字；实际执行的是下方 Python。修改后需要校验并保存提案。", "Name and purpose are documentation. The Python below executes. Validate and save a proposal after editing."],
  agent: ["写清任务、依据和输出要求，再设置模型预算、可用工具与对话记忆。", "Describe the task, evidence and expected output. Then set model budget, tools and conversation memory.", "任务说明不改变触发条件。何时调用由上游脚本和调度器决定。", "Instructions do not change when this Agent is invoked. The upstream script and scheduler control that."],
  scheduler: ["选择固定间隔或每天定时，并检查时区；复杂时间使用 Cron。", "Choose an interval or daily time and check the timezone. Use Cron for other schedules.", "这里编辑的是计划。保存提案不会立即执行，也不会改变已经开始的运行。", "This edits a schedule definition. Saving a proposal does not start a job or change an in-flight run."],
  account: ["从已有账户中选择绑定；余额、凭据和权限在账户管理中维护。", "Choose an existing account. Manage balances, credentials and permissions in Account management.", "只改变候选策略的账户引用，不复制密钥，也不自动授予实盘权限。", "This changes the candidate account reference, without copying secrets or granting live permissions."],
  risk: ["先确认脚本能否直接下单，再设置金额、持仓和复核限制。", "Check whether the script may place orders directly, then set amounts, position caps and review requirements.", "脚本下单开关不等于 Agent 的工具权限；Agent、账户和运行时限制仍分别生效。", "The direct-order switch is not the Agent tool permission. Agent, account and runtime limits still apply separately."],
  evidence: ["选择回看多少次运行、多少小时；这不是运行记录本身。", "Choose how many runs and hours to review. These are selection settings, not execution records.", "仅改变下一次复盘的证据范围，不会编辑或删除历史。", "This changes the next review's evidence window, not historical records."],
  proposal: ["选要改善的指标，补充要求，并限定允许修改的文件。", "Choose improvement objectives, add instructions and limit editable files.", "得到的是候选改进，不会自动覆盖当前策略。", "This produces a candidate improvement, not an automatic replacement."],
  validation: ["配置需要的回测、影子运行与修改幅度限制。", "Configure required backtests, shadow runs and limits on changes.", "这是验收要求，不是已通过的结果；真实报告在运行和复盘记录中查看。", "These are requirements, not passed results. Inspect actual run and review evidence."],
  approval: ["可编辑卡片名称和备注；接受或拒绝方案请打开版本与审批。", "Edit the card name and notes. Accept or reject proposals in Version & approval.", "不能把审批结果作为文本改成“已通过”。", "Approval outcomes cannot be edited into a passing state."],
  apply: ["可编辑说明；批准后的应用和回滚在版本管理中操作。", "Edit the notes. Apply approved versions or roll back in version management.", "历史版本和应用结果由系统记录，不能在这里改写。", "Versions and application outcomes are system records, not editable text."],
  observation: ["可编辑说明，打开实际记录查看应用后的效果。", "Edit the notes and open actual records to inspect outcomes after application.", "没有运行证据时保持未观测，不把流程定义标成成功。", "Without execution evidence, the stage remains unobserved rather than successful."],
};
export function cardGuide(node: WorkflowNode, t: WorkflowText) {
  const [zh, en, impactZh, impactEn] = GUIDES[node.kind];
  return { how: t(zh, en), impact: t(impactZh, impactEn) };
}
export function extraSourceFields(config: Record<string, unknown>) {
  const standard = new Set(["id", "name", "title", "description", "provider", "capability", "timeframe", "timeframes", "market", "markets", "sources", "source", "limit", "consumers", "account", "connection_id", "endpoint", "venue"]);
  return Object.fromEntries(Object.entries(config).filter(([key]) => !standard.has(key)));
}
export function editableSource(node: WorkflowNode) {
  return node.kind === "source" && node.binding.path?.[0] === "data_sources" && Object.keys(asObject(node.config)).length > 0;
}
