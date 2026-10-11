import { commandErrorText } from "./commandCopy";

/** Keep debugging useful without copying credentials into visible diagnostics. */
export function redactDiagnosticText(value: string): string {
  return value
    .replace(/\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+/gi, "$1 [redacted]")
    .replace(/(["']?(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|passphrase|secret)["']?\s*[:=]\s*)(["'])(.*?)\2/gi, "$1$2[redacted]$2")
    .replace(/(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passphrase|secret)\s*[:=]\s*)[^\s,;"'}]+/gi, "$1[redacted]")
    .replace(/(https?:\/\/)[^\s/@:]+:[^\s/@]+@/gi, "$1[redacted]@")
    .slice(0, 20000);
}
export function conversationError(raw: string, zh: boolean) {
  let code = "", status: number | undefined;
  try {
    const data: unknown = JSON.parse(raw);
    if (data && typeof data === "object") {
      const error = data as { code?: unknown; status_code?: unknown };
      if (typeof error.code === "string") code = error.code;
      if (typeof error.status_code === "number") status = error.status_code;
    }
  } catch { /* Older transcripts contain transport error strings. */ }
  if(code==="turn_failed" && (status===401||status===403))code="model_auth_failed";
  if(code==="turn_failed" && status===429)code="rate_limited";
  if (!code) {
    if (/insufficient[ _](?:balance|credit|quota)|no resource package|余额不足|账户欠费/i.test(raw)) code = "model_quota_exhausted";
    else if (/429|rpm\s+exhausted|rate[_ -]?limit/i.test(raw)) { code = "rate_limited"; status = 429; }
    else if (/unauthorized|HTTP\s+(401|403)/i.test(raw)) code = "service_auth_failed";
    else if (/api\s+error\s*\((401|403)\)|invalid[_ -]api[_ -]key/i.test(raw)) code = "model_auth_failed";
    else if (/upstream_unreachable|ECONNREFUSED/i.test(raw)) code = "backend_unreachable";
    else if (/context.*(exceed|large)|maximum.*context|context_length/i.test(raw)) code = "context_limit";
    else if (/timeout|ETIMEDOUT|504/i.test(raw)) code = "request_timeout";
    else code = "turn_failed";
  }
  const messages: Record<string,[string,string]> = {
    model_quota_exhausted:["模型账户余额或资源包不足，等待重试无法恢复。请在模型设置中更换可用模型或处理账户额度；已执行步骤不会自动重放。","The model account has insufficient credit or no resource package. Waiting will not fix this. Check billing or select an available model; completed actions are not replayed."],
    model_stream_interrupted:["模型流式响应中途断开，部分内容已保留，但本轮未完成。为避免重复写入或交易，未自动重放；请先核对已完成步骤。","The model stream stopped after output began. Partial output is retained, but the task is incomplete. Review completed steps before continuing; writes and trades were not replayed."],
    model_stream_connection_failed:["模型连接中断，有限次自动重试已结束。请检查模型服务连接；之前完成的工具步骤不会自动重放。","The model connection failed and bounded retries have ended. Check provider connectivity; earlier tool steps will not be replayed automatically."],
    model_stream_provider_error:["模型服务返回流式错误。请检查所选模型和服务状态；这不是本地登录或 MCP 授权失败。","The provider returned a stream error. Check the selected model and provider; this is not a local login or MCP authorization failure."],
    model_stream_protocol_error:["模型返回了不完整或无效的流式数据。未执行残缺的工具调用，请检查模型兼容性。","The provider returned incomplete or invalid stream data. Incomplete tool calls were not executed; check model compatibility."],
    model_stream_tls_error:["模型连接的 TLS 证书校验失败。请检查地址、证书或代理设置；不会通过关闭校验来重试。","The model connection failed TLS verification. Check its endpoint, certificate or proxy settings; certificate verification remains enabled."],
    model_stream_network_config_error:["模型网络配置无法建立连接。请检查服务地址、DNS 或代理设置。","The model network configuration could not establish a connection. Check its endpoint, DNS or proxy configuration."],
    command_control_unavailable:["任务控制暂时无法访问数据库，系统已暂停执行；不是你主动停止。请先核对当前状态与已完成步骤，不要重复提交。","Task control lost database access and execution was stopped by the system, not by you. Reconcile its status and completed steps before submitting again."],
    command_lease_lost:["任务执行租约已丢失，当前结果需要核对。系统不会自动重复执行已提交操作。","The task execution lease was lost. Reconcile the result first; submitted actions will not be replayed automatically."],
    service_auth_failed:["本地服务鉴权未通过。本轮没有成功完成，请检查服务与界面使用的登录配置。","Service authentication failed. Check that the dashboard and backend use the same access configuration."],
    model_auth_failed:["模型提供方拒绝了认证。请检查模型配置中的密钥；本轮不会继续自动重试。","The model provider rejected authentication. Check its credentials; automatic retries have ended."],
    backend_unreachable:["界面暂时无法连接本地服务。已发送的任务可能仍在执行，请先检查服务状态。","The dashboard cannot reach the local service. Accepted work may still be running; check its status first."],
    context_limit:["请求超过模型实际接受的上下文限制。已完成步骤保留，请检查模型窗口配置或减少引用后重新运行。","The request exceeded the model's accepted context limit. Completed work is retained; review its window configuration or references."],
    strategy_version_changed:["策略在排队期间已变化。请检查新版后再提交。","The strategy changed while queued. Review the new version before submitting again."],
    request_timeout:["请求等待超时。超时不表示后台执行已撤销，请先核对本轮状态。","The request timed out. This does not undo background execution; reconcile its status first."],
  };
  return { code, status, message: messages[code]?.[zh ? 0 : 1] || commandErrorText(code,zh),
    needsSettings: ["service_auth_failed","model_auth_failed","context_limit","model_quota_exhausted","model_stream_provider_error","model_stream_tls_error","model_stream_network_config_error"].includes(code),
    canRerun: !["execution_unconfirmed","backend_unreachable","request_timeout","event_persistence_failed","delivery_unconfirmed","command_control_unavailable","command_lease_lost","model_quota_exhausted","model_stream_interrupted","model_stream_connection_failed","model_stream_provider_error","model_stream_protocol_error","model_stream_tls_error","model_stream_network_config_error"].includes(code),
    diagnostics: redactDiagnosticText(raw) };
}
