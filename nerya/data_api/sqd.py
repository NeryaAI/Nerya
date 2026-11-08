"""Bounded Ethereum indexed event discovery; no replay or state certification."""

import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..harness.cancellation import CancelledError, raise_if_cancelled
from .types import DataActionSpec, DataApiError

BASE = "https://portal.sqd.dev/datasets/ethereum-mainnet"
MAX_BYTES = 1024 * 1024
DOCS = "https://docs.sqd.dev/en/portal/evm/api.md"


def _need(ok, message="Malformed SQD response", kind="provider_error"):
    if not ok:
        raise DataApiError(message, kind=kind, retryable=False)


def _integer(value, maximum=2**63 - 1):
    return type(value) is int and 0 <= value <= maximum


def _hex(value, size=None):
    return (
        isinstance(value, str)
        and re.fullmatch(
            r"0x(?:[0-9a-fA-F]{2})*"
            if size is None
            else rf"0x[0-9a-fA-F]{{{size * 2}}}",
            value,
        )
        is not None
    )


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _fetch(method, url, body):
    # UrllibHttp discards response headers and records bodies; keep this read stateless.
    _need(url.startswith(BASE + "/"), "Invalid SQD origin", "schema_validation")
    request = Request(
        url,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, application/x-ndjson",
        },
    )
    try:
        response = build_opener(_NoRedirect()).open(request, timeout=15)
    except HTTPError as exc:
        response = exc
    with response:
        return (
            response.code,
            dict(response.headers.items()),
            response.read(MAX_BYTES + 1),
        )


def sqd_specs(fetch=None):
    provider = SqdProvider(fetch)
    integer = {"type": "integer", "minimum": 0, "maximum": 2**63 - 1}
    properties = {
        "address": {"type": "string", "pattern": r"^0x[0-9a-fA-F]{40}$"},
        "from_block": integer,
        "to_block": integer,
        "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
        **{
            k: {"type": "string", "pattern": r"^0x[0-9a-fA-F]{64}$"}
            for k in ("topic0", "topic1")
        },
    }
    return [
        DataActionSpec(
            provider="sqd",
            action=action,
            title=title,
            description=description,
            input_schema={
                "type": "object",
                "properties": props,
                "required": required,
                "additionalProperties": False,
            },
            handler=handler,
            output_kind="table",
            docs_url=DOCS,
            replay_input=False,
            search_terms=(
                "Ethereum",
                "Aave",
                "Uniswap",
                "historical logs",
                "以太坊",
                "历史事件",
                "日志",
            ),
            tags=("ethereum", "indexed", "events", "discovery"),
        )
        for action, title, description, props, required, handler in (
            (
                "timestamp_block",
                "SQD timestamp block",
                "Ethereum 1 first block at or after Unix seconds; not a floor lookup.",
                {"timestamp": integer},
                ["timestamp"],
                provider.timestamp_block,
            ),
            (
                "evm_log_slice",
                "SQD indexed log sample",
                "Ethereum 1 exact address, at most 100 inclusive blocks, one batch, 500 rows. Raw hex; no event/state completeness or performance evidence.",
                properties,
                ["address", "from_block", "to_block"],
                provider.evm_log_slice,
            ),
        )
    ]


