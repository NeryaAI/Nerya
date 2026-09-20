"use client";

import { useId, useRef, useState, type ReactNode } from "react";
import { useLocale } from "next-intl";
import { ChevronRightIcon } from "../icons";
import { WorkspaceTabs } from "./WorkspaceTabs";

// These are editable examples, not live market recommendations or execution modes.
const topics = [
  { id: "suggested", zh: "推荐问题", en: "Suggested" },
  { id: "research", zh: "市场研究", en: "Research" },
  { id: "strategy", zh: "策略验证", en: "Strategies" },
  { id: "review", zh: "持仓复盘", en: "Review" },
  { id: "team", zh: "团队协作", en: "Teamwork" },
];
type Starter = { topic: string; zh: string; en: string; promptZh: string; promptEn: string };
const starters: Starter[] = [
  { topic: "research", zh: "今天的市场有哪些值得关注的变化？", en: "What market changes should I pay attention to?", promptZh: "梳理近期市场的重要变化，区分已确认事实与推测，列明信息来源、时间和主要风险。没有实时数据时请明确说明，不要下单。", promptEn: "Review recent market changes. Separate confirmed facts from interpretations and include sources, timestamps and risks. State when live data is unavailable. Do not place trades." },
  { topic: "strategy", zh: "把我的交易想法变成可验证的策略", en: "Turn my trading idea into a testable strategy", promptZh: "帮我把交易想法整理成可验证的策略草案。先询问标的、周期、入场退出条件与风险预算，再设计回测步骤。区分回测与实盘，不部署或下单。", promptEn: "Help turn my trading idea into a testable strategy draft. First ask for the asset, timeframe, entry and exit rules and risk budget, then outline a backtest. Distinguish backtests from live trading. Do not deploy or place trades." },
  { topic: "review", zh: "检查我的持仓风险和需要确认的事项", en: "Review my portfolio risks and open questions", promptZh: "基于已连接账户或我提供的持仓记录，检查集中度、杠杆与主要风险，并列出需要我确认的事项。没有账户数据时先向我索取，不要假设仓位或执行交易。", promptEn: "Review concentration, leverage and risks using connected account data or positions I provide. List questions I need to confirm. Ask for missing data instead of inventing positions. Do not execute trades." },
  { topic: "team", zh: "让研究员和审阅员一起核查一份报告", en: "Have a researcher and reviewer verify a report", promptZh: "帮我核查一份报告。先确认报告内容，再让研究员整理来源、审阅员交叉检查结论；保留各自上下文，必要时相互补充消息，最后汇总一致结论、分歧和待办。", promptEn: "Help verify a report. First confirm the report, then have a researcher collect sources and a reviewer cross-check conclusions. Preserve their contexts and exchange clarifications when needed. Summarize agreements, disagreements and next steps." },
  { topic: "research", zh: "比较 BTC 和 ETH 的趋势与风险", en: "Compare the trends and risks for BTC and ETH", promptZh: "比较 BTC 与 ETH 的趋势、波动和风险，引用来源并注明数据时间。缺少行情时明确说明。给出观察条件而不是确定性收益承诺，不执行交易。", promptEn: "Compare BTC and ETH trends, volatility and risks with sources and data timestamps. Disclose missing market data. Provide conditions to watch rather than return guarantees. Do not trade." },
  { topic: "research", zh: "一条宏观新闻会怎样影响我的观察清单？", en: "How could a macro event affect my watchlist?", promptZh: "帮我分析一条宏观新闻对观察清单的影响。先确认新闻和标的，区分直接证据与情景推演，并列出需要继续核实的来源。", promptEn: "Help assess a macro event against my watchlist. First confirm the event and assets. Separate direct evidence from scenarios and list sources that need further verification." },
  { topic: "strategy", zh: "检查回测是否存在过拟合和费用遗漏", en: "Check a backtest for overfitting and missing costs", promptZh: "审阅我提供的回测报告，检查数据泄漏、样本外验证、交易费用、滑点和风险指标。缺少记录时先指出，不把历史表现当成实盘收益。", promptEn: "Review my backtest for data leakage, out-of-sample validation, fees, slippage and risk metrics. Identify missing records. Do not treat historical performance as live returns." },
  { topic: "strategy", zh: "先设计一套模拟执行的验证流程", en: "Plan a paper-trading validation workflow", promptZh: "帮我设计策略的模拟执行验证流程，明确输入、步骤、风控与审批条件。先输出方案供我审阅，不创建定时任务或启用实盘。", promptEn: "Design a paper-trading validation workflow with inputs, steps, risk controls and approval conditions. Produce a plan for review first. Do not create schedules or enable live trading." },
  { topic: "review", zh: "从我的成交记录中找出重复出现的问题", en: "Find recurring issues in my trade history", promptZh: "请根据我上传的成交记录做交易复盘，检查入场纪律、退出、费用和集中度，使用记录中的实际数据，提出可验证的改进建议。", promptEn: "Review the trade history I upload for entry discipline, exits, fees and concentration. Use only recorded data and propose improvements I can evaluate." },
  { topic: "review", zh: "整理一份带来源的账户复盘报告", en: "Create a sourced account review", promptZh: "整理已提供账户记录的复盘报告，区分已实现与未实现盈亏、模拟与实盘，列出数据范围、缺失项和风险。未经确认不执行账户操作。", promptEn: "Create a review of the account records provided. Distinguish realized from unrealized PnL and paper from live results. List the data window, gaps and risks. Do not perform account actions without confirmation." },
  { topic: "team", zh: "让两个成员分别分析支持和反对证据", en: "Compare supporting and opposing evidence", promptZh: "围绕我接下来提供的问题，让两个成员分别整理支持与反对证据，互相指出需要核实的地方，保留上下文，最后给出分歧与不确定性，不强行得出一致结论。", promptEn: "For the question I provide next, have two collaborators collect supporting and opposing evidence and exchange verification questions. Preserve context and report disagreements and uncertainty without forcing a consensus." },
  { topic: "team", zh: "延续已有成员的工作，不从头开始", en: "Continue an existing collaborator's work", promptZh: "先列出当前会话可复用的成员与已保存结果，确认我要继续的任务后，沿用原成员和上下文补充工作，不重复创建同名成员。", promptEn: "First list reusable collaborators and saved results in this conversation. Confirm the task to continue, then reuse the original collaborator and context rather than creating a duplicate." },
];

