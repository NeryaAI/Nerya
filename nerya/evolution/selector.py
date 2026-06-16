"""Select reusable evolution assets for current signals."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..core import jsonl
from ..core.paths import WorkspacePaths
from .assets import list_capsules, list_genes
from .observation_summary import (
    POST_APPLY_HEALTHY_STATUSES,
    POST_APPLY_NEGATIVE_STATUSES,
    summarize_observation_weights,
)


_POST_APPLY_HEALTHY = POST_APPLY_HEALTHY_STATUSES
_POST_APPLY_NEGATIVE = POST_APPLY_NEGATIVE_STATUSES


def select_assets_for_signals(
    paths: WorkspacePaths,
    signals: list[dict[str, Any]],
    *,
    strategy_id: str | None = None,
    limit: int = 8,
) -> dict[str, list[dict[str, Any]]]:
    kinds = {str(s.get("kind") or "") for s in signals}
    usage = _asset_usage(paths)
    post_apply = _post_apply_summary_by_proposal(paths)
    genes_all = list_genes(paths)
    gene_signal_map = {
        str(gene.get("id") or ""): {
            str(signal)
            for signal in (gene.get("signals_match") or [])
            if str(signal).strip()
        }
        for gene in genes_all
        if gene.get("id")
    }
    context = _signal_context(signals)
    genes: list[dict[str, Any]] = []
    for gene in genes_all:
        matches = set(str(x) for x in (gene.get("signals_match") or []))
        if kinds & matches:
            genes.append(_with_gdi(
                gene,
                _score_gene(gene, kinds=kinds, usage=usage),
            ))
    genes.sort(key=lambda g: float((g.get("gdi") or {}).get("score") or 0.0), reverse=True)
    capsules = [
        _with_gdi(
            capsule,
            _score_capsule(
                capsule,
                kinds=kinds,
                usage=usage,
                post_apply=post_apply,
                gene_signal_map=gene_signal_map,
                context=context,
            ),
        )
        for capsule in list_capsules(paths, strategy_id=strategy_id, limit=max(limit, 200))
    ]
    capsules.sort(key=lambda c: float((c.get("gdi") or {}).get("score") or 0.0), reverse=True)
    return {
        "genes": genes[:limit],
        "capsules": capsules[:limit],
        "gdi": {
            "version": "gdi_v1",
            "dimensions": ["intrinsic", "usage", "human", "freshness", "relevance"],
            "signal_kinds": sorted(k for k in kinds if k),
        },
    }


def annotate_assets_with_gdi(
    paths: WorkspacePaths,
    assets: list[dict[str, Any]],
    *,
    signals: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Attach GDI v1 scoring to already-listed assets."""

    kinds = {str(s.get("kind") or "") for s in (signals or [])}
    usage = _asset_usage(paths)
    post_apply = _post_apply_summary_by_proposal(paths)
    genes_all = list_genes(paths)
    gene_signal_map = {
        str(gene.get("id") or ""): {
            str(signal)
            for signal in (gene.get("signals_match") or [])
            if str(signal).strip()
        }
        for gene in genes_all
        if gene.get("id")
    }
    context = _signal_context(signals or [])
    out: list[dict[str, Any]] = []
    for asset in assets:
        if str(asset.get("kind") or "") == "gene":
            out.append(_with_gdi(asset, _score_gene(asset, kinds=kinds, usage=usage)))
        elif str(asset.get("kind") or "") == "capsule":
            out.append(_with_gdi(asset, _score_capsule(
                asset,
                kinds=kinds,
                usage=usage,
                post_apply=post_apply,
                gene_signal_map=gene_signal_map,
                context=context,
            )))
        else:
            out.append(asset)
    out.sort(key=lambda row: float((row.get("gdi") or {}).get("score") or 0.0), reverse=True)
    return out


def _score_gene(
    gene: dict[str, Any],
    *,
    kinds: set[str],
    usage: dict[str, Any],
) -> dict[str, Any]:
    gene_id = str(gene.get("id") or "")
    matches = sorted(kinds & {str(x) for x in (gene.get("signals_match") or [])})
    intrinsic = _clamp(float(gene.get("confidence") or 0.0))
    if matches:
        intrinsic = _clamp(intrinsic + 0.12)
    used = int((usage.get("genes") or {}).get(gene_id, 0))
    usage_score = _clamp(0.25 + min(0.5, used * 0.1))
    human = _gene_human_score(gene_id, usage)
    freshness = _freshness_score(gene)
    score = _weighted(
        intrinsic=intrinsic,
        usage=usage_score,
        human=human,
        freshness=freshness,
    )
    return {
        "version": "gdi_v1",
        "score": score,
        "polarity": "positive",
        "components": {
            "intrinsic": round(intrinsic, 4),
            "usage": round(usage_score, 4),
            "human": round(human, 4),
            "freshness": round(freshness, 4),
        },
        "matched_signals": matches,
        "usage_count": used,
        "rationale": _rationale("gene", score, matches, used, None),
    }


