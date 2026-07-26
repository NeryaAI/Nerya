#!/usr/bin/env python3
"""Verify that built distributions contain every bundled skill resource."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path


def _archive_members(archive: Path) -> set[str]:
    if archive.suffix == ".whl":
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
    elif archive.name.endswith(".tar.gz"):
        with tarfile.open(archive, mode="r:gz") as bundle:
            names = bundle.getnames()
    else:
        raise ValueError(f"unsupported distribution archive: {archive}")

    normalized: set[str] = set()
    for name in names:
        marker = "nerya/"
        offset = name.find(marker)
        if offset >= 0:
            normalized.add(name[offset:])
    return normalized


def _bundled_skill_files(repo_root: Path) -> set[str]:
    skills_root = repo_root / "nerya" / "skills" / "builtin"
    return {
        path.relative_to(repo_root).as_posix()
        for path in skills_root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    }


def _smoke_installed_wheel(wheel: Path) -> None:
    """Extract a wheel and load its skill registry outside the source tree."""
    with tempfile.TemporaryDirectory(prefix="nerya-wheel-smoke-") as raw_tmp:
        target = Path(raw_tmp)
        with zipfile.ZipFile(wheel) as bundle:
            bundle.extractall(target)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(target)
        code = """
from nerya.skills.registry import SkillRegistry

ids = {entry.manifest.id for entry in SkillRegistry.load_builtin().list()}
required = {"analysis", "expert_investors", "finance-creators", "quant-strategy-loop"}
missing = sorted(required - ids)
if missing:
    raise SystemExit(f"wheel registry is missing built-in skills: {missing}")
print(f"wheel install smoke: loaded {len(ids)} built-in skills")
"""
        subprocess.run(
            [sys.executable, "-c", code],
            cwd=target,
            env=env,
            check=True,
        )


def verify(dist_dir: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    expected = _bundled_skill_files(repo_root)
    if not expected:
        raise SystemExit("no bundled skill files found in the source tree")

    archives = [*sorted(dist_dir.glob("*.whl")), *sorted(dist_dir.glob("*.tar.gz"))]
    if not archives:
        raise SystemExit(f"no wheel or sdist found in {dist_dir}")

    failures: list[str] = []
    for archive in archives:
        members = _archive_members(archive)
        missing = sorted(expected - members)
        if missing:
            preview = "\n    ".join(missing[:20])
            suffix = f"\n    ... and {len(missing) - 20} more" if len(missing) > 20 else ""
            failures.append(
                f"{archive.name} is missing {len(missing)} bundled skill files:\n"
                f"    {preview}{suffix}"
            )
        generated = sorted(
            member
            for member in members
            if "__pycache__" in Path(member).parts or member.endswith((".pyc", ".pyo"))
        )
        if generated:
            preview = "\n    ".join(generated[:20])
            suffix = f"\n    ... and {len(generated) - 20} more" if len(generated) > 20 else ""
            failures.append(
                f"{archive.name} contains {len(generated)} generated Python files:\n"
                f"    {preview}{suffix}"
            )
        if not missing and not generated:
            print(f"{archive.name}: verified {len(expected)} bundled skill files")

    if failures:
        raise SystemExit("\n".join(failures))
    _smoke_installed_wheel(next(path for path in archives if path.suffix == ".whl"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist_dir", nargs="?", type=Path, default=Path("dist"))
    args = parser.parse_args()
    verify(args.dist_dir.resolve())


if __name__ == "__main__":
    main()
