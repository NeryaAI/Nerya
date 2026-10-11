"use client";
import { copy as i18nCopy } from "../../lib/i18n";

import { useLocale } from "next-intl";
import type { InteractionReceipt as Receipt } from "../../lib/toolOutputPresentation";
import { CheckIcon } from "../icons";
import styles from "./ExecutionTimeline.module.css";

export function InteractionReceipt({ receipt }: { receipt: Receipt }) {
  const zh = useLocale().startsWith("zh");
  const labels: Record<string,string> = { answer: i18nCopy(zh, "copy.components_chat_InteractionReceipt.001"), accept: i18nCopy(zh, "copy.components_chat_InteractionReceipt.002"), revise: i18nCopy(zh, "copy.components_chat_InteractionReceipt.003"), reject: i18nCopy(zh, "copy.components_chat_InteractionReceipt.004") };
  return <section className={styles.receipt} data-testid="interaction-receipt" data-find-text>
    <div className={styles.receiptTitle}><CheckIcon size={14}/><span>{labels[receipt.action] || labels.answer}</span><span className={styles.caption}>{receipt.title}</span></div>
    <dl className={styles.answerList}>{receipt.rows.map(row => <div key={row.id}><dt>{row.question}</dt><dd>{row.answers.length ? row.answers.map((answer, i) => <span key={i}>{answer}</span>) : <span className={styles.caption}>{i18nCopy(zh, "copy.components_chat_InteractionReceipt.005")}</span>}</dd></div>)}</dl>
    {receipt.note && <p className={styles.receiptNote}>{receipt.note}</p>}
    <p className={styles.caption}>{i18nCopy(zh, "copy.components_chat_InteractionReceipt.006")}</p>
  </section>;
}