def _score_capsule(
    capsule: dict[str, Any],
    *,
    kinds: set[str],
    usage: dict[str, Any],
    post_apply: dict[str, dict[str, Any]],
    gene_signal_map: dict[str, set[str]],
    context: dict[str, Any],
) -> dict[str, Any]:
    cid = str(capsule.get("id") or "")
    outcome = _float(capsule.get("outcome_score"), default=0.0)
    proposal_id = _capsule_proposal_id(capsule)
    post_summary = post_apply.get(proposal_id or "", {})
    post_status = str(post_summary.get("status") or "")
    weighted_negative = _float(post_summary.get("weighted_negative_count"), default=0.0)
    weighted_healthy = _float(post_summary.get("weighted_healthy_count"), default=0.0)
    polarity = (
        "negative"
        if outcome < 0 or post_status in _POST_APPLY_NEGATIVE or weighted_negative >= 0.5
        else "positive"
    )
    validation = _validation_quality(capsule)
    relevance = _capsule_relevance(
        capsule,
        kinds=kinds,
        gene_signal_map=gene_signal_map,
        context=context,
    )
    relevance_score = _float(relevance.get("score"), default=0.45)
    intrinsic = _clamp(validation + min(0.2, abs(outcome) * 0.2))
    used = int((usage.get("capsules") or {}).get(cid, 0))
    usage_score = _clamp(0.25 + min(0.45, used * 0.1))
    human = _capsule_human_score(
        outcome,
        post_status,
        weighted_negative=weighted_negative,
        weighted_healthy=weighted_healthy,
        weighted_observing=_float(post_summary.get("weighted_observing_count"), default=0.0),
    )
    freshness = _freshness_score(capsule)
    score = _weighted_capsule(
        intrinsic=intrinsic,
        usage=usage_score,
        human=human,
        freshness=freshness,
        relevance=relevance_score,
    )
    if polarity == "negative":
        # Negative capsules are not "good outcomes", but they are valuable
        # cautionary assets when fresh and relevant. Keep them selectable,
        # but avoid promoting explicitly mismatched triggers above better
        # regime matches.
        if relevance_score >= 0.65:
            score = max(score, 0.55 if post_status in _POST_APPLY_NEGATIVE else 0.42)
        elif relevance_score >= 0.45:
            score = max(score, 0.42)
    return {
        "version": "gdi_v1",
        "score": round(score, 4),
        "polarity": polarity,
        "components": {
            "intrinsic": round(intrinsic, 4),
            "usage": round(usage_score, 4),
            "human": round(human, 4),
            "freshness": round(freshness, 4),
            "relevance": round(relevance_score, 4),
        },
        "matched_signals": relevance.get("matched_signals") or [],
        "relevance": relevance,
        "usage_count": used,
        "post_apply_status": post_status,
        "post_apply_weighted": post_summary or None,
        "rationale": _rationale(
            "capsule",
            score,
            list(relevance.get("matched_signals") or []),
            used,
            post_status,
            relevance_score=relevance_score,
        ),
    }


def _with_gdi(asset: dict[str, Any], gdi: dict[str, Any]) -> dict[str, Any]:
    return {**asset, "gdi": gdi}


def _asset_usage(paths: WorkspacePaths) -> dict[str, dict[str, int]]:
    genes: dict[str, int] = {}
    capsules: dict[str, int] = {}
    for row in jsonl.read_all(paths.evolution_events):
        for gid in row.get("genes_used") or []:
            text = str(gid or "")
            if text:
                genes[text] = genes.get(text, 0) + 1
        meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        capsule = meta.get("capsule")
        if isinstance(capsule, dict) and capsule.get("id"):
            cid = str(capsule.get("id"))
            capsules[cid] = capsules.get(cid, 0) + 1
        for cid in meta.get("capsules_used") or []:
            text = str(cid or "")
            if text:
                capsules[text] = capsules.get(text, 0) + 1
    return {"genes": genes, "capsules": capsules}


