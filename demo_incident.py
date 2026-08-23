"""
Demo: Simulate incidents to show Superlog-lite pattern in action.
- Run 1: Detect new incident → store in memory
- Run 2: Detect same incident → recurrence, load prior findings
Uses monitor.fingerprint/init_db for single source of truth; demo writes to separate demo_incidents.db
to avoid polluting incidents.db.
"""

import argparse
import json
import os
import sqlite3
from pathlib import Path

from monitor import (
    _CREATE_AGENT_RUNS_DDL,
    _CREATE_INCIDENTS_DDL,
    INITIAL_FINDINGS_TEMPLATE,
    fingerprint,
    now_iso,
)

# Reuse single source of truth from monitor.py
from monitor import (
    DB_PATH as MONITOR_DB_PATH,
)

# Demo uses separate DB by default (avoid polluting real incidents)
DEMO_DB_PATH = Path(__file__).parent / "demo_incidents.db"


def init_db(db_path=DEMO_DB_PATH):
    """Initialize demo SQLite DB — uses monitor.py DDL as single source of truth."""
    with sqlite3.connect(db_path, timeout=5.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(_CREATE_INCIDENTS_DDL)
        conn.execute(_CREATE_AGENT_RUNS_DDL)
        conn.commit()


def store_incident(error_type: str, message: str, severity: str = "warning", db_path=DEMO_DB_PATH):
    """Store incident + log agent run (Superlog pattern) — single INSERT, no double UPDATE."""
    fp = fingerprint(error_type, message)
    now = now_iso()

    with sqlite3.connect(db_path, timeout=5.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        row = conn.execute(
            "SELECT run_count, findings FROM incidents WHERE fingerprint=?", (fp,)
        ).fetchone()

        if row:
            # Recurrence
            conn.execute(
                "UPDATE incidents SET last_seen=?, run_count=run_count+1 WHERE fingerprint=?",
                (now, fp),
            )
            is_recurrence = True
            prior_findings = row[1]
            run_count = row[0] + 1
        else:
            # New incident — single INSERT
            findings_init = INITIAL_FINDINGS_TEMPLATE.format(error_type=error_type)
            conn.execute(
                "INSERT INTO incidents (id, fingerprint, error_type, top_frame, first_seen, last_seen, run_count, findings, resolution) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?, NULL)",
                (fp, fp, error_type, message, now, now, findings_init),
            )
            is_recurrence = False
            prior_findings = None
            run_count = 1

        ended = now_iso()
        conn.execute(
            "INSERT INTO agent_runs (incident_id, started_at, ended_at, status, actions_json) VALUES (?, ?, ?, 'completed', ?)",
            (
                fp,
                now,
                ended,
                json.dumps(
                    {"action": "monitored", "error_type": error_type, "message": message, "severity": severity},
                    ensure_ascii=False,
                ),
            ),
        )
        conn.commit()

    return {
        "fingerprint": fp,
        "is_recurrence": is_recurrence,
        "run_count": run_count,
        "prior_findings": prior_findings,
    }


def show_memory(db_path=DEMO_DB_PATH):
    """Display incident memory (like Superlog dashboard)."""
    with sqlite3.connect(db_path, timeout=5.0) as conn:
        rows = conn.execute(
            "SELECT fingerprint, error_type, run_count, first_seen, last_seen, findings FROM incidents"
        ).fetchall()
        runs = conn.execute("SELECT id, incident_id, started_at, status FROM agent_runs").fetchall()

    print(f"\nINCIDENT MEMORY ({len(rows)} incidents):")
    for r in rows:
        print(f"\n  {r[0]}")
        print(f"     type: {r[1]}")
        print(f"     runs: {r[2]}")
        print(f"     first: {r[3][:19]}")
        print(f"     last:  {r[4][:19]}")
        if r[5]:
            print(f"     findings: {r[5][:100]}...")

    print(f"\nAGENT RUNS ({len(runs)} total):")
    for r in runs:
        print(f"  #{r[0]}  incident={r[1]}  at={r[2][:19]}  status={r[3]}")


def main():
    parser = argparse.ArgumentParser(description="Superlog-lite incident lifecycle demo")
    parser.add_argument("--db", type=str, default=None, help=f"path to demo DB (default: {DEMO_DB_PATH})")
    parser.add_argument("--real-db", action="store_true", help="use real incidents.db (not recommended)")
    args = parser.parse_args()

    # Resolve DB path with explicit priority:
    #   --real-db > --db (CLI) > DEMO_DB env > DEMO_DB_PATH default
    if args.real_db:
        db_path = MONITOR_DB_PATH
    elif args.db is not None:
        db_path = Path(args.db)
    elif (env_db := os.getenv("DEMO_DB")):
        db_path = Path(env_db)
    else:
        db_path = DEMO_DB_PATH

    init_db(db_path)

    print("=" * 60)
    print("SUPERLOG-LITE: Incident Lifecycle Demo")
    print(f"DB: {db_path}")
    print("=" * 60)

    # --- RUN 1: Simulate new incident ---
    print("\nRUN 1: Simulating NEW incident (low_throughput)")
    r1 = store_incident(
        error_type="low_throughput",
        message="tok_s=8.2 (threshold 15)",
        severity="warning",
        db_path=db_path,
    )
    print(f"  fingerprint: {r1['fingerprint']}")
    print(f"  recurrence: {r1['is_recurrence']}")
    print(f"  run_count:  {r1['run_count']}")
    print(f"  prior_findings: {r1['prior_findings']}")

    # --- RUN 2: Simulate same incident (recurrence) ---
    print("\nRUN 2: Simulating SAME incident (recurrence)")
    r2 = store_incident(
        error_type="low_throughput",
        message="tok_s=8.2 (threshold 15)",
        severity="warning",
        db_path=db_path,
    )
    print(f"  fingerprint: {r2['fingerprint']}")
    print(f"  recurrence: {r2['is_recurrence']}")
    print(f"  run_count:  {r2['run_count']}")
    print(f"  prior_findings: {r2['prior_findings']}")

    # --- RUN 3: Different incident type ---
    print("\nRUN 3: Simulating DIFFERENT incident (high_latency)")
    r3 = store_incident(
        error_type="high_latency",
        message="latency=45.2s (threshold 30s)",
        severity="warning",
        db_path=db_path,
    )
    print(f"  fingerprint: {r3['fingerprint']}")
    print(f"  recurrence: {r3['is_recurrence']}")
    print(f"  run_count:  {r3['run_count']}")

    # --- Show memory ---
    show_memory(db_path)

    print("\n" + "=" * 60)
    print("DEMO COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
