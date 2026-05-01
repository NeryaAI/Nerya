"""Quick exploration of a CSV / Parquet / TSV file.

Standalone CLI usage::

    python -m nerya.skills.builtin.analysis.scripts.explore_table \
        --json '{"path": "data.csv", "head": 5, "describe": true}'

Output is JSON with shape, dtypes, head rows, and (optionally)
descriptive stats. Designed to fit in a model's context — head defaults
to 5 rows, never the full table.

Payload schema::

    {
      "path": "<file path>",          # required
      "head": 5,                      # optional, default 5
      "describe": true,               # optional, include df.describe()
      "columns": ["col1", "col2"],    # optional, project before reading
      "sample": 1000                  # optional, random sample N rows for big files
    }

Reads CSV/TSV/Parquet via pandas. For very large files use ``sample``
to avoid loading the whole table.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def run(
    path: str,
    *,
    head: int = 5,
    describe: bool = False,
    columns: list[str] | None = None,
    sample: int | None = None,
) -> dict[str, Any]:
    import pandas as pd

    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(p)

    suffix = p.suffix.lower()
    if suffix in {".parquet", ".pq"}:
        df = pd.read_parquet(p, columns=columns)
    elif suffix in {".tsv", ".txt"}:
        df = pd.read_csv(p, sep="\t", usecols=columns)
    else:  # csv or unknown — try csv with sniffed delimiter
        df = pd.read_csv(p, usecols=columns)

    rows, cols = df.shape
    if sample and rows > sample:
        df = df.sample(n=sample, random_state=0)

    out: dict[str, Any] = {
        "path": str(p),
        "shape": {"rows": rows, "cols": cols},
        "dtypes": {c: str(t) for c, t in df.dtypes.items()},
        "head": json.loads(df.head(head).to_json(orient="records", date_format="iso")),
    }
    if describe:
        try:
            out["describe"] = json.loads(
                df.describe(include="all").to_json(date_format="iso")
            )
        except Exception as exc:
            out["describe_error"] = f"{type(exc).__name__}: {exc}"
    return out


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload_file:
        with open(args.payload_file, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    if args.payload_json:
        return json.loads(args.payload_json) or {}
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        return json.loads(raw) if raw else {}
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", dest="payload_json", default=None)
    parser.add_argument("--payload-file", dest="payload_file", default=None)
    parser.add_argument("--path", dest="path", default=None)
    parser.add_argument("--head", dest="head", type=int, default=None)
    parser.add_argument("--describe", dest="describe", action="store_true")
    args = parser.parse_args()

    payload = _load_payload(args)
    path = args.path or payload.get("path")
    if not path:
        sys.stderr.write("missing required field: path\n")
        raise SystemExit(2)
    head = args.head if args.head is not None else int(payload.get("head", 5))
    describe = args.describe or bool(payload.get("describe", False))
    columns = payload.get("columns")
    sample = payload.get("sample")

    try:
        result = run(
            path,
            head=head,
            describe=describe,
            columns=columns,
            sample=sample,
        )
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