def _post_apply_summary_by_proposal(paths: WorkspacePaths) -> dict[str, dict[str, Any]]:
    rows_by_proposal: dict[str, list[dict[str, Any]]] = {}
    for row in jsonl.read_all(paths.journal("evolution")):
        if row.get("kind") != "proposal.post_apply_observation":
            continue
        proposal_id = str(row.get("proposal_id") or "")
        if proposal_id:
            rows_by_proposal.setdefault(proposal_id, []).append(row)
    return {
        proposal_id: _post_apply_summary(rows)
        for proposal_id, rows in rows_by_proposal.items()
    }


def _post_apply_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted(rows, key=lambda row: str(row.get("observed_at") or row.get("ts") or ""))
    latest = rows[-1] if rows else {}
    summary = summarize_observation_weights(rows)
    return {
        "status": str(latest.get("status") or latest.get("outcome") or "").lower(),
        "observed_at": latest.get("observed_at") or latest.get("ts"),
        "count": len(rows),
        "by_status": summary["by_status"],
        "by_source": summary["by_source"],
        "weighted_by_status": summary["weighted_by_status"],
        "weighted_by_source": summary["weighted_by_source"],
        "weighted_negative_count": summary["weighted_negative_count"],
        "weighted_healthy_count": summary["weighted_healthy_count"],
        "weighted_observing_count": summary["weighted_observing_count"],
        "decay": summary["decay"],
    }


def _validation_quality(capsule: dict[str, Any]) -> float:
    results = [row for row in (capsule.get("validation_results") or []) if isinstance(row, dict)]
    if not results:
        return 0.35
    statuses = {str(row.get("status") or "").lower() for row in results}
    if statuses & {"passed", "ok", "safe", "ready"}:
        return 0.8
    if statuses & {"failed", "blocked"}:
        return 0.2
    return 0.5


def _gene_human_score(gene_id: str, usage: dict[str, Any]) -> float:
    used = int((usage.get("genes") or {}).get(gene_id, 0))
    return _clamp(0.5 + min(0.35, used * 0.08))


def _capsule_human_score(
    outcome: float,
    post_status: str | None,
    *,
    weighted_negative: float = 0.0,
    weighted_healthy: float = 0.0,
    weighted_observing: float = 0.0,
) -> float:
    if weighted_healthy >= 0.5 or post_status in _POST_APPLY_HEALTHY:
        return 0.9
    if weighted_negative >= 0.5 or post_status in _POST_APPLY_NEGATIVE:
        return 0.85
    if weighted_observing >= 0.5:
        return 0.5
    if outcome > 0:
        return _clamp(0.55 + min(0.35, outcome * 0.25))
    if outcome < 0:
        return _clamp(0.55 + min(0.3, abs(outcome) * 0.2))
    return 0.45


def _capsule_proposal_id(capsule: dict[str, Any]) -> str | None:
    metadata = capsule.get("metadata") if isinstance(capsule.get("metadata"), dict) else {}
    direct = metadata.get("proposal_id")
    if direct:
        return str(direct)
    ref = str(capsule.get("promotion_ref") or "")
    if ref.startswith("proposal:"):
        return ref.split(":", 1)[1]
    return None


def _signal_context(signals: list[dict[str, Any]]) -> dict[str, Any]:
    regimes: set[str] = set()
    markets: set[str] = set()
    timeframes: set[str] = set()
    data_quality: set[str] = set()
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        kind = str(signal.get("kind") or "")
        if kind.startswith("market_regime_"):
            regimes.add(kind.removeprefix("market_regime_"))
        if kind == "market_news_context":
            regimes.add("news_context")
        if kind == "market_data_degraded":
            data_quality.add("degraded")
        metadata = signal.get("metadata") if isinstance(signal.get("metadata"), dict) else {}
        markets.update(_str_set(metadata.get("markets")))
        timeframe = str(metadata.get("timeframe") or "").strip()
        if timeframe:
            timeframes.add(timeframe)
    return {
        "market_regimes": regimes,
        "markets": markets,
        "timeframes": timeframes,
        "data_quality": data_quality,
    }


