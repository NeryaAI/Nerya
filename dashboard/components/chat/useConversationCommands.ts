"use client";

import { useEffect, useRef, useState } from "react";
import type { LiveEvent } from "../../lib/chat";
import { ApiError } from "../../lib/clientApi";
import { mergeStreamEvents } from "../../lib/streamEvents";
import { controlConversationGoal, goalFromSnapshot, GoalControlError, type ConversationGoal, type GoalAction } from "../../lib/conversationGoal";
import { getWorkspaceIdentity, useWorkspaceIdentity, workspaceGeneration } from "../../lib/workspaceIdentity";
import { COMMAND_PENDING_EVENT, commandControl, commandEvents, commandGet, commandSettled, commandSubmitting,
  commandSnapshot, CommandClientError, pendingCommands, runningCommand, settlePending, submitCommand,
  type CommandSnapshot, type ConversationCommand, type PendingCommand } from "../../lib/conversationCommands";

export type Reconciliation = { status: "matched" | "insufficient_evidence" | "lookup_failed";
  command_id: string; session_id: string; turn_id: string | null; source?: string | null; evidence_path: string };

export function settleSnapshotPending(sessionId: string, commands: ConversationCommand[]) {
  const received = new Set(commands.filter(command => command.session_id === sessionId).map(command => command.command_id));
  for (const item of pendingCommands(sessionId)) {
    if (item.session_id === sessionId && received.has(item.command_id)) settlePending(item.command_id, item.workspace_id);
  }
}

