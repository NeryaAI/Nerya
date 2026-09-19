"use client";

import { useEffect, useRef, useState } from "react";
import { callApi } from "../../lib/clientApi";
import { confirm } from "../../lib/dialogs";
import { useWorkflowText } from "./WorkflowCanvas";
import ui from "./WorkflowNative.module.css";
import styles from "./ContinuousStrategyStatus.module.css";

type Service = {
  ok: boolean; state: string; error?: string; connection?: string;
  agent_active?: boolean; queue_depth?: number; accepted_events?: number;
  rejected_events?: number; restart_count?: number; last_message_at?: number;
  last_error?: string; stop_reason?: string;
  last_event?: { event_id: string; status: string; session_id?: string };
};
const activeStates = new Set(["starting", "running", "restarting", "stopping", "unresponsive"]);

export function ContinuousStrategyStatus({ strategyId, proposalId, dirty }: {
  strategyId: string; proposalId?: string | null; dirty: boolean;
}) {
  const t = useWorkflowText();
  const [service, setService] = useState<Service | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const mounted = useRef(true);
  const serial = useRef(0);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; serial.current++; }; }, []);
  useEffect(() => {
    if (proposalId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      const request = ++serial.current;
      try {
        const next = await callApi<Service>(`/strategies/runtime/service/status?strategy_id=${encodeURIComponent(strategyId)}`, { signal: controller.signal });
        if (!next.ok) throw new Error(next.error || "Runtime status unavailable");
        if (request === serial.current && !controller.signal.aborted) { setService(next); setError(""); }
      } catch (reason) {
        if (request === serial.current && !controller.signal.aborted) setError(String(reason));
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(poll, 2000);
      }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [strategyId, proposalId, refresh]);

  async function control(action: "start" | "stop") {
    setBusy(true);
    try {
      let hash: string | undefined;
      if (action === "start") {
        const current = await callApi<{ ok: boolean; package_hash: string; error?: string; manifest: { mode?: string } }>(
          `/strategies/runtime/status?strategy_id=${encodeURIComponent(strategyId)}`, { signal: new AbortController().signal });
        if (!current.ok || !current.package_hash) throw new Error(current.error || "Current strategy version unavailable");
        hash = current.package_hash;
        const approved = await confirm({ title: t("启动常驻监听？", "Start continuous listener?"), tone: "warning",
          message: t(`将运行当前 ${current.manifest.mode || "paper"} 版本。符合条件的事件可唤醒策略 Agent，并在账户授权和风控允许范围内自主下单。不是单次测试。`,
            `Start the current ${current.manifest.mode || "paper"} version. Qualifying events can invoke the strategy Agent and place orders within account permissions and risk limits. This is not a single test.`),
          okLabel: t("启动监听", "Start listener") });
        if (!approved || !mounted.current) return;
      }
      const result = await callApi<Service>(`/strategies/runtime/service/${action}`, {
        method: "POST", body: { strategy_id: strategyId, ...(hash ? { expected_hash: hash } : {}) },
      });
      if (!result.ok) throw new Error(result.error || "Service action failed");
      if (mounted.current) { serial.current++; setService(result); setError(""); }
    } catch (reason) { if (mounted.current) setError(String(reason)); }
    finally { if (mounted.current) { setBusy(false); setRefresh((value) => value + 1); } }
  }
  const labels: Record<string, string> = {
    starting: t("启动中", "Starting"), running: t("运行中", "Running"), restarting: t("恢复中", "Restarting"),
    stopping: t("正在停止", "Stopping"), stopped: t("已停止", "Stopped"), finished: t("监听已结束", "Listener finished"),
    failed: t("运行失败", "Failed"), interrupted: t("进程已中断", "Interrupted"), unresponsive: t("进程无响应", "Unresponsive"),
  };
  const connection = ({ connected: t("已连接", "Connected"), connecting: t("连接中", "Connecting"), reconnecting: t("重新连接中", "Reconnecting"), disconnected: t("未连接", "Disconnected"), idle: t("待连接", "Idle") } as Record<string, string>)[service?.connection || ""];
  const active = !!service && activeStates.has(service.state);
  return <section className={styles.root} data-testid="continuous-strategy-status" aria-label={t("常驻脚本状态", "Continuous script status")}>
    <div className={styles.line}><strong>{t("常驻脚本", "Continuous script")}</strong>
      <span role="status" className={styles.state} data-state={error ? "unknown" : service?.state || "stopped"}>
        {proposalId ? t("待审候选 · 未运行", "Candidate · not running") : error ? t("状态不可用", "Status unavailable") : labels[service?.state || ""] || t("读取状态…", "Reading status…")}
      </span><span className={styles.grow} />
      {!proposalId && <><button type="button" className={ui.quietButton} disabled={busy || dirty || !!error || !service || active} onClick={() => void control("start")}>{t("启动监听", "Start listener")}</button>
      <button type="button" className={ui.quietButton} disabled={busy || (!active && !error)} onClick={() => void control("stop")}>{t("停止监听", "Stop listener")}</button></>}
    </div>
    <p className={styles.note}>{proposalId ? t("审核并应用后才能启动。候选不会继承当前版本的运行状态。", "Apply this reviewed candidate before starting. It does not inherit the active version’s running state.") :
      t("持续接收事件，不依赖定时任务。停止会取消待处理工作；已提交的订单不会自动撤销或平仓。", "Receives events continuously without a timer. Stop cancels pending work, not submitted orders or existing positions.")}</p>
    {!proposalId && service && !error && <div className={styles.metrics}>
      {connection && <span>WebSocket · {connection}</span>}
      <span>Agent · {service.agent_active ? t("处理中", "Processing") : t("等待事件", "Waiting for events")}</span>
      <span>{t("排队", "Queued")} {service.queue_depth ?? 0}</span>
      <span>{t("接收", "Accepted")} {service.accepted_events ?? 0} / {t("拦截", "Filtered")} {service.rejected_events ?? 0}</span>
      {!!service.restart_count && <span>{t("重启", "Restarts")} {service.restart_count}</span>}
      {service.last_message_at && <span>{t("最近消息", "Last message")} {new Date(service.last_message_at * 1000).toLocaleTimeString()}</span>}
    </div>}
    {(error || service?.last_error) && <p className={styles.error} role="alert">{error || service?.last_error}<button type="button" className={ui.quietButton} onClick={() => setRefresh((n) => n + 1)}>{t("刷新", "Refresh")}</button></p>}
    {service?.stop_reason && !active && <p className={styles.note}>{t("停止原因", "Stop reason")} · {service.stop_reason}</p>}
    {service?.last_event && <details className={styles.note}><summary>{t("最近事件回执", "Latest event receipt")} · {service.last_event.status}</summary><p>{service.last_event.event_id}<br />{service.last_event.session_id}</p></details>}
  </section>;
}