class SqdProvider:
    def __init__(self, fetch=None):
        # Injectable contract: (method, fixed_url, json_body) -> (status, headers, raw_bytes).
        self.fetch = fetch if fetch is not None else _fetch

    def _read(self, context, path, body=None):
        url, method = BASE + path, "GET" if body is None else "POST"
        raise_if_cancelled(context.cancel_token)
        try:
            status, headers, raw = self.fetch(method, url, body)
        except CancelledError:
            raise
        except Exception as exc:
            raise DataApiError(
                "SQD transport failed",
                kind="provider_error",
                retryable=True,
                detail={"source_url": url},
            ) from exc
        raise_if_cancelled(context.cancel_token)
        headers = {k.lower(): v for k, v in headers.items()}
        metadata = {
            key: value
            for key, name in (
                ("retry_after", "retry-after"),
                ("request_id", "x-request-id"),
            )
            if isinstance(value := headers.get(name), str)
            and re.fullmatch(r"[ -~]{1,128}", value)
        }
        if status != 200:
            try:
                payload = (
                    json.loads(raw)
                    if isinstance(raw, bytes) and len(raw) <= MAX_BYTES
                    else {}
                )
                if isinstance(payload, dict):
                    for key in ("error_name", "error_category"):
                        value = payload.get(key)
                        if isinstance(value, str) and re.fullmatch(
                            r"[A-Za-z0-9_-]{1,64}", value
                        ):
                            metadata[key] = value
                    if _integer(payload.get("error_code")):
                        metadata["error_code"] = payload["error_code"]
                    for key in ("owner_action_required", "retryable"):
                        if type(payload.get(key)) is bool:
                            metadata[key] = payload[key]
                    ray_id = payload.get("ray_id", headers.get("cf-ray"))
                    if isinstance(ray_id, str) and re.fullmatch(
                        r"[0-9a-fA-F]{16}(?:-[A-Z]{3})?", ray_id
                    ):
                        metadata["ray_id"] = ray_id
                error = payload.get("error", {}) if isinstance(payload, dict) else {}
                for key in ("type", "code"):
                    value = error.get(key) if isinstance(error, dict) else None
                    if isinstance(value, str) and re.fullmatch(
                        r"[A-Za-z0-9_-]{1,64}", value
                    ):
                        metadata["error_" + key if key == "type" else key] = value
            except (ValueError, UnicodeError, RecursionError, AttributeError):
                pass
            if re.fullmatch(r"[0-9]{1,10}", metadata.get("retry_after", "")):
                metadata["retry_after_seconds"] = int(metadata["retry_after"])
            kind = (
                "rate_limit_error"
                if status == 429 or metadata.get("error_type") == "rate_limit_error"
                else "provider_error"
            )
            raise DataApiError(
                (
                    "SQD access blocked by client signature (Cloudflare 1010); "
                    "requires provider owner resolution"
                    if status == 403
                    and metadata.get("error_code") == 1010
                    and metadata.get("error_name") == "browser_signature_banned"
                    else "SQD HTTP request failed"
                ),
                kind=kind,
                retryable={
                    "rate_limit_error": True,
                    "availability_error": True,
                    "api_error": False,
                    "invalid_request_error": False,
                    "authentication_error": False,
                    "permission_error": False,
                }.get(
                    metadata.get("error_type"),
                    metadata.get("retryable", status == 429 or status >= 500),
                ),
                detail={"source_url": url, "http_status": status, **metadata},
            )
        _need(
            isinstance(raw, bytes) and len(raw) <= MAX_BYTES,
            "Invalid or oversized SQD response",
        )
        source = {
            "url": url,
            "request": json.dumps({"method": method, "query": body}, sort_keys=True),
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "hash_basis": "raw_response_bytes",
            **metadata,
        }
        try:
            docs = (
                [json.loads(raw.decode("utf-8"))]
                if body is None
                else [
                    json.loads(line)
                    for line in raw.decode("utf-8").splitlines()
                    if line.strip()
                ]
            )
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise DataApiError(
                "Malformed SQD JSON", kind="provider_error", retryable=False
            ) from exc
        _need(all(isinstance(doc, dict) for doc in docs))
        return docs, source

    def timestamp_block(self, context, args):
        _need(
            set(args) == {"timestamp"} and _integer(args.get("timestamp")),
            "Expected integer Unix seconds",
            "schema_validation",
        )
        docs, source = self._read(context, f"/timestamps/{args['timestamp']}/block")
        _need(len(docs) == 1 and _integer(docs[0].get("block_number")))
        return self._result(
            [{"timestamp": args["timestamp"], "block_number": docs[0]["block_number"]}],
            source,
            requested_window={"timestamp": args["timestamp"]},
            coverage={
                "timestamp_semantics": "first_block_at_or_after_not_floor",
                "completeness": "not_asserted",
            },
        )

    def evm_log_slice(self, context, args):
        start, end, limit = (
            args.get("from_block"),
            args.get("to_block"),
            args.get("limit", 50),
        )
        _need(
            not (
                set(args)
                - {"address", "from_block", "to_block", "topic0", "topic1", "limit"}
            )
            and _hex(args.get("address"), 20)
            and _integer(start)
            and _integer(end)
            and 0 <= end - start < 100
            and _integer(limit, 500)
            and limit > 0
            and all(_hex(args[k], 32) for k in ("topic0", "topic1") if k in args),
            "Expected exact address, up to 100 inclusive blocks, optional topics and limit 1..500",
            "schema_validation",
        )
        query = {
            "type": "evm",
            "fromBlock": start,
            "toBlock": end,
            "includeAllBlocks": True,
            "fields": {
                "block": dict.fromkeys(
                    ("number", "hash", "parentHash", "timestamp"), True
                ),
                "log": dict.fromkeys(
                    (
                        "address",
                        "topics",
                        "data",
                        "transactionHash",
                        "transactionIndex",
                        "logIndex",
                    ),
                    True,
                ),
            },
            "logs": [
                {
                    "address": [args["address"]],
                    **{k: [args[k]] for k in ("topic0", "topic1") if k in args},
                }
            ],
        }
        docs, source = self._read(context, "/finalized-stream", query)
        _need(len(docs) <= end - start + 1)
        rows, previous, count, resume = [], None, 0, None
        for offset, doc in enumerate(docs):
            header, logs = doc.get("header"), doc.get("logs", [])
            _need(isinstance(header, dict) and isinstance(logs, list))
            _need(
                type(header.get("number")) is int
                and header["number"] == start + offset
                and _hex(header.get("hash"), 32)
                and _hex(header.get("parentHash"), 32)
                and _integer(header.get("timestamp"), 253402300799)
            )
            _need(
                previous is None
                or (
                    header["parentHash"].lower() == previous["hash"].lower()
                    and header["timestamp"] > previous["timestamp"]
                )
            )
            last_log, last_tx = -1, -1
            for log in logs:
                _need(isinstance(log, dict))
                topics = log.get("topics")
                _need(
                    _hex(log.get("address"), 20)
                    and log["address"].lower() == args["address"].lower()
                    and isinstance(topics, list)
                    and len(topics) <= 4
                    and all(_hex(t, 32) for t in topics)
                    and all(
                        len(topics) > i and topics[i].lower() == args[k].lower()
                        for i, k in enumerate(("topic0", "topic1"))
                        if k in args
                    )
                    and _hex(log.get("data"))
                    and _hex(log.get("transactionHash"), 32)
                    and _integer(log.get("logIndex"))
                    and log["logIndex"] > last_log
                    and _integer(log.get("transactionIndex"))
                    and log["transactionIndex"] >= last_tx
                )
                last_log, last_tx = log["logIndex"], log["transactionIndex"]
                count += 1
                if count <= limit:
                    rows.append(
                        {
                            "block_number": header["number"],
                            "block_hash": header["hash"],
                            "parent_hash": header["parentHash"],
                            "timestamp": header["timestamp"],
                            **{k: log[k] for k in query["fields"]["log"]},
                        }
                    )
                elif resume is None:
                    resume = header["number"]
            previous = header
        scanned_to = previous["number"] if previous else None
        truncated = scanned_to != end or count > limit
        return self._result(
            rows,
            source,
            requested_window={"from_block": start, "to_block": end},
            filters={k: args[k] for k in ("address", "topic0", "topic1") if k in args},
            coverage={
                "scanned_from_block": start if docs else None,
                "scanned_to_block": scanned_to,
                "first_hash": docs[0]["header"]["hash"] if docs else None,
                "last_hash": previous["hash"] if docs else None,
                "first_timestamp": datetime.fromtimestamp(
                    docs[0]["header"]["timestamp"], timezone.utc
                ).isoformat()
                if docs
                else None,
                "last_timestamp": datetime.fromtimestamp(
                    previous["timestamp"], timezone.utc
                ).isoformat()
                if docs
                else None,
                "scanned_block_count": len(docs),
                "source_log_count": count,
                "returned_row_count": len(rows),
                "next_block": resume
                if resume is not None
                else (scanned_to + 1 if docs else start),
                "resume_repeats_block": resume is not None,
                "sample_truncated": truncated,
                "scanned_window_complete": scanned_to == end,
                "completeness": "incomplete" if truncated else "not_asserted",
            },
        )

    @staticmethod
    def _result(rows, source, **metadata):
        metadata["coverage"].update(
            chain_id=1,
            dataset="ethereum-mainnet",
            replay_input=False,
            limitations=[
                "Indexed discovery sample only; no event completeness, contract state or performance evidence.",
                "One HTTP batch only. Row-cap continuation repeats the first omitted block; deduplicate logs.",
            ],
        )
        return {
            "rows": rows,
            "source_url": source["url"],
            "_envelope": {
                "provider": "sqd",
                "mode": "public_read_only",
                "mock": False,
                "observation_kind": "indexed_discovery_sample",
                "sources": [source],
                **metadata,
            },
        }
