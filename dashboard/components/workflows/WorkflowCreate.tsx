"use client";

import { useRef } from "react";
import { WorkflowEditorDialog } from "./WorkflowEditorDialog";
import { useWorkflowText } from "./WorkflowCanvas";
import ui from "./WorkflowNative.module.css";
import styles from "./WorkflowVerification.module.css";

/** Examples fill editable human requests. They never generate strategy code. */
export function WorkflowCreate({ prompt, onChange, onClose, onContinue }: {
  prompt: string; onChange: (text: string) => void; onClose: () => void; onContinue: () => void;
}) {
  const t = useWorkflowText();
  const input = useRef<HTMLTextAreaElement>(null);
  const examples = [
    [t("价格提醒", "Price alerts"), t("帮我盯一下比特币，15分钟收盘价站上20周期均线时提醒我，不用AI分析，也不要下单。", "Watch Bitcoin and alert me when the 15-minute close rises above its 20-period average. No AI analysis or orders.")],
    [t("信号出现才分析", "Analyze only on a signal"), t("比特币MACD金叉时才让AI看看机会和风险，没金叉就别调用AI，同一根K线别重复分析，不要下单。", "Ask AI to analyze Bitcoin's opportunities and risks only on a MACD bullish crossover. No AI without a signal, no duplicate candle analysis and no orders.")],
    [t("每天看一次", "A daily overview"), t("每天北京时间早上9点帮我看看比特币，简单说说机会和风险，不用脚本筛选，也不要下单。", "Give me a simple Bitcoin opportunity and risk overview every day at 9 a.m. Beijing time, without script filtering or orders.")],
  ];
  return <WorkflowEditorDialog open title={t("创建策略", "Create a strategy")} onClose={onClose} footer={<><span className={ui.muted}>{t("先在模拟环境验证", "Validate in paper first")}</span><span className={ui.spacer} /><button type="button" className={ui.reviewButton} disabled={!prompt.trim()} onClick={onContinue}>{t("交给 Agent 创建", "Create with Agent")} →</button></>}>
    <section className={styles.root} data-testid="workflow-create"><header className={styles.heading}><div><h2>{t("你想让策略做什么？", "What should your strategy do?")}</h2><p>{t("说清楚看什么、何时行动、你想得到什么。其余交给 Agent。", "Describe what to watch, when to act, and the result you need.")}</p></div><button className={ui.iconButton} type="button" aria-label={t("关闭创建", "Close creation")} onClick={onClose}>×</button></header>
      <textarea ref={input} className={styles.prompt} aria-label={t("描述你的策略", "Describe your strategy")} placeholder={t("例如：帮我观察比特币，有明显变化时分析一下，先别交易。", "For example: watch Bitcoin and analyze significant changes, without trading yet.")} rows={4} maxLength={5000} value={prompt} onChange={(event) => onChange(event.target.value)} />
      <p className={styles.exampleHint}>{t("也可以从一句话开始", "Or start with an example")}</p><div className={styles.examples}>{examples.map(([label, text]) => <button type="button" key={label} onClick={() => { onChange(text); input.current?.focus(); }}><span>{label}</span><span aria-hidden="true">↗</span></button>)}</div>
      <p className={styles.note}>{t("示例只填写需求，不套用代码模板。下一步进入 Agent 对话草稿，确认发送后开始创建；不会自动开实盘。", "Examples fill the request, not code templates. Continue to an Agent chat draft and send it to start; live mode is not enabled automatically.")}</p>
    </section>
  </WorkflowEditorDialog>;
}