def _capsule_relevance(
    capsule: dict[str, Any],
    *,
    kinds: set[str],
    gene_signal_map: dict[str, set[str]],
    context: dict[str, Any],
) -> dict[str, Any]:
    metadata = capsule.get("metadata") if isinstance(capsule.get("metadata"), dict) else {}
    explicit_signals = _first_str_set(
        metadata,
        "trigger_signal_kinds",
        "signal_kinds",
        "matched_signal_kinds",
        "selection_signal_kinds",
        "signals_match",
    ) | _str_set(capsule.get("signals_match"))
    gene_id = str(capsule.get("gene_id") or "")
    inherited_signals = set(gene_signal_map.get(gene_id) or set())
    trigger_signals = explicit_signals or inherited_signals
    matched_signals = sorted(kinds & trigger_signals)
    explicit_trigger = bool(explicit_signals)
    inherited_from_gene = bool(not explicit_signals and inherited_signals)

    if matched_signals:
        score = 0.78 if inherited_from_gene else 0.86
    elif trigger_signals:
        score = 0.28 if explicit_trigger else 0.38
    else:
        score = 0.45

    current_regimes = set(context.get("market_regimes") or set())
    current_markets = set(context.get("markets") or set())
    current_timeframes = set(context.get("timeframes") or set())
    current_quality = set(context.get("data_quality") or set())
    capsule_regimes = _first_str_set(
        metadata,
        "trigger_market_regimes",
        "market_regimes",
        "market_regime",
    )
    capsule_markets = _first_str_set(metadata, "trigger_markets", "markets", "market")
    capsule_timeframes = _first_str_set(metadata, "trigger_timeframes", "timeframes", "timeframe")
    capsule_quality = _first_str_set(metadata, "trigger_data_quality", "data_quality")

    matched_context: dict[str, list[str]] = {}
    if capsule_regimes and current_regimes:
        overlap = sorted(capsule_regimes & current_regimes)
        if overlap:
            score += 0.08
            matched_context["market_regimes"] = overlap
        elif explicit_trigger:
            score -= 0.04
    if capsule_markets and current_markets:
        overlap = sorted(capsule_markets & current_markets)
        if overlap:
            score += 0.04
            matched_context["markets"] = overlap
    if capsule_timeframes and current_timeframes:
        overlap = sorted(capsule_timeframes & current_timeframes)
        if overlap:
            score += 0.03
            matched_context["timeframes"] = overlap
    if capsule_quality and current_quality:
        overlap = sorted(capsule_quality & current_quality)
        if overlap:
            score += 0.04
            matched_context["data_quality"] = overlap

    return {
        "version": "capsule_relevance_v1",
        "score": round(_clamp(score), 4),
        "matched_signals": matched_signals,
        "trigger_signal_kinds": sorted(trigger_signals),
        "source": (
            "metadata" if explicit_trigger
            else "gene" if inherited_from_gene
            else "neutral"
        ),
        "gene_id": gene_id or None,
        "matched_context": matched_context,
    }


def _first_str_set(metadata: dict[str, Any], *keys: str) -> set[str]:
    for key in keys:
        values = _str_set(metadata.get(key))
        if values:
            return values
    return set()


def _str_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        text = value.strip()
        return {text} if text else set()
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip() for item in value if str(item).strip()}
    text = str(value).strip()
    return {text} if text else set()


def _freshness_score(asset: dict[str, Any]) -> float:
    metadata = asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {}
    raw = asset.get("ts") or metadata.get("ts")
    if not raw:
        return 0.55
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 86400)
    except Exception:
        return 0.55
    if age_days <= 7:
        return 1.0
    if age_days <= 30:
        return 0.8
    if age_days <= 90:
        return 0.6
    return 0.35


def _weighted(*, intrinsic: float, usage: float, human: float, freshness: float) -> float:
    return round(
        _clamp(intrinsic) * 0.4
        + _clamp(usage) * 0.25
        + _clamp(human) * 0.25
        + _clamp(freshness) * 0.1,
        4,
    )


def _weighted_capsule(
    *,
    intrinsic: float,
    usage: float,
    human: float,
    freshness: float,
    relevance: float,
) -> float:
    return round(
        _clamp(intrinsic) * 0.32
        + _clamp(usage) * 0.18
        + _clamp(human) * 0.2
        + _clamp(freshness) * 0.1
        + _clamp(relevance) * 0.2,
        4,
    )


def _rationale(
    kind: str,
    score: float,
    matches: list[str],
    usage_count: int,
    post_status: str | None,
    *,
    relevance_score: float | None = None,
) -> str:
    parts = [f"{kind} GDI={score:.2f}"]
    if matches:
        parts.append(f"matches {', '.join(matches[:3])}")
    elif relevance_score is not None and relevance_score < 0.4:
        parts.append("low trigger relevance")
    if usage_count:
        parts.append(f"used {usage_count} time(s)")
    if post_status:
        parts.append(f"post-apply {post_status}")
    return "; ".join(parts) + "."


def _float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


__all__ = ["annotate_assets_with_gdi", "select_assets_for_signals"]