export function AgentStart({ composer, value, onChange, disabled = false }: {
  composer: ReactNode; value: string; onChange: (value: string) => void; disabled?: boolean;
}) {
  const zh = useLocale().startsWith("zh");
  const id = useId().replace(/:/g, "");
  const root = useRef<HTMLDivElement>(null);
  const [topic, setTopic] = useState("suggested");
  const [last, setLast] = useState<{ before: string; after: string } | null>(null);
  const visible = topic === "suggested" ? starters.slice(0, 4) : starters.filter((item) => item.topic === topic);
  function choose(item: Starter) {
    if (disabled) return;
    const prompt = zh ? item.promptZh : item.promptEn;
    const after = value.trimEnd().endsWith(prompt) ? value : value.trim() ? `${value.trimEnd()}\n\n${prompt}` : prompt;
    if (after !== value) { setLast({ before: value, after }); onChange(after); }
    root.current?.querySelector<HTMLTextAreaElement>("textarea")?.focus();
  }
  const canUndo = last && value === last.after;
  return <div ref={root} className="flex min-h-0 flex-1 overflow-y-auto" data-testid="agent-start">
    <div className="mx-auto my-auto w-full max-w-[860px] px-4 py-8 sm:px-8 sm:py-12">
      <div className="mb-7 text-left sm:mb-8">
        <h1 className="text-balance text-[28px] font-semibold tracking-tight leading-tight text-[color:var(--text-base)] sm:text-[36px]">{zh ? "今天想研究什么？" : "What would you like to explore?"}</h1>
        <p className="mt-3 max-w-[60ch] text-pretty text-sm leading-6 text-[color:var(--text-muted)]">{zh ? "研究市场、验证策略，或把一个任务交给你的 Agent 团队。" : "Research markets, test a strategy, or work through a task with your agent team."}</p>
      </div>
      {composer}
      <div className="mt-2 flex min-h-7 items-center justify-between gap-2 px-1 text-xs text-[color:var(--text-muted)]" role="status">
        <span>{canUndo ? (zh ? "已填入输入框，确认后再发送。" : "Added to your draft. Review it before sending.") : (zh ? "从下方选择问题，或直接描述你的目标。" : "Choose a question below, or describe your goal.")}</span>
        {canUndo ? <button type="button" disabled={disabled} className="min-h-8 shrink-0 rounded px-2 underline focus-visible:ring-2 focus-visible:ring-brand-400" onClick={() => { if (last && value === last.after) { onChange(last.before); setLast(null); root.current?.querySelector<HTMLTextAreaElement>("textarea")?.focus(); } }}>{zh ? "撤销填入" : "Undo"}</button> : null}
      </div>
      <div className="mt-5 [&_[role=tablist]]:border-0 [&_[role=tablist]]:px-0 [&_[role=tab]]:min-h-9 [&_[role=tab]]:rounded-full [&_[role=tab]]:border-0 [&_[role=tab]]:px-3 [&_[role=tab]]:text-xs [&_[aria-selected=true]]:bg-[color:var(--panel-bg)]">
        <WorkspaceTabs id={`start-${id}`} label={zh ? "起手问题主题" : "Starter topics"} tabs={topics.map((item) => ({ id: item.id, label: zh ? item.zh : item.en }))} value={topic} onChange={setTopic} />
      </div>
      {topics.map((item) => <section key={item.id} id={`start-${id}-panel-${item.id}`} role="tabpanel" aria-labelledby={`start-${id}-tab-${item.id}`} hidden={topic !== item.id} className={topic === item.id ? "mt-3" : "hidden"}>
        {topic === item.id ? <div className="grid gap-2 sm:grid-cols-2">{visible.map((question, index) => <button type="button" key={question.en} disabled={disabled} onClick={() => choose(question)} data-testid="starter-question"
          title={zh ? "加入输入框，发送前可编辑" : "Add to your draft and edit before sending"}
          className={`group flex min-h-16 w-full items-center gap-3 rounded-xl border border-[color:var(--line)] px-4 py-3 text-left text-[13px] leading-6 text-[color:var(--text-base)] transition-colors hover:border-[color:var(--line-hi)] hover:bg-[color:var(--card)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400 disabled:opacity-40 sm:text-sm ${visible.length === 3 && index === 2 ? "sm:col-span-2" : ""}`}>
          <span className="min-w-0 flex-1">{zh ? question.zh : question.en}</span><ChevronRightIcon size={14} className="shrink-0 text-[color:var(--text-muted)] motion-safe:transition-transform motion-safe:group-hover:translate-x-0.5" />
        </button>)}</div> : null}
      </section>)}
    </div>
  </div>;
}
