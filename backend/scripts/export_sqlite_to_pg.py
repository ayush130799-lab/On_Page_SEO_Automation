#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite -> PostgreSQL data export script.

Reads the local seo_automation.db SQLite database and generates a
PostgreSQL-compatible SQL INSERT dump.

Usage (from backend/ directory):
    python scripts/export_sqlite_to_pg.py > pg_data_import.sql 2>export_log.txt
    # Then import into Postgres:
    psql "$DATABASE_URL" < pg_data_import.sql

Safety:
  - Read-only: never modifies the source SQLite file.
  - Generates INSERT ... ON CONFLICT DO NOTHING (idempotent).
  - Skips alembic_version (Alembic manages that itself).
  - Uses session_replication_role=replica to skip FK triggers during bulk load.
"""

from __future__ import annotations

import io
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Path to the SQLite database (relative to this script's location)
SQLITE_PATH = Path(__file__).resolve().parent.parent / "seo_automation.db"

# Tables to skip - managed by Alembic or empty/ephemeral
SKIP_TABLES = {"alembic_version"}

# Tables in FK-safe insertion order (parents before children)
TABLE_ORDER = [
    "users",
    "websites",
    "website_members",
    "settings",
    "integrations",
    "crawl_runs",
    "pages",
    "seo_audits",
    "seo_issues",
    "ga4_metrics",
    "gsc_metrics",
    "semrush_metrics",
    "historical_metrics",
    "priority_scores",
    "ai_recommendations",
    "recommendation_scores",
    "keyword_opportunities",
    "page_intent_profiles",
    "jobs",
    "seo_roadmaps",
    "github_events",
    "github_commits",
    "github_pull_requests",
    "github_changes",
    "competitor_analyses",
    "competitor_results",
    "deployment_analyses",
    "seo_experiments",
    "seo_experiment_checkpoints",
]


def pg_literal(value) -> str:
    """Convert a Python value to a PostgreSQL literal string."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    s = str(value)
    s = s.replace("'", "''")
    return f"'{s}'"


def export_table(cur: sqlite3.Cursor, table: str, out, batch_size: int = 100) -> int:
    """Write INSERT statements for one table. Returns row count."""
    # Use offset/limit to avoid loading entire large tables into memory
    # and to be resilient against WAL/locking issues
    offset = 0
    count = 0
    columns = None

    while True:
        try:
            cur.execute(f'SELECT * FROM "{table}" LIMIT {batch_size} OFFSET {offset}')
            rows = cur.fetchall()
        except Exception as e:
            print(f"  WARNING: Error reading {table} at offset {offset}: {e}", file=sys.stderr)
            break

        if not rows:
            break

        if columns is None:
            columns = [d[0] for d in cur.description]
            col_list = ", ".join(f'"{c}"' for c in columns)
        
        col_list_str = ", ".join(f'"{c}"' for c in columns)

        for row in rows:
            try:
                values = ", ".join(pg_literal(v) for v in row)
                out.write(
                    f'INSERT INTO "{table}" ({col_list_str}) VALUES ({values}) ON CONFLICT DO NOTHING;\n'
                )
                count += 1
            except Exception as e:
                print(f"  WARNING: Skipping row in {table} at offset {offset + count}: {e}", file=sys.stderr)

        offset += len(rows)
        if len(rows) < batch_size:
            break

    return count


def main() -> None:
    if not SQLITE_PATH.exists():
        print(f"ERROR: SQLite database not found at {SQLITE_PATH}", file=sys.stderr)
        sys.exit(1)

    print(f"Opening SQLite database: {SQLITE_PATH}", file=sys.stderr)
    
    # Open with URI mode and immutable flag to avoid WAL issues
    try:
        conn = sqlite3.connect(f"file:{SQLITE_PATH}?mode=ro", uri=True, timeout=30)
    except Exception as e:
        print(f"WARNING: Could not open read-only, trying normal mode: {e}", file=sys.stderr)
        conn = sqlite3.connect(str(SQLITE_PATH), timeout=30)

    conn.row_factory = sqlite3.Row

    cur = conn.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    all_tables = {row[0] for row in cur.fetchall()}

    print(f"Found tables: {sorted(all_tables)}", file=sys.stderr)

    ordered = [t for t in TABLE_ORDER if t in all_tables]
    remaining = sorted(all_tables - set(ordered) - SKIP_TABLES)
    tables_to_export = ordered + remaining

    # Use a UTF-8 wrapped stdout so emojis and non-ASCII chars survive on Windows
    out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", newline="\n")

    out.write("-- ============================================================\n")
    out.write("-- SEO Automation Platform - SQLite to PostgreSQL data dump\n")
    out.write(f"-- Generated: {datetime.now(timezone.utc).isoformat()}Z\n")
    out.write("-- Source: seo_automation.db\n")
    out.write("-- ============================================================\n\n")

    # Disable FK checks during bulk load; re-enable after
    out.write("SET session_replication_role = replica;  -- disables FK triggers\n")
    out.write("BEGIN;\n\n")

    total = 0
    for table in tables_to_export:
        if table in SKIP_TABLES:
            continue
        out.write(f"-- Table: {table}\n")
        n = export_table(cur, table, out)
        out.write("\n")
        print(f"  Exported {table}: {n} rows", file=sys.stderr)
        total += n

    out.write("COMMIT;\n")
    out.write("SET session_replication_role = DEFAULT;  -- re-enable FK triggers\n\n")

    # Reset sequences to avoid PK collisions on future inserts
    out.write("-- Reset sequences to avoid PK conflicts on future inserts\n")
    seq_tables = [
        "users", "websites", "pages", "seo_issues", "seo_audits",
        "crawl_runs", "ai_recommendations", "recommendation_scores",
        "priority_scores", "ga4_metrics", "gsc_metrics", "integrations",
        "keyword_opportunities", "page_intent_profiles", "jobs",
        "historical_metrics", "seo_roadmaps", "seo_experiments",
        "competitor_analyses", "competitor_results",
    ]
    for table in seq_tables:
        if table in all_tables:
            out.write(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE(MAX(id), 1)) FROM \"{table}\";\n"
            )

    out.write("\n-- Import complete.\n")
    out.flush()
    conn.close()
    print(f"\nTotal rows exported: {total}", file=sys.stderr)


if __name__ == "__main__":
    main()
