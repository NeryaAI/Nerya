"""Plan 30 P1 §4 — Hermes-style skills hub: trust + hash + lock.

Hermes's skills hub keeps a ``skills.lock`` next to ``skills/installed/``
that records, for every promoted skill, the exact ``sha256`` of its tree
plus the publisher / source / version it was approved with. The kernel
verifies the lock at boot and flags drift before anything is loaded —
so a skill modified on disk after promotion can't quietly arm new
behaviour.

Nerya's installer already records ``sha256`` in ``install_report.json``
for every skill it stages. This module simply lifts that data into a
single ``skills/skills.lock.yml`` ledger and adds:

* :func:`record_lock_entry` — append/refresh an entry when a skill is
  promoted via :func:`nerya.skills.installer.promote_installed`.
* :func:`load_lock` — read the current ledger.
* :func:`verify_lock` — recompute every installed skill's sha256 and
  return a structured drift report.
* :func:`load_trust` — load the optional ``skills/trust.yml`` allowlist
  of trusted publishers / pinned hashes (forward-compat hook for the
  full signed-manifest workflow).

The lock file format is intentionally tiny and forward-compatible:

.. code-block:: yaml

    version: 1
    skills:
      btc_trader:
        version: "1.2.0"
        sha256: "abc123..."
        source_kind: git
        source: "git+https://github.com/operator/btc_trader.git"
        installed_at: "2026-04-25T03:00:00+00:00"
        publisher: ""
        signature: ""
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..core import yaml_io
from ..core.paths import WorkspacePaths
from ..core.time import now_iso

LOCK_VERSION = 1


@dataclass
class LockEntry:
    skill_id: str
    version: str = ""
    sha256: str = ""
    source_kind: str = ""
    source: str = ""
    installed_at: str = ""
    publisher: str = ""
    signature: str = ""

    def asdict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("skill_id")
        return d


@dataclass
class TrustEntry:
    publisher: str
    pinned_hashes: list[str] = field(default_factory=list)
    fingerprints: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class DriftReport:
    ok: bool
    missing: list[str] = field(default_factory=list)        # in lock, not on disk
    untracked: list[str] = field(default_factory=list)      # on disk, not in lock
    mismatches: list[dict[str, Any]] = field(default_factory=list)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


def hash_skill_tree(root: Path) -> str:
    """Return a deterministic sha256 for the tree at ``root``.

    Mirrors :func:`nerya.skills.installer._hash_dir` so a freshly
    installed skill and the lock entry agree byte-for-byte.
    """
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode("utf-8"))
            h.update(b"\x00")
            h.update(p.read_bytes())
    return h.hexdigest()


def load_lock(paths: WorkspacePaths) -> dict[str, LockEntry]:
    """Read the lock file. Returns ``{}`` when the file is missing."""
    doc = yaml_io.load(paths.skills_lock, default={}) or {}
    out: dict[str, LockEntry] = {}
    for sid, raw in (doc.get("skills") or {}).items():
        if not isinstance(raw, dict):
            continue
        out[sid] = LockEntry(
            skill_id=sid,
            version=str(raw.get("version") or ""),
            sha256=str(raw.get("sha256") or ""),
            source_kind=str(raw.get("source_kind") or ""),
            source=str(raw.get("source") or ""),
            installed_at=str(raw.get("installed_at") or ""),
            publisher=str(raw.get("publisher") or ""),
            signature=str(raw.get("signature") or ""),
        )
    return out


def save_lock(paths: WorkspacePaths, entries: dict[str, LockEntry]) -> None:
    """Atomic-ish write of the lock file (yaml_io.dump handles the
    write)."""
    paths.skills.mkdir(parents=True, exist_ok=True)
    doc = {
        "version": LOCK_VERSION,
        "skills": {sid: e.asdict() for sid, e in sorted(entries.items())},
    }
    yaml_io.dump(paths.skills_lock, doc)


def record_lock_entry(
    paths: WorkspacePaths,
    *,
    skill_id: str,
    version: str = "",
    sha256: str = "",
    source_kind: str = "",
    source: str = "",
    publisher: str = "",
    signature: str = "",
    installed_at: str | None = None,
) -> LockEntry:
    """Insert or refresh an entry in the lock file.

    Called by the installer after :func:`promote_installed` so the lock
    ledger always agrees with what's actually on disk.
    """
    entries = load_lock(paths)
    entry = LockEntry(
        skill_id=skill_id,
        version=version,
        sha256=sha256 or _compute_sha256(paths, skill_id),
        source_kind=source_kind,
        source=source,
        publisher=publisher,
        signature=signature,
        installed_at=installed_at or now_iso(),
    )
    entries[skill_id] = entry
    save_lock(paths, entries)
    return entry


def remove_lock_entry(paths: WorkspacePaths, skill_id: str) -> bool:
    """Drop ``skill_id`` from the lock file. Returns ``True`` when the
    entry existed."""
    entries = load_lock(paths)
    if skill_id not in entries:
        return False
    del entries[skill_id]
    save_lock(paths, entries)
    return True


def verify_lock(paths: WorkspacePaths) -> DriftReport:
    """Recompute every installed skill's sha256 and compare it against
    the lock ledger. Returns a structured drift report."""
    lock = load_lock(paths)
    on_disk: dict[str, str] = {}
    if paths.skills_installed.exists():
        for d in sorted(paths.skills_installed.iterdir()):
            if d.is_dir() and (d / "SKILL.md").exists():
                on_disk[d.name] = hash_skill_tree(d)

    missing = sorted(set(lock) - set(on_disk))
    untracked = sorted(set(on_disk) - set(lock))
    mismatches: list[dict[str, Any]] = []
    for sid, entry in sorted(lock.items()):
        if sid not in on_disk:
            continue
        if entry.sha256 and entry.sha256 != on_disk[sid]:
            mismatches.append({
                "skill_id": sid,
                "expected": entry.sha256,
                "actual": on_disk[sid],
            })

    return DriftReport(
        ok=not (missing or untracked or mismatches),
        missing=missing,
        untracked=untracked,
        mismatches=mismatches,
    )


def load_trust(paths: WorkspacePaths) -> dict[str, TrustEntry]:
    """Load the optional ``skills/trust.yml`` allowlist.

    Format::

        publishers:
          operator-corp:
            fingerprints: ["abcd...sha256-of-pgp-key..."]
            pinned_hashes:
              - "deadbeef..."   # exact-match hash for one approved version
            note: "Internal skills team"

    Operators can grow this incrementally; today it's a hint surface
    used by tooling, tomorrow it gates installs.
    """
    doc = yaml_io.load(paths.skills_trust, default={}) or {}
    publishers = doc.get("publishers") or {}
    out: dict[str, TrustEntry] = {}
    for name, raw in publishers.items():
        if not isinstance(raw, dict):
            continue
        out[name] = TrustEntry(
            publisher=name,
            pinned_hashes=[str(s) for s in (raw.get("pinned_hashes") or [])],
            fingerprints=[str(s) for s in (raw.get("fingerprints") or [])],
            note=str(raw.get("note") or ""),
        )
    return out


def is_trusted(paths: WorkspacePaths, *, sha256: str,
               publisher: str = "") -> tuple[bool, str]:
    """Return ``(trusted, reason)``.

    A skill is "trusted" when:

    * an entry under ``skills/trust.yml::publishers.<publisher>`` lists
      ``sha256`` in ``pinned_hashes``, or
    * the publisher field is empty *and* the trust file is empty (no
      explicit allowlist => fall back to legacy behaviour).
    """
    trust = load_trust(paths)
    if not trust:
        return True, "no_trust_policy"
    if publisher and publisher in trust:
        if sha256 and sha256 in trust[publisher].pinned_hashes:
            return True, f"pinned:{publisher}"
        return False, f"unpinned_for_publisher:{publisher}"
    return False, "untrusted_publisher" if publisher else "no_publisher"


def _compute_sha256(paths: WorkspacePaths, skill_id: str) -> str:
    target = paths.skills_installed / skill_id
    if not target.exists():
        return ""
    return hash_skill_tree(target)


__all__ = [
    "LOCK_VERSION",
    "LockEntry",
    "TrustEntry",
    "DriftReport",
    "hash_skill_tree",
    "load_lock",
    "save_lock",
    "record_lock_entry",
    "remove_lock_entry",
    "verify_lock",
    "load_trust",
    "is_trusted",
]
