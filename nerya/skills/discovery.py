"""Compact discovery without removing skills or widening an operator allowlist.

Nested playbooks belong to their nearest available hub. A flat compatibility
playbook can name ``metadata.nerya.catalog_parent`` in SKILL.md. Exact lookup,
script paths and permissions are unchanged; only the advertised catalog folds.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable
import re
import unicodedata


def rank_skill_metadata(query: str, rows: Iterable[tuple[str, str]]) -> list[dict[str, Any]]:
    """Bounded, offline multilingual discovery. Never selects permission policy.

    Callers filter access BEFORE this function. Only names/descriptions are
    searched; no playbook, reference, script, model or network is read here.
    """
    query = unicodedata.normalize("NFKC", query).strip().casefold()[:512]
    rows = list(rows)
    # An operator-provided namespace prefix is precise, not a bag of concepts.
    if re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)+", query):
        exact_prefix = [(name, description) for name, description in rows if name.casefold().startswith(query)]
        if exact_prefix:
            rows = exact_prefix
    terms = set(re.findall(r"[a-z0-9][a-z0-9.-]+", query))
    terms.difference_update({"the", "and", "for", "with", "this", "that", "please", "help", "me", "to", "of", "in"})
    for phrase in re.findall(r"[\u3400-\u9fff]+", query):
        for width in (2, 3):
            terms.update(phrase[i:i + width] for i in range(len(phrase) - width + 1))
    ranked = []
    for skill_id, description in rows:
        name = unicodedata.normalize("NFKC", skill_id).casefold()
        body = unicodedata.normalize("NFKC", description).casefold()
        matches = sorted(term for term in terms if term in name or term in body)
        score = sum(3 * (term in name) + 2 * (term in body) for term in matches)
        if query and query == name:
            score += 100
        elif query and (query in name or query in body):
            score += 8
        if score:
            ranked.append({"skill_id": skill_id, "score": score, "matched_terms": matches[:8]})
    return sorted(ranked, key=lambda row: (-row["score"], row["skill_id"]))


def catalog_parent(metadata: Any) -> str:
    """Read optional discovery metadata, never execution or permission policy."""
    nerya = metadata.get("nerya") if isinstance(metadata, dict) else None
    value = nerya.get("catalog_parent") if isinstance(nerya, dict) else None
    return value.strip() if isinstance(value, str) else ""


def catalog_roots(rows: Iterable[tuple[str, Path | None, str]]) -> dict[str, str]:
    """Map each entry to its visible workflow, without changing permissions."""
    entries = {name: (path, parent) for name, path, parent in rows}
    parents: dict[str, str] = {}
    for name, (path, declared) in entries.items():
        if declared in entries and declared != name:
            parents[name] = declared
        elif path is not None:
            ancestors = [
                (len(other.parts), candidate)
                for candidate, (other, _) in entries.items()
                if (other is not None and other in path.parents
                    and name.startswith(candidate + "."))
            ]
            if ancestors:
                parents[name] = max(ancestors)[1]
    roots = {name: name for name in entries}
    for name in parents:
        seen = {name}
        current = parents[name]
        while current in parents and current not in seen:
            seen.add(current)
            current = parents[current]
        if current not in seen:
            roots[name] = current
        # Fail open for catalog mistakes; do not make a cycle disappear.
    return roots


def catalog_ids(rows: Iterable[tuple[str, Path | None, str]]) -> set[str]:
    """Return visible IDs; missing hubs and malformed cycles stay discoverable."""
    return {name for name, root in catalog_roots(rows).items() if name == root}


def catalog_group(metadata: Any) -> str:
    """Optional presentation group; unknown/custom entries stay in core."""
    nerya = metadata.get("nerya") if isinstance(metadata, dict) else None
    return "professional" if isinstance(nerya, dict) and nerya.get("catalog_group") == "professional" else "core"