/** A route owns an observer, never the runtime. Server commands survive navigation. */
export function useConversationCommands(sessionId: string | undefined,
  onProjection: (command: ConversationCommand, events: LiveEvent[]) => void) {
  const workspace = useWorkspaceIdentity(), identityGeneration = workspaceGeneration();
  const ownsWorkspace = () => Boolean(workspace && workspace === getWorkspaceIdentity() && identityGeneration === workspaceGeneration());
  const sink = useRef(onProjection); sink.current = onProjection;
  const [state, setState] = useState<{ session: string; workspace: string | null; identityGeneration: number; data: (CommandSnapshot & { goal?: ConversationGoal | null }) | null; connection: "connecting" | "online" | "offline" | "incompatible" }>({ session: "", workspace: null, identityGeneration: -1, data: null, connection: "connecting" });
  const [pending, setPending] = useState<PendingCommand[]>([]);
  const refreshRef = useRef<() => void>(() => {});
  const generation = useRef(0);
  const currentSession = useRef(sessionId); currentSession.current = sessionId;
  useEffect(() => {
    const update = () => setPending(sessionId && ownsWorkspace() ? pendingCommands(sessionId).filter(item => !commandSubmitting(item)) : []);
    update();
    window.addEventListener(COMMAND_PENDING_EVENT, update);
    window.addEventListener("storage", update);
    return () => { window.removeEventListener(COMMAND_PENDING_EVENT, update); window.removeEventListener("storage", update); };
  }, [sessionId, workspace, identityGeneration]);

  useEffect(() => {
    const stamp = ++generation.current;
    if (!sessionId || !ownsWorkspace()) { setState({ session: "", workspace, identityGeneration, data: null, connection: "connecting" }); return; }
    const controller = new AbortController();
    const cursor = new Map<string, number>();
    const seen = new Map<string, number>();
    const drained = new Set<string>();
    const results = new Map<string, number>();
    const needsResult = (command: ConversationCommand) => commandSettled(command) && command.has_result &&
      !command.result && results.get(command.command_id) !== command.revision;
    const hydrating = new Map<string, { revision: number; controller: AbortController }>();
    let busy = false, queuedRefresh = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let latest: CommandSnapshot | null = null;
    // Effect cleanup owns session changes. A concurrent, abandoned render can
    // mutate currentSession without committing and must not kill this observer.
    const valid = () => !controller.signal.aborted && generation.current === stamp && ownsWorkspace() && currentSession.current === sessionId;
    async function hydrate(summary: ConversationCommand) {
      const cid = summary.command_id;
      const pending = hydrating.get(cid);
      if (pending?.revision === summary.revision) return;
      pending?.controller.abort();
      hydrating.delete(cid);
      const request = { revision: summary.revision, controller: new AbortController() };
      hydrating.set(cid, request);
      const current = () => valid() && !request.controller.signal.aborted &&
        latest?.commands.some(row => row.command_id === cid && row.session_id === sessionId && row.turn_id === command.turn_id && row.revision <= command.revision);
      let command = summary;
      const events: LiveEvent[] = [];
      const publish = () => {
        request.revision = command.revision;
        // Detail GETs can advance past the summary that started hydration. Keep
        // that authoritative revision for both live callbacks and later polls.
        const merge = (row: ConversationCommand) => row.command_id === cid && row.session_id === sessionId &&
          row.turn_id === command.turn_id && row.revision <= command.revision
          ? { ...command, hydrated_events: mergeStreamEvents(row.hydrated_events || [], events) } : row;
        if (latest) latest = { ...latest, commands: latest.commands.map(merge) };
        setState(previous => {
          if (previous.session !== sessionId || previous.workspace !== workspace || previous.identityGeneration !== identityGeneration || !previous.data) return previous;
          return { ...previous, data: { ...previous.data, commands: previous.data.commands.map(merge) } };
        });
        sink.current(command, events);
      };
      const readDetail = async () => {
        const detail = (await commandGet(sessionId!, cid, request.controller.signal)).command;
        if (!current() || detail.session_id !== sessionId || detail.command_id !== cid ||
          detail.turn_id !== command.turn_id || detail.revision < command.revision) return false;
        command = detail;
        publish();
        if (command.result) results.set(cid, command.revision);
        return true;
      };
      let nextCursor = cursor.get(cid) || 0;
      try {
        if (needsResult(summary)) {
          if (!await readDetail()) return;
          // has_result promises a producer result, not a drained event stream.
          // A temporarily missing detail must not settle this revision forever.
          if (!command.result) return;
        }
        // A terminal result does not replace its durable event log. A fresh
        // route must replay that log so tool activity survives reloads.
        let complete = commandSettled(command) && Boolean(command.result) && drained.has(cid);
        if (!drained.has(cid)) {
          // Commit the cursor only after projection, so a failed later
          // page cannot discard an earlier batch that was never displayed.
          for (let page = 0; page < 4; page++) {
            if (!current()) return;
            const response = await commandEvents(sessionId!, cid, nextCursor, request.controller.signal);
            if (!current()) return;
            events.push(...response.events);
            nextCursor = response.cursor;
            // A subscription can reach the producer outcome before its running
            // summary catches up. Read the command's top-level result now.
            if (runningCommand(command) && response.events.some(event => event.kind === "command.outcome" &&
              event.command_id === cid && event.session_id === sessionId && event.turn_id === command.turn_id)) {
              if (!await readDetail()) return;
            }
            if (!response.has_more) { complete = commandSettled(command); break; }
          }
        }
        if (!current()) return;
        if (command !== summary || events.length) publish();
        cursor.set(cid, nextCursor);
        seen.set(cid, command.revision);
        if (command.result) results.set(cid, command.revision);
        if (complete) drained.add(cid);
      } catch {
        // Hydration is retried after the next snapshot; it never owns liveness.
      } finally {
        if (hydrating.get(cid) === request) hydrating.delete(cid);
      }
    }
    async function tick() {
      if (!valid()) return;
      if (busy) { queuedRefresh = true; return; }
      if (timer) clearTimeout(timer);
      busy = true;
      try {
        const response = await commandSnapshot(sessionId!, controller.signal);
        if (!valid()) return;
        if (!Array.isArray(response.commands) || !response.queue) throw new Error("Invalid command snapshot");
        const snapshot = { ...response, commands: response.commands.map(summary => {
          const prior = latest?.commands.find(row => row.command_id === summary.command_id &&
            row.session_id === summary.session_id && row.turn_id === summary.turn_id);
          return prior && prior.revision > summary.revision ? prior : summary;
        }) };
        latest = snapshot;
        for (const [cid, request] of hydrating) {
          if (!snapshot.commands.some(command => command.command_id === cid && command.session_id === sessionId && command.revision === request.revision)) {
            request.controller.abort(); hydrating.delete(cid);
          }
        }
        settleSnapshotPending(sessionId!, snapshot.commands);
        setState(previous => {
          const owned = previous.session === sessionId && previous.workspace === workspace && previous.identityGeneration === identityGeneration;
          // Keep hydrated results in the state consumed by ChatView's transcript
          // projection. A later summary must not erase same-revision evidence.
          const commands = snapshot.commands.map(summary => {
            const prior = owned ? previous.data?.commands.find(row => row.command_id === summary.command_id && row.session_id === summary.session_id && row.turn_id === summary.turn_id) : undefined;
            return { ...summary,
              hydrated_events: prior?.hydrated_events,
              result: summary.has_result && !summary.result && prior?.revision === summary.revision && prior.state === summary.state && prior.result ? prior.result : summary.result };
          });
          return { session: sessionId!, workspace, identityGeneration,
            data: { ...snapshot, commands, goal: goalFromSnapshot(snapshot, sessionId!, owned ? goalFromSnapshot(previous.data, sessionId!) : null) }, connection: "online" };
        });
        for (const summary of snapshot.commands) {
          if (!valid()) return;
          if (summary.session_id !== sessionId) continue;
          const cid = summary.command_id;
          if (summary.kind === "guide" || ["queued","removed"].includes(summary.state)) continue;
          const changed = seen.get(cid) !== summary.revision;
          if (!changed && !runningCommand(summary) && drained.has(cid) && !needsResult(summary)) continue;
          // Admission/state comes from the snapshot, independently of event or
          // result hydration. A slow stream must not hide an accepted command.
          if (changed) sink.current(summary, []);
        }
        // Reserve transport capacity for summaries; current work takes priority
        // over historical details regardless of snapshot ordering.
        const details = snapshot.commands.filter(summary => summary.session_id === sessionId && summary.kind !== "guide" &&
          !["queued", "removed"].includes(summary.state) &&
          (seen.get(summary.command_id) !== summary.revision || runningCommand(summary) || !drained.has(summary.command_id) || needsResult(summary)))
          .sort((a, b) => Number(runningCommand(b)) - Number(runningCommand(a)) || b.created_at - a.created_at).slice(0, 2);
        for (const [cid, request] of hydrating) {
          if (!details.some(command => command.command_id === cid)) { request.controller.abort(); hydrating.delete(cid); }
        }
        for (const summary of details) {
          if (!valid()) return;
          // Polling durable state must progress while events/results hydrate.
          void hydrate(summary);
        }
      } catch(error) {
        if (valid()) setState(previous => ({ session: sessionId!, workspace, identityGeneration, data: previous.session === sessionId && previous.workspace === workspace && previous.identityGeneration === identityGeneration ? previous.data : null, connection: error instanceof ApiError && [404,405].includes(error.status) ? "incompatible" : "offline" }));
      } finally {
        busy = false;
        if (valid()) {
          const active = latest?.commands.some(runningCommand);
          const delay = queuedRefresh ? 0 : document.hidden ? 5000 : active ? 500 : 2000;
          queuedRefresh = false;
          timer = setTimeout(tick, delay);
        }
      }
    }
    refreshRef.current = () => { if (!document.hidden) void tick(); };
    const visible = () => { if (!document.hidden) void tick(); };
    document.addEventListener("visibilitychange", visible);
    window.addEventListener("online", visible);
    void tick();
    return () => {
      controller.abort(); if (timer) clearTimeout(timer);
      for (const request of hydrating.values()) request.controller.abort();
      document.removeEventListener("visibilitychange", visible); window.removeEventListener("online", visible);
      refreshRef.current = () => {};
    };
  }, [sessionId, workspace, identityGeneration]);

  const ownsState = ownsWorkspace() && state.session === sessionId && state.workspace === workspace && state.identityGeneration === identityGeneration;
  const data = ownsState ? state.data : null;
  const goal = sessionId ? goalFromSnapshot(data, sessionId) : null;
  const commands = data?.commands || [];
  async function control(action: string, command?: ConversationCommand, extra: Record<string, unknown> = {}) {
    if (!sessionId || !data || !ownsWorkspace() || (command && command.session_id !== sessionId)) throw new CommandClientError("connection_lost");
    try {
      const response=await commandControl(sessionId, action, command?.revision ?? data.queue.revision, command?.command_id, extra);
      if (!ownsWorkspace() || currentSession.current !== sessionId) throw new CommandClientError("workspace_changed", true);
      if(Array.isArray(response.commands))setState(previous => ({session:sessionId,workspace,identityGeneration,
        data:{...response,goal:goalFromSnapshot(response,sessionId,goalFromSnapshot(previous.data,sessionId))},connection:"online"}));
      return response as typeof response & { reconciliation?: Reconciliation };
    } finally { if (ownsWorkspace() && currentSession.current === sessionId) refreshRef.current(); }
  }
  async function controlGoal(action: GoalAction, expected: ConversationGoal) {
    if (!sessionId || !goal || !ownsState || !ownsWorkspace() || state.connection !== "online" ||
      currentSession.current !== sessionId || expected.session_id !== sessionId || expected.goal_id !== goal.goal_id || expected.revision !== goal.revision) {
      throw new GoalControlError("unavailable");
    }
    const stamp = generation.current;
    try {
      const confirmed = await controlConversationGoal(expected, action);
      if (!ownsWorkspace() || currentSession.current !== sessionId || generation.current !== stamp) throw new GoalControlError("workspaceChanged");
      setState(previous => {
        if (previous.session !== sessionId || previous.workspace !== workspace || previous.identityGeneration !== identityGeneration || !previous.data) return previous;
        const current = goalFromSnapshot(previous.data, sessionId);
        if (current && (current.goal_id !== confirmed.goal_id || current.revision > confirmed.revision)) return previous;
        return {...previous,data:{...previous.data,goal:confirmed}};
      });
      return confirmed;
    } finally { if (ownsWorkspace() && currentSession.current === sessionId && generation.current === stamp) refreshRef.current(); }
  }
  async function recover(item: PendingCommand, resend = false) {
    const valid = () => ownsWorkspace() && item.workspace_id === workspace && item.session_id === sessionId && currentSession.current === sessionId;
    if (!valid()) throw new CommandClientError("workspace_mismatch");
    try {
      const found = await commandGet(item.session_id, item.command_id);
      if (!valid()) throw new CommandClientError("workspace_changed", true);
      if (found.command.session_id !== item.session_id || found.command.command_id !== item.command_id) throw new CommandClientError("receipt_invalid", true);
      settlePending(item.command_id, item.workspace_id);
      if (currentSession.current === item.session_id) sink.current(found.command, []);
    } catch (error) {
      // Not-found is not proof the user intended another command. Explicit
      // resend preserves the original key and cannot multiply admission.
      if (!valid() || !(error instanceof ApiError && error.status === 404) || !resend) throw error;
      const accepted = await submitCommand(item.session_id, item.command_type, item.request, item);
      if (!valid()) throw new CommandClientError("workspace_changed", true);
      if (currentSession.current === item.session_id) sink.current(accepted, []);
    } finally { if (ownsWorkspace() && currentSession.current === sessionId) refreshRef.current(); }
  }
  return { data, commands, goal, controlGoal, active: commands.find(runningCommand),
    pending: ownsWorkspace() ? pending.filter(item => item.workspace_id === workspace && item.session_id === sessionId) : [],
    connection: ownsState ? state.connection : "connecting",
    refresh: () => { if (ownsWorkspace()) refreshRef.current(); }, control, recover };
}
