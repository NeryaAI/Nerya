#!/usr/bin/env python3
"""Factor registry CLI for quant factor library governance."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def get_db_path() -> Path:
    return Path(__file__).resolve().parents[3] / "factor_library" / "registry.sqlite3"


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS factors (
            factor_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            family TEXT,
            category TEXT,
            scope TEXT DEFAULT 'universal',
            version INTEGER DEFAULT 1,
            status TEXT DEFAULT 'candidate',
            formula_path TEXT,
            description TEXT,
            data_requirements TEXT,
            required_data TEXT,
            lookback INTEGER,
            available_at TEXT,
            direction TEXT,
            normalization TEXT,
            applicability TEXT,
            not_applicable TEXT,
            validation TEXT,
            validation_by_market TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def cmd_init(args: argparse.Namespace) -> int:
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    init_db(conn)
    conn.close()
    print(f"Initialized factor registry at {db_path}")
    return 0


def cmd_register(args: argparse.Namespace) -> int:
    db_path = get_db_path()
    conn = sqlite3.connect(str(db_path))
    init_db(conn)
    conn.execute(
        """INSERT OR REPLACE INTO factors
           (factor_id, name, family, category, status, formula_path, description,
            data_requirements, required_data, lookback, available_at, direction,
            normalization, applicability, version, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        (
            args.factor_id, args.name, args.family, args.category,
            args.status, args.formula_path, args.description,
            json.dumps(args.data_requirements or []),
            json.dumps(args.required_data or []),
            args.lookback, args.available_at, args.direction,
            args.normalization, json.dumps({}),
            1,
        ),
    )
    conn.commit()
    conn.close()
    print(f"Registered factor {args.factor_id} as {args.status}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    db_path = get_db_path()
    conn = sqlite3.connect(str(db_path))
    init_db(conn)
    query = "SELECT factor_id, name, family, category, status, version FROM factors"
    conditions = []
    params = []
    if args.status:
        conditions.append("status = ?")
        params.append(args.status)
    if args.category:
        conditions.append("category = ?")
        params.append(args.category)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY category, family, factor_id"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    if not rows:
        print("No factors found.")
        return 0
    print(f"{'ID':<35} {'Name':<30} {'Family':<25} {'Category':<15} {'Status':<12} {'Ver'}")
    print("-" * 120)
    for row in rows:
        print(f"{row[0]:<35} {row[1]:<30} {row[2]:<25} {row[3]:<15} {row[4]:<12} {row[5]}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    db_path = get_db_path()
    conn = sqlite3.connect(str(db_path))
    init_db(conn)
    row = conn.execute(
        "SELECT status FROM factors WHERE factor_id = ?", (args.factor_id,)
    ).fetchone()
    if not row:
        print(f"Factor {args.factor_id} not found.")
        conn.close()
        return 1
    if row[0] == "production":
        print(f"Factor {args.factor_id} is already in production.")
        conn.close()
        return 1
    conn.execute(
        "UPDATE factors SET status = 'validated', updated_at = datetime('now') WHERE factor_id = ?",
        (args.factor_id,),
    )
    conn.commit()
    conn.close()
    print(f"Factor {args.factor_id} validated.")
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    print("Promotion to production requires human approval.")
    print("This command cannot auto-promote factors.")
    return 1


def cmd_demote(args: argparse.Namespace) -> int:
    db_path = get_db_path()
    conn = sqlite3.connect(str(db_path))
    init_db(conn)
    conn.execute(
        "UPDATE factors SET status = ?, updated_at = datetime('now') WHERE factor_id = ?",
        (args.status, args.factor_id),
    )
    conn.commit()
    conn.close()
    print(f"Factor {args.factor_id} set to {args.status}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Factor registry CLI")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="Initialize factor registry").set_defaults(func=cmd_init)

    p = sub.add_parser("register", help="Register a new factor")
    p.add_argument("--factor-id", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--family", required=True)
    p.add_argument("--category", required=True)
    p.add_argument("--status", default="candidate")
    p.add_argument("--formula-path", default="")
    p.add_argument("--description", default="")
    p.add_argument("--data-requirements", nargs="*", default=[])
    p.add_argument("--required-data", nargs="*", default=[])
    p.add_argument("--lookback", type=int, default=0)
    p.add_argument("--available-at", default="bar_close")
    p.add_argument("--direction", default="higher_is_bullish")
    p.add_argument("--normalization", default="rolling_zscore")
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("list", help="List factors")
    p.add_argument("--status")
    p.add_argument("--category")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("validate", help="Mark factor as validated")
    p.add_argument("--factor-id", required=True)
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("promote", help="Promote factor (requires human approval)")
    p.add_argument("--factor-id", required=True)
    p.set_defaults(func=cmd_promote)

    p = sub.add_parser("demote", help="Demote factor to degraded/retired/rejected")
    p.add_argument("--factor-id", required=True)
    p.add_argument("--status", choices=["degraded", "retired", "rejected"], required=True)
    p.set_defaults(func=cmd_demote)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
