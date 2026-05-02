from __future__ import annotations

from ..memory import memsearch_index


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

    return [
        ("GET", "/memory/vector/status", vector_status),
        ("POST", "/memory/vector/config", vector_config),
        ("POST", "/memory/vector/install", vector_install),
        ("POST", "/memory/vector/reindex", vector_reindex),
        ("POST", "/memory/vector/search", vector_search),
        ("POST", "/memory/vector/start", vector_start),
        ("POST", "/memory/vector/stop", vector_stop),
    ]

