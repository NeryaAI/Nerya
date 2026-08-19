/**
 * Declarative workspace UI contract.
 *
 * The runtime owns the manifest (`ui/workspace.yml`).  The dashboard only
 * renders the small, allow-listed vocabulary below; a manifest can never
 * inject React, JavaScript, HTML, or an arbitrary URL into the shell.
 */

export type WorkspaceUiWidget = {
  id: string;
  kind: string;
  title?: string;
  description?: string;
  /** `full`, `half`, `third`, or a bounded integer column span. */
  span?: string | number;
  /** Widget-specific, read-only configuration. */
  config?: Record<string, unknown>;
  /** Alias accepted for manifests authored by older agents. */
  source?: Record<string, unknown>;
  [key: string]: unknown;
};

export type WorkspaceUiHome = {
  widgets: WorkspaceUiWidget[];
  title?: string;
  description?: string;
};

export type WorkspaceUiPage = {
  id: string;
  title: string;
  description?: string;
  icon?: string;
  widgets: WorkspaceUiWidget[];
  /** Optional navigation metadata; the backend also derives a safe href. */
  nav?: {
    label?: string;
    order?: number;
    section?: string;
    hidden?: boolean;
  };
  [key: string]: unknown;
};

export type WorkspaceUiManifest = {
  version: number | string;
  home: WorkspaceUiHome;
  pages: WorkspaceUiPage[];
  [key: string]: unknown;
};

export type WorkspaceUiWidgetKind = {
  kind: string;
  title?: string;
  description?: string;
  read_only?: boolean;
  source?: string;
  config_schema?: Record<string, unknown>;
  [key: string]: unknown;
};

export type WorkspaceUiCatalog = {
  widget_kinds: Array<WorkspaceUiWidgetKind | string>;
  [key: string]: unknown;
};

export type WorkspaceUiEnvelope = {
  ok: boolean;
  status?: "ok" | "warn" | "error" | "blocked" | string;
  source?: string;
  path?: string;
  revision?: number | string;
  manifest: WorkspaceUiManifest;
  catalog: WorkspaceUiCatalog;
  errors?: string[];
  warnings?: string[];
  [key: string]: unknown;
};

export type WorkspaceUiOperation = {
  op: string;
  [key: string]: unknown;
};

export type WorkspaceUiProposalRequest = {
  /** Full post-change manifest for advanced callers. */
  manifest?: WorkspaceUiManifest;
  /** Preferred conversational surface: small validated incremental changes. */
  patch?: { operations: WorkspaceUiOperation[] };
  /** Shorthand accepted by the runtime alongside `patch.operations`. */
  operations?: WorkspaceUiOperation[];
  /** Optimistic concurrency guard for the declarative UI manifest. */
  base_revision?: number;
  base_digest?: string;
  summary?: string;
  rationale?: string;
  actor_id?: string;
};

export type WorkspaceUiProposalResponse = WorkspaceUiEnvelope & {
  proposal_id?: string;
  diff?: Record<string, unknown>;
  state?: string;
};

export type WorkspaceUiApplyResponse = WorkspaceUiEnvelope & {
  proposal_id?: string;
  applied?: boolean;
  state?: string;
};

