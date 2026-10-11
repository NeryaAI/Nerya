import { copy as i18nCopy } from "./i18n";
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
  const messages: Record<string,string> = {
    service_auth_failed:"copy.lib_conversationError.001",
    model_auth_failed:"copy.lib_conversationError.002",
    backend_unreachable:"copy.lib_conversationError.003",
    context_limit:"copy.lib_conversationError.004",
    strategy_version_changed:"copy.lib_conversationError.005",
    request_timeout:"copy.lib_conversationError.006",
    model_stream_connection_failed:"copy.lib_conversationError.007",
    model_stream_provider_error:"copy.lib_conversationError.008",
    model_stream_protocol_error:"copy.lib_conversationError.009",
    model_stream_tls_error:"copy.lib_conversationError.010",
    model_stream_network_config_error:"copy.lib_conversationError.011",
    model_quota_exhausted:"copy.lib_conversationError.012",
    model_stream_interrupted:"copy.lib_conversationError.013",
    command_control_unavailable:"copy.lib_conversationError.014",
    command_lease_lost:"copy.lib_conversationError.015",
  };
  return { code, status, message: i18nCopy(zh, messages[code] ?? "") || commandErrorText(code,zh),
    needsSettings: ["service_auth_failed","model_auth_failed","context_limit","model_stream_provider_error","model_stream_tls_error","model_stream_network_config_error","model_quota_exhausted"].includes(code),
    // A failed provider response may follow completed local tools. Re-running
    // the entire request from the error card could repeat their side effects.
    canRerun: !["execution_unconfirmed","backend_unreachable","request_timeout","event_persistence_failed","delivery_unconfirmed","model_stream_connection_failed","model_stream_provider_error","model_stream_protocol_error","model_stream_tls_error","model_stream_network_config_error","model_quota_exhausted","model_stream_interrupted","command_control_unavailable","command_lease_lost"].includes(code),
    diagnostics: redactDiagnosticText(raw) };
}
