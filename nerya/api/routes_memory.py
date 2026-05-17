from __future__ import annotations

from typing import Any

from ..memory import memsearch_index
from ..memory.activity import MemoryActivityLog
from ..memory.notebook import VALID_TARGETS as NOTEBOOK_VALID_TARGETS
from ..memory.write_rules import (
    DEDUPE_STRATEGIES,
    MEMORY_CATEGORIES,
    load_write_rules,
    save_write_rules,
    validate_write_rules,
)
from ..memory.agentmemory_provider import (
    AgentMemoryProvider,
    agentmemory_install_instructions,
    agentmemory_install_run,
    configure_agentmemory,
    external_memory_config,
    selected_external_provider,
)
from ..memory.notebook import MemoryNotebook
from ..memory.writer import default_notebook


def routes():
    def vector_status(client, _payload):
        return memsearch_index.status(client.config)

    def vector_config(client, payload):
        body = payload or {}
        embedding = body.get("embedding")
        if not isinstance(embedding, dict):
            embedding = None
        milvus = body.get("milvus")
        if not isinstance(milvus, dict):
            milvus = None
        return memsearch_index.configure(
            client.config,
            enabled=body.get("enabled") if "enabled" in body else None,
            watch_enabled=body.get("watch_enabled") if "watch_enabled" in body else None,
            paths=body.get("paths") if isinstance(body.get("paths"), list) else None,
            install_package=body.get("install_package"),
            embedding=embedding,
            milvus=milvus,
        )

    def vector_install(client, _payload):
        return memsearch_index.install_dependency(client.config)

    def vector_reindex(client, payload):
        return memsearch_index.reindex(
            client.config,
            force=bool((payload or {}).get("force", False)),
        )

    def vector_search(client, payload):
        return memsearch_index.search(
            client.config,
            query=str((payload or {}).get("query") or ""),
            top_k=int((payload or {}).get("top_k") or 5),
        )

    def vector_start(client, _payload):
        return memsearch_index.start_watcher(client.config)

    def vector_stop(client, _payload):
        return memsearch_index.stop_watcher(client.config)

    # ----------------------------------------------- write rules + activity
    def write_rules_get(client, _payload):
        rules = load_write_rules(client.config)
        return {
            "categories": [c.to_dict() for c in MEMORY_CATEGORIES],
            "dedupe_strategies": list(DEDUPE_STRATEGIES),
            "rules": {k: r.to_dict() for k, r in rules.items()},
            "warnings": validate_write_rules(rules),
        }

    def write_rules_set(client, payload):
        body = payload or {}
        try:
            rules = save_write_rules(client.config, body.get("rules") or {})
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "categories": [c.to_dict() for c in MEMORY_CATEGORIES],
            "dedupe_strategies": list(DEDUPE_STRATEGIES),
            "rules": {k: r.to_dict() for k, r in rules.items()},
            "warnings": validate_write_rules(rules),
        }

    def memory_capture(client, payload):
        body = payload or {}
        # Lazy import to avoid pulling the agent kernel into module-load
        # time (writer → memory_index → agent kernel → strategies …).
        from ..memory.writer import MemoryWriter
        writer = MemoryWriter(client.config)
        result = writer.capture(
            category=str(body.get("category") or ""),
            content=str(body.get("content") or ""),
            title=str(body.get("title") or ""),
            key=str(body.get("key") or ""),
            tags=list(body.get("tags") or []) if isinstance(body.get("tags"), list) else None,
            source=str(body.get("source") or "api"),
            actor_id=str(body.get("actor_id") or "default"),
            scope=str(body.get("scope") or "global"),
            strategy_id=str(body.get("strategy_id") or ""),
            target_files=body.get("target_files") if isinstance(body.get("target_files"), list) else None,
        )
        return {
            "ok": result.ok,
            "skipped": result.skipped,
            "skip_reason": result.skip_reason,
            "category": result.category,
            "key": result.key,
            "title": result.title,
            "hash": result.hash,
            "fact_ts": result.fact_ts,
            "target_files": result.target_files,
        }

    # ----------------------------------------------- curated notebook
    def _notebook_for(client):
        # Lazy import — ``MemoryWriter`` pulls in ``MemoryIndex`` which
        # we do not want to drag into module-load time.
        from ..memory.writer import default_notebook
        return default_notebook(client.config)

    def notebook_list(client, _payload):
        nb = _notebook_for(client)
        return {
            "targets": list(NOTEBOOK_VALID_TARGETS),
            "agent": {
                "entries": list(nb.entries("agent")),
                "used_chars": nb.used_chars("agent"),
                "char_limit": nb.char_limit("agent"),
                "snapshot": nb.snapshot_block("agent"),
            },
            "operator": {
                "entries": list(nb.entries("operator")),
                "used_chars": nb.used_chars("operator"),
                "char_limit": nb.char_limit("operator"),
                "snapshot": nb.snapshot_block("operator"),
            },
        }

    def notebook_mutate(client, payload):
        body = payload or {}
        action = str(body.get("action") or "").strip().lower()
        target = str(body.get("target") or "").strip().lower()
        if action not in {"add", "replace", "remove"}:
            return {"ok": False, "error": f"unknown action {action!r}; expected add|replace|remove"}
        if target not in NOTEBOOK_VALID_TARGETS:
            return {
                "ok": False,
                "error": f"unknown target {target!r}; expected one of {list(NOTEBOOK_VALID_TARGETS)}",
            }
        nb = _notebook_for(client)
        if action == "add":
            res = nb.add(target, str(body.get("content") or ""))
        elif action == "replace":
            res = nb.replace(
                target,
                str(body.get("old_text") or ""),
                str(body.get("content") or ""),
            )
        else:
            res = nb.remove(target, str(body.get("old_text") or ""))
        # Mirror the mutation onto the activity log so the dashboard's
        # /memory/activity stream reflects notebook curation alongside
        # rule-driven captures.
        log = MemoryActivityLog(config=client.config)
        try:
            from ..memory.activity import MemoryActivityEvent
            cat = "notebook_agent" if target == "agent" else "notebook_operator"
            if res.ok:
                log.append(MemoryActivityEvent.write_ok(
                    category=cat,
                    title=f"notebook.{action}",
                    preview=str(body.get("content") or "")[:200],
                    source="api:notebook",
                    extra={
                        "action": action,
                        "notebook_target": target,
                        "notebook_used_chars": res.used_chars,
                        "notebook_char_limit": res.char_limit,
                        "notebook_entry_count": len(res.entries),
                    },
                ))
            else:
                log.append(MemoryActivityEvent.write_skipped(
                    category=cat,
                    skip_reason="notebook_rejected",
                    title=f"notebook.{action}",
                    source="api:notebook",
                    extra={
                        "action": action,
                        "notebook_target": target,
                        "notebook_error": res.error,
                        "notebook_used_chars": res.used_chars,
                        "notebook_char_limit": res.char_limit,
                    },
                ))
        except Exception:  # noqa: BLE001 — activity log must never break notebook
            pass
        # Auto-ingest a research-vault row when the operator/agent saves a
        # notebook entry via this API. Mirrors the MemoryWriter path so
        # *All* durable notebook writes feed the evidence vault. Honors
        # ``runtime.evidence_vault`` and never raises.
        try:
            if res.ok and action in ("add", "replace"):
                import hashlib as _hashlib
                from ..evidence import autoingest as _evidence_autoingest

                content = str(body.get("content") or "")
                artifact_id = "sha256:" + _hashlib.sha256(
                    content.encode("utf-8", errors="ignore")
                ).hexdigest()[:16]
                cat = "notebook_agent" if target == "agent" else "notebook_operator"
                _evidence_autoingest.on_research_save(
                    client,
                    provider=cat,
                    artifact_id=artifact_id,
                    title=f"notebook.{action}:{target}",
                    body=content[:8000],
                    tags=[
                        cat,
                        f"notebook_target:{target}",
                        "source:api:notebook",
                        f"action:{action}",
                    ],
                )
        except Exception:  # pragma: no cover - defensive
            pass
        return res.to_dict()

    def memory_providers(client, _payload):
        """Materialised view of registered memory providers + their state.

        Mirrors the dashboard's needs: which provider is the
        always-on builtin, which (if any) external is currently
        active, and what other externals are registered but idle.
        Backed by :class:`MemoryManager` so the rule "1 builtin + at
        most 1 external" is enforced server-side.
        """
        from ..memory.builtin_provider import BuiltinMemoryProvider
        from ..memory.manager import MemoryManager

        mgr = MemoryManager(client.config)
        mgr.set_builtin(BuiltinMemoryProvider(client.config))
        agentmemory = AgentMemoryProvider(client.config)
        mgr.register_external_provider(agentmemory)
        if selected_external_provider(client.config) == "agentmemory":
            mgr.set_external(agentmemory)
        mgr.initialize()
        snap = mgr.snapshot()
        try:
            mgr.shutdown()
        except Exception:  # noqa: BLE001 — best-effort
            pass
        return {
            "builtin": snap.builtin,
            "external": snap.external,
            "available_external": list(snap.available_external),
        }

    def external_config_get(client, _payload):
        return external_memory_config(client.config)

    def external_config_set(client, payload):
        body = payload or {}
        try:
            return configure_agentmemory(
                client.config,
                enabled=body.get("enabled") if "enabled" in body else None,
                provider=body.get("provider") if "provider" in body else None,
                agentmemory=(
                    body.get("agentmemory")
                    if isinstance(body.get("agentmemory"), dict)
                    else None
                ),
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def external_install(client, _payload):
        return agentmemory_install_instructions(client.config)

    def external_install_run(client, _payload):
        """Actually install the agentmemory npm package globally.

        Mirrors ``memsearch_index.install_dependency`` (which runs
        ``pip install``) so the dashboard's Install button works for
        both backends. The dashboard renders ``stdout_tail`` /
        ``stderr_tail`` so operators can see why install failed without
        having to open a separate terminal.
        """

        return agentmemory_install_run(client.config)

    def memory_test(client, payload):
        """Unified backend-aware recall probe.

        Body:
            ``{"query": "..."}`` optional; defaults to "memory test".

        Routes the probe based on which backend is currently active
        (built-in always, plus memsearch / agentmemory if enabled). The
        dashboard's "Test recall" button calls this so operators can
        confirm their backend is wired correctly without learning the
        per-backend endpoints.
        """

        body = payload or {}
        query = str(body.get("query") or "memory test").strip() or "memory test"
        try:
            limit = max(1, min(20, int(body.get("limit") or 3)))
        except (TypeError, ValueError):
            limit = 3
        out: dict[str, Any] = {
            "ok": True,
            "query": query,
            "backends": [],
        }
        # Built-in: list a couple of notebook entries; never fails.
        try:
            nb: MemoryNotebook = default_notebook(client.config)
            agent_entries = list(nb.entries("agent"))[: max(1, limit)]
            operator_entries = list(nb.entries("operator"))[: max(1, limit)]
            out["backends"].append({
                "backend": "builtin",
                "ok": True,
                "agent_entries": len(agent_entries),
                "operator_entries": len(operator_entries),
                "preview": (
                    (agent_entries + operator_entries)[:2]
                ),
            })
        except Exception as exc:  # noqa: BLE001 — diagnostic only
            out["backends"].append({
                "backend": "builtin",
                "ok": False,
                "error": str(exc),
            })
        # memsearch: run a real vector search if the package is installed.
        try:
            res = memsearch_index.search(
                client.config,
                query=query,
                top_k=limit,
            )
            if isinstance(res, dict) and res.get("ok") is not False:
                rows = res.get("results") or []
                out["backends"].append({
                    "backend": "memsearch",
                    "ok": True,
                    "matches": len(rows),
                    "preview": [
                        {
                            "source": str(r.get("source") or r.get("path") or ""),
                            "score": r.get("score"),
                        }
                        for r in rows[: max(1, limit)] if isinstance(r, dict)
                    ],
                })
            else:
                out["backends"].append({
                    "backend": "memsearch",
                    "ok": False,
                    "error": (res or {}).get("error") if isinstance(res, dict) else "search_failed",
                    "detail": (res or {}).get("detail") if isinstance(res, dict) else None,
                })
        except Exception as exc:  # noqa: BLE001 — diagnostic only
            out["backends"].append({
                "backend": "memsearch",
                "ok": False,
                "error": str(exc),
            })
        # agentmemory: health probe + smart-search if enabled.
        try:
            provider = AgentMemoryProvider(client.config)
            settings = provider.settings
            if settings.enabled and settings.provider == "agentmemory":
                available = provider.is_available()
                chunks = provider.prefetch(query, limit=limit) if available else []
                out["backends"].append({
                    "backend": "agentmemory",
                    "ok": bool(available),
                    "available": bool(available),
                    "base_url": settings.base_url,
                    "matches": len(chunks),
                    "preview": [
                        {"source": c.source, "score": c.score, "text_preview": c.text[:120]}
                        for c in chunks[: max(1, limit)]
                    ],
                    "last_error": getattr(provider, "_last_error", "") or None,
                })
            else:
                out["backends"].append({
                    "backend": "agentmemory",
                    "ok": False,
                    "enabled": False,
                    "note": "agentmemory not selected in memory.external",
                })
        except Exception as exc:  # noqa: BLE001 — diagnostic only
            out["backends"].append({
                "backend": "agentmemory",
                "ok": False,
                "error": str(exc),
            })
        return out

    def activity_tail(client, payload):
        body = payload or {}
        log = MemoryActivityLog(config=client.config)
        try:
            limit = max(1, min(500, int(body.get("limit", 100))))
        except (TypeError, ValueError):
            limit = 100
        kinds_raw = body.get("kinds")
        kinds = None
        if isinstance(kinds_raw, list) and kinds_raw:
            kinds = [str(k) for k in kinds_raw if str(k or "")]
        return {
            "events": log.tail(
                limit=limit,
                kinds=kinds,
                category=str(body.get("category") or ""),
            ),
            "stats": log.stats(),
        }

    # ------------------------------------------------------------------
    # Operator preference profile.
    # ------------------------------------------------------------------

    from ..agent import operator_profile as _profile
    from ..runtime import feature_flags as _ff

    _PROFILE_FLAG = "runtime.operator_profile"
    _PROMPT_GUARD_FLAG = "runtime.prompt_guard_review_queue"

    def _profile_disabled():
        return {
            "ok": False,
            "error": "feature_disabled",
            "flag": _PROFILE_FLAG,
            "detail": "Operator preference profile is disabled via feature flag",
            "_status": 503,
        }

    def _prompt_guard_disabled():
        return {
            "ok": False,
            "error": "feature_disabled",
            "flag": _PROMPT_GUARD_FLAG,
            "detail": "Prompt guard review queue is disabled via feature flag",
            "_status": 503,
        }

    def profile_get(client, query):
        if not _ff.is_enabled(client, _PROFILE_FLAG):
            return _profile_disabled()
        q = query if isinstance(query, dict) else {}
        facet = q.get("facet") or None
        scope = q.get("scope") or None
        facts = _profile.list_facts(
            client.config.paths,
            facet=facet,
            scope=scope,
            include_forgotten=bool(q.get("include_forgotten")),
        )
        return {
            "ok": True,
            "facts": facts,
            "stats": _profile.stats(client.config.paths),
        }

    def profile_set(client, payload):
        if not _ff.is_enabled(client, _PROFILE_FLAG):
            return _profile_disabled()
        body = payload or {}
        try:
            rec = _profile.set_fact(
                client.config.paths,
                facet=str(body.get("facet") or "style"),
                key=str(body.get("key") or ""),
                value=body.get("value"),
                scope=str(body.get("scope") or "global"),
                pinned=bool(body.get("pinned", False)),
                source=str(body.get("source") or "operator_set"),
                operator_id=str(body.get("operator_id") or "operator"),
            )
        except (ValueError, PermissionError) as exc:
            return {"ok": False, "error": str(exc), "_status": 400}
        return {"ok": True, "fact": rec}

    def profile_pin(client, payload):
        if not _ff.is_enabled(client, _PROFILE_FLAG):
            return _profile_disabled()
        body = payload or {}
        try:
            rec = _profile.pin(
                client.config.paths,
                fact_id=str(body.get("fact_id") or body.get("id") or ""),
            )
        except KeyError as exc:
            return {"ok": False, "error": str(exc), "_status": 404}
        return {"ok": True, "fact": rec}

    def profile_forget(client, payload):
        if not _ff.is_enabled(client, _PROFILE_FLAG):
            return _profile_disabled()
        body = payload or {}
        try:
            rec = _profile.forget(
                client.config.paths,
                fact_id=str(body.get("fact_id") or body.get("id") or ""),
            )
        except KeyError as exc:
            return {"ok": False, "error": str(exc), "_status": 404}
        return {"ok": True, "fact": rec}

    def profile_rebuild(client, _payload):
        if not _ff.is_enabled(client, _PROFILE_FLAG):
            return _profile_disabled()
        return {"ok": True, "cache": _profile.rebuild_cache(client.config.paths)}

    # ------------------------------------------------------------------
    # Prompt-guard review queue.
    # ------------------------------------------------------------------

    from ..security import prompt_guard_queue as _pg

    def prompt_guard_list(client, query):
        if not _ff.is_enabled(client, _PROMPT_GUARD_FLAG):
            return _prompt_guard_disabled()
        q = query if isinstance(query, dict) else {}
        state = q.get("state") or None
        items = _pg.list_items(client, state=state)
        return {
            "ok": True,
            "items": items,
            "count": len(items),
            "stats": _pg.stats(client),
        }

    def prompt_guard_resolve(client, payload):
        if not _ff.is_enabled(client, _PROMPT_GUARD_FLAG):
            return _prompt_guard_disabled()
        body = payload or {}
        try:
            rec = _pg.resolve(
                client,
                item_id=str(body.get("id") or ""),
                decision=str(body.get("decision") or ""),
                operator_id=str(body.get("operator_id") or "operator"),
                note=str(body.get("note") or ""),
            )
        except (ValueError, KeyError) as exc:
            return {"ok": False, "error": str(exc), "_status": 400}
        return {"ok": True, "item": rec}

    def prompt_guard_classify(client, payload):
        """Classify a prompt sample and optionally enqueue it.

        Body::
          {
            "content": "...",
            "source_route": "POST /agent/run_turn",
            "source_channel": "dashboard",
            "enqueue": true
          }
        """
        if not _ff.is_enabled(client, _PROMPT_GUARD_FLAG):
            return _prompt_guard_disabled()
        from ..security import prompt_injection as _pi
        body = payload or {}
        content = str(body.get("content") or "")
        verdict = _pi.classify(content)
        rec = None
        if bool(body.get("enqueue", True)) and verdict["verdict"] in ("review", "block"):
            rec = _pg.enqueue(
                client,
                verdict=verdict["verdict"],
                policy=verdict["policy"],
                matched=verdict["hits"],
                excerpt=_pi.sanitized_excerpt(content),
                raw_content=content,
                source_route=str(body.get("source_route") or ""),
                source_channel=str(body.get("source_channel") or ""),
                affected_action=str(body.get("affected_action") or ""),
            )
        return {
            "ok": True,
            "verdict": verdict["verdict"],
            "policy": verdict["policy"],
            "matched": verdict["hits"],
            "enqueued": rec,
        }

    return [
        ("GET", "/memory/vector/status", vector_status),
        ("POST", "/memory/vector/config", vector_config),
        ("POST", "/memory/vector/install", vector_install),
        ("POST", "/memory/vector/reindex", vector_reindex),
        ("POST", "/memory/vector/search", vector_search),
        ("POST", "/memory/vector/start", vector_start),
        ("POST", "/memory/vector/stop", vector_stop),
        # Write rules + capture + activity feed
        ("GET", "/memory/write_rules", write_rules_get),
        ("POST", "/memory/write_rules", write_rules_set),
        ("POST", "/memory/capture", memory_capture),
        ("GET", "/memory/activity", activity_tail),
        ("POST", "/memory/activity", activity_tail),
        # Curated agent / operator notebook
        ("GET", "/memory/notebook", notebook_list),
        ("POST", "/memory/notebook", notebook_mutate),
        # Provider directory (builtin + registered externals)
        ("GET", "/memory/providers", memory_providers),
        ("GET", "/memory/external/config", external_config_get),
        ("POST", "/memory/external/config", external_config_set),
        ("POST", "/memory/external/install", external_install),
        # Real npm-based install runner for agentmemory (mirrors memsearch
        # /memory/vector/install which runs pip install). UI surfaces both
        # under the same "Install dependency" button.
        ("POST", "/memory/external/install/run", external_install_run),
        # Unified backend-aware recall probe for the "Test recall" button.
        # Returns one entry per backend (builtin always, memsearch +
        # agentmemory if enabled) so operators can compare reach in one shot.
        ("POST", "/memory/test", memory_test),
        # Operator preference profile.
        ("GET", "/memory/profile", profile_get),
        ("POST", "/memory/profile/set", profile_set),
        ("POST", "/memory/profile/pin", profile_pin),
        ("POST", "/memory/profile/forget", profile_forget),
        ("POST", "/memory/profile/rebuild", profile_rebuild),
        # Prompt-guard review queue.
        ("GET", "/security/prompt_guard/items", prompt_guard_list),
        ("POST", "/security/prompt_guard/resolve", prompt_guard_resolve),
        ("POST", "/security/prompt_guard/classify", prompt_guard_classify),
    ]
