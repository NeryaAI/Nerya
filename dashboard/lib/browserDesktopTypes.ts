export interface BrowserNetworkRequest {
  id: string; seq: number; url: string; method: string; type: string;
  state: string; status: number | null; duration_ms: number; bytes: number;
  request_headers?: Record<string,string>; response_headers?: Record<string,string>;
  request_body?: string; body?: string; body_state?: string; next_offset?: number | null;
  challenge?: boolean; cached?: boolean; error?: string;
}

export interface DesktopExtension {
  path: string;
  digest: string;
  name: string;
  version: string;
  permissions: string[];
  extension_id: string;
  control_ui: boolean;
  enabled?: boolean;
}

export interface DesktopBrowserResponse {
  ok: boolean;
  error?: string;
  hint?: string;
  running?: boolean;
  closing?: boolean;
  paused?: boolean;
  human_control?: boolean;
  control_id?: string;
  sensitive?: boolean;
  challenge?: { state: string; provider?: string; url?: string };
  requests?: BrowserNetworkRequest[];
  request?: BrowserNetworkRequest;
  cursor?: number;
  generation?: string;
  reset?: boolean;
  has_more?: boolean;
  listening?: boolean;
  dropped?: number;
  attach_errors?: number;
  preferences?: { automatic: boolean };
  history?: { id: string; url: string; at: number }[];
  image?: string;
  upload?: { id: string; name: string; bytes: number };
  agent_access?: {
    enabled: boolean;
    occupied?: boolean;
    session_id?: string;
    executing?: boolean;
    all_web?: boolean;
    origins?: string[];
    expires_at?: number;
    revision?: number;
    uploads?: { id: string; name: string }[];
    last_actions?: { kind: string; action?: string; ok?: boolean }[];
  };
  tabs?: { id: string; url: string; selected: boolean; protected: boolean }[];
  config?: { id: string; version: number; extensions: DesktopExtension[] };
  review?: DesktopExtension;
  capabilities?: {
    playwright_installed: boolean;
    persistent_profile: boolean;
    credential_import: boolean;
    local_wallet_injection: boolean;
    passkey_provider: string;
  };
}
