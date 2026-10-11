'use client';
import { copy as i18nCopy } from "../../lib/i18n";


import { useEffect, useState } from 'react';
import { useLocale } from 'next-intl';
import type { ModelRetry } from '../../lib/modelRetry';

export function ModelRetryStatus({ retry }: { retry: ModelRetry }) {
  const zh = useLocale().startsWith('zh');
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    setNow(Date.now());
    if (retry.state !== 'waiting') return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [retry.retry_at, retry.state]);
  const remaining = Math.max(0, Math.ceil(retry.retry_at - now / 1000));
  const waiting = retry.state === 'waiting' && remaining > 0;
  const reason = retry.status_code === 429
    ? (i18nCopy(zh, "copy.components_chat_ModelRetryStatus.001"))
    : (i18nCopy(zh, "copy.components_chat_ModelRetryStatus.002"));
  const action = waiting
    ? (i18nCopy(zh, "copy.components_chat_ModelRetryStatus.003", { value0: remaining }))
    : (i18nCopy(zh, "copy.components_chat_ModelRetryStatus.004"));
  return <div role="status" aria-live="polite" data-testid="model-retry-status"
    className="mb-2 flex flex-wrap items-center gap-2 py-1 text-xs text-[color:var(--text-muted)]">
    <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-current motion-safe:animate-pulse" />
    <span>{reason}</span><span className="tabular-nums">{action} · {retry.attempt}/{retry.max_attempts}</span>
  </div>;
}
