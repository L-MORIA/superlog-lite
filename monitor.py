"""
Superlog-Lite: Lightweight incident monitoring for local LLM servers.

Pattern: Fingerprint → Memory → Agent Run
- Fingerprint: hash(error_type, bucket) → incident_id (bucketed, not exact value)
- Memory: SQLite with findings per incident (WAL, timeout, ON CONFLICT)
- Recurrence: if fingerprint seen before → load prior findings
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Windows-only: process creation flag for detached console window.
# Defined at module level (not inside the function) so it's a proper constant,
# not a local var that N806 would flag.
_CREATE_NEW_CONSOLE = 0x00000010


def now_iso() -> str:
    """ISO timestamp in UTC (tz-aware). Use for all SQLite storage."""
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(s: str) -> datetime:
    """Parse ISO timestamp; legacy naive values are assumed UTC."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --- Single source of truth for schema & initial findings ---
# demo_incident.py imports these — keep in sync.

INITIAL_FINDINGS_TEMPLATE = (
    "Initial investigation: {error_type}. "
    "Checked server status, VRAM, process list."
)

_CREATE_INCIDENTS_DDL = """
CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    fingerprint TEXT UNIQUE,
    error_type TEXT,
    top_frame TEXT,
    first_seen TEXT,
    last_seen TEXT,
    run_count INTEGER DEFAULT 0,
    findings TEXT,
    resolution TEXT
)
"""

_CREATE_AGENT_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT,
    started_at TEXT,
    ended_at TEXT,
    status TEXT,
    actions_json TEXT
)
"""

# --- Config (env-overridable, no hardcoded F:\\ in logic) ---
BASE = os.getenv("SUPERLOG_BASE", "http://localhost:8083/v1")
TOK_S_THRESHOLD = float(os.getenv("SUPERLOG_TOK_S_THRESHOLD", "10"))
LATENCY_THRESHOLD = float(os.getenv("SUPERLOG_LATENCY_THRESHOLD", "30"))
RESTART_COOLDOWN_S = int(os.getenv("SUPERLOG_RESTART_COOLDOWN", "600"))
GEN_TIMEOUT = float(os.getenv("SUPERLOG_GEN_TIMEOUT", "120"))
DB_PATH = Path(__file__).parent / "incidents.db"

# Restart bat — env overrides, fallback to sibling dir (F:/barozp-opus-8083) for backward compat
_default_restart = str(Path(__file__).parent.parent / "barozp-opus-8083" / "run_barozp_8083_mtp.bat")
RESTART_BAT = os.getenv("SUPERLOG_RESTART_BAT", _default_restart)
RESTART_CWD = os.getenv("SUPERLOG_RESTART_CWD", str(Path(RESTART_BAT).parent) if RESTART_BAT else "")

# Generation test defaults
DEFAULT_MAX_TOKENS = 100
DEFAULT_MODEL_FALLBACK = "Qwen3_8-Opus-4_7-MTP-Q3KM-hybrid"


def _normalize_frame(text: str) -> str:
    """Normalize error frame for fingerprint bucketing: strip numbers/timings."""
    if not text:
        return ""
    # Replace numbers (incl. floats) with N to avoid 8.2 vs 8.3 fragmentation
    normalized = re.sub(r"\d+(\.\d+)?", "N", text)
    # Collapse whitespace
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized[:200]


def api(path, body=None, timeout=120):
    """Make API call to local LLM server. Returns dict; on error {"error":..., "status":...}."""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
    # BASE is a local-LLM endpoint (http://localhost:port) configured at startup;
    # not user-supplied input, so file:// risk does not apply. S310 silenced.
    url = path if path.startswith("http") else BASE + path
    req = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            raw = r.read().decode("utf-8", errors="replace")
            if not raw:
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            body_text = e.read().decode("utf-8", errors="replace")[:500]
        except UnicodeDecodeError:
            body_text = ""
        return {"error": f"HTTP {e.code}: {e.reason} {body_text}".strip(), "status": e.code}
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        return {"error": str(e)}


def fingerprint(error_type: str, top_frame: str = "") -> str:
    """Superlog-style fingerprint: bucketed hash of error signature."""
    # Bucket fixed values for throughput/latency to avoid fragmentation
    if error_type == "low_throughput":
        bucket = "low"
    elif error_type == "high_latency":
        bucket = "high"
    else:
        bucket = _normalize_frame(top_frame)
    sig = f"{error_type}|{bucket}"
    return hashlib.sha256(sig.encode()).hexdigest()[:16]


def init_db(db_path=None):
    """Initialize SQLite incident memory (WAL, timeout).

    db_path defaults to DB_PATH resolved at call time; demo_incident.py
    passes its own demo DB path here.
    """
    with sqlite3.connect(db_path or DB_PATH, timeout=5.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(_CREATE_INCIDENTS_DDL)
        conn.execute(_CREATE_AGENT_RUNS_DDL)
        conn.commit()


def measure_tok_s(max_tokens=100, timeout=None):
    """Measure tokens/second with a real generation test (dynamic model)."""
    # Try to discover model from /v1/models
    model = DEFAULT_MODEL_FALLBACK
    try:
        models_resp = api("/models", timeout=10)
        if "error" not in models_resp:
            data = models_resp.get("data", [])
            if data and isinstance(data, list) and "id" in data[0]:
                model = data[0]["id"]
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        logger.debug("model discovery failed: %s", e)

    req_timeout = GEN_TIMEOUT if timeout is None else timeout
    t0 = time.time()
    r = api(
        "/chat/completions",
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "Отвечай кратко, без размышлений."},
                {"role": "user", "content": "Сгенерируй текст примерно на 100 токенов про локальные LLM."},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        },
        timeout=req_timeout,
    )
    t1 = time.time()

    if "error" in r:
        return {"error": r["error"], "tok_s": 0, "latency_s": t1 - t0, "status": r.get("status")}

    usage = r.get("usage", {})
    comp_tokens = usage.get("completion_tokens", 0)
    estimated = False

    # If server didn't return usage, try to estimate from text length or mark as error
    if comp_tokens == 0:
        # Try to estimate from response text
        choices = r.get("choices", [])
        if choices and isinstance(choices, list):
            text = choices[0].get("message", {}).get("content", "") or choices[0].get("text", "")
            if text:
                # Rough estimate: 1 token ~ 4 chars for Russian/English mix
                est = max(1, len(text) // 4)
                comp_tokens = est
                estimated = True
            else:
                return {"error": "no completion_tokens in response", "tok_s": 0, "latency_s": t1 - t0}
        else:
            return {"error": "no completion_tokens in response", "tok_s": 0, "latency_s": t1 - t0}

    gen_s = t1 - t0
    if gen_s <= 0:
        gen_s = 0.001
    tok_s = comp_tokens / gen_s if gen_s > 0 else 0

    result = {
        "tok_s": tok_s,
        "latency_s": gen_s,
        "completion_tokens": comp_tokens,
        "model": model,
        "ok": True,
    }
    if estimated:
        # Flag heuristic results so downstream findings can mark them approximate
        result["estimated"] = True
    return result


def _server_root() -> str:
    """Server root URL: strip a trailing /v1 from BASE (health lives above it)."""
    base = BASE.rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def check_server():
    """Full health check: /models + generation test."""
    result = {
        "timestamp": now_iso(),
        "checks": {},
    }

    # Check 1: /v1/models reachable (api never throws, no try needed)
    models = api("/models", timeout=10)
    result["checks"]["models"] = {
        "ok": "error" not in models,
        "count": len(models.get("data", [])) if "error" not in models else 0,
        "ids": [m["id"] for m in models.get("data", [])] if "error" not in models else [],
    }
    if "error" in models:
        result["checks"]["models"]["error"] = models["error"]
        if "status" in models:
            result["checks"]["models"]["status"] = models["status"]
        # Server is unreachable at the API root: probing /health and running
        # the generation test would only add up to GEN_TIMEOUT of waiting and
        # produce a duplicate generation_error next to server_unreachable.
        result["checks"]["slot"] = {"ok": False, "status": "not_checked", "note": "server_unreachable"}
        result["checks"]["generation"] = {
            "tok_s": 0,
            "latency_s": 0.0,
            "completion_tokens": 0,
            "skipped": "server_unreachable",
        }
        return result

    # Check 1b: /health slot availability (server root, not /v1). With
    # --parallel 1 a busy slot makes the generation probe block up to
    # GEN_TIMEOUT and get misclassified as a critical generation_error —
    # detect server_busy instead and skip the probe.
    health = api(_server_root() + "/health", timeout=5)
    if "error" in health:
        slot = {"ok": True, "status": "unknown", "note": health["error"]}
    elif health.get("status", "ok") == "ok":
        slot = {"ok": True, "status": "ok"}
    else:
        slot = {
            "ok": False,
            "status": health.get("status", ""),
            "slots_idle": health.get("slots_idle"),
            "slots_processing": health.get("slots_processing"),
        }
    result["checks"]["slot"] = slot

    # Check 2: Generation test (skipped when the slot is busy)
    if slot["ok"]:
        result["checks"]["generation"] = measure_tok_s(DEFAULT_MAX_TOKENS)
    else:
        result["checks"]["generation"] = {
            "tok_s": 0,
            "latency_s": 0.0,
            "completion_tokens": 0,
            "skipped": f"slot_busy: {slot['status']}",
        }

    return result


def classify_incident(checks):
    """Superlog-style: classify checks into incident fingerprints (bucketed)."""
    incidents = []

    models_check = checks["checks"]["models"]
    if not models_check["ok"]:
        # Root cause: server unreachable. Degradation checks are meaningless
        # without a reachable API — report only the root incident.
        fp = fingerprint("server_unreachable", models_check.get("error", ""))
        return [
            {
                "fingerprint": fp,
                "error_type": "server_unreachable",
                "severity": "critical",
                "message": "Server not responding",
            }
        ]

    gen = checks["checks"]["generation"]
    slot = checks["checks"].get("slot")

    if slot is not None and not slot["ok"]:
        # Busy slot: probe was skipped, report as warning — restarting the server
        # would kill whoever holds the slot (e.g. a long agent job).
        status = slot.get("status", "unknown")
        fp = fingerprint("server_busy", "")
        incidents.append(
            {
                "fingerprint": fp,
                "error_type": "server_busy",
                "severity": "warning",
                "message": f"Slot busy, generation probe skipped (status: {status})",
            }
        )
    elif "error" in gen:
        fp = fingerprint("generation_error", gen["error"])
        incidents.append(
            {
                "fingerprint": fp,
                "error_type": "generation_error",
                "severity": "critical",
                "message": gen["error"],
            }
        )
    else:
        # Throughput and latency are independent — both can trigger
        if gen.get("tok_s", 0) < TOK_S_THRESHOLD:
            fp = fingerprint("low_throughput", "")
            incidents.append(
                {
                    "fingerprint": fp,
                    "error_type": "low_throughput",
                    "severity": "warning",
                    "message": f"Throughput degraded: {gen['tok_s']:.1f} tok/s",
                }
            )
        if gen.get("latency_s", 0) > LATENCY_THRESHOLD:
            fp = fingerprint("high_latency", "")
            incidents.append(
                {
                    "fingerprint": fp,
                    "error_type": "high_latency",
                    "severity": "warning",
                    "message": f"High latency: {gen['latency_s']:.1f}s",
                }
            )

    return incidents


def store_incident(incident, db_path=None):
    """Store incident in SQLite memory (Superlog pattern) — handles race via ON CONFLICT.

    db_path defaults to DB_PATH resolved at call time (so tests/CLI can patch it).
    demo_incident.py reuses this function with its own demo DB.
    """
    fp = incident["fingerprint"]
    now = now_iso()
    findings_init = INITIAL_FINDINGS_TEMPLATE.format(error_type=incident["error_type"])

    with sqlite3.connect(db_path or DB_PATH, timeout=5.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")

        # Try to insert, on conflict do update
        try:
            conn.execute(
                "INSERT INTO incidents (id, fingerprint, error_type, top_frame, first_seen, last_seen, run_count, findings, resolution) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?, NULL)",
                (fp, fp, incident["error_type"], incident["message"], now, now, findings_init),
            )
            is_recurrence = False
            prior_findings = None
            run_count = 1
        except sqlite3.IntegrityError:
            # Already exists — increment
            row = conn.execute(
                "SELECT run_count, findings FROM incidents WHERE fingerprint=?", (fp,)
            ).fetchone()
            prior_findings = row[1] if row else None
            prev_count = row[0] if row else 0
            conn.execute(
                "UPDATE incidents SET last_seen=?, run_count=run_count+1 WHERE fingerprint=?",
                (now, fp),
            )
            is_recurrence = True
            run_count = prev_count + 1

        # Log agent run with ended_at
        ended = now_iso()
        conn.execute(
            "INSERT INTO agent_runs (incident_id, started_at, ended_at, status, actions_json) VALUES (?, ?, ?, 'completed', ?)",
            (fp, now, ended, json.dumps({"action": "monitored", "data": incident}, ensure_ascii=False)),
        )
        conn.commit()

    return {
        "fingerprint": fp,
        "is_recurrence": is_recurrence,
        "prior_findings": prior_findings,
        "run_count": run_count,
    }


def auto_fix(incident):
    """Restart the LLM server on critical incidents (with cooldown)."""
    # Only auto-fix critical incidents (not warnings like low_throughput)
    if incident["severity"] != "critical":
        print(f"    [auto-fix] skipped (severity={incident['severity']})")
        return {"action": "skipped", "reason": f"severity={incident['severity']}"}

    # Cooldown: avoid restart storm — check last_seen for this fingerprint
    try:
        with sqlite3.connect(DB_PATH, timeout=5.0) as conn:
            row = conn.execute(
                "SELECT last_seen, run_count FROM incidents WHERE fingerprint=?", (incident["fingerprint"],)
            ).fetchone()
            if row:
                last_seen_str, run_count = row
                try:
                    last_seen = _parse_ts(last_seen_str)
                    elapsed = (datetime.now(timezone.utc) - last_seen).total_seconds()
                    # If we just restarted recently (<cooldown) and run_count >1, skip
                    if elapsed < RESTART_COOLDOWN_S and run_count > 1:
                        msg = f"cooldown {RESTART_COOLDOWN_S}s, elapsed {int(elapsed)}s, run_count={run_count}"
                        print(f"    [auto-fix] skipped (cooldown: {msg})")
                        return {"action": "skipped", "reason": f"cooldown: {msg}"}
                except (ValueError, sqlite3.Error) as e:
                    logger.debug("cooldown check failed: %s", e)
    except (sqlite3.Error, ValueError) as e:
        logger.debug("cooldown DB access failed: %s", e)

    print("    [auto-fix] RESTARTING server...")
    print(f"    [auto-fix] bat: {RESTART_BAT}")

    if not Path(RESTART_BAT).exists():
        return {"action": "failed", "error": f"restart bat not found: {RESTART_BAT}"}

    if not Path(RESTART_CWD).exists():
        return {"action": "failed", "error": f"restart cwd not found: {RESTART_CWD}"}

    try:
        # Use cmd /c for reliable .bat execution on Windows.
        # `cmd` is a hardcoded Windows shell binary (always in PATH), not
        # user-supplied input. S603/S607 do not apply.
        proc = subprocess.Popen(  # noqa: S603
            [r"C:\Windows\System32\cmd.exe", "/c", RESTART_BAT],
            creationflags=_CREATE_NEW_CONSOLE,
            cwd=RESTART_CWD,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"    [auto-fix] PID={proc.pid}")

        # Wait for server to come back (up to 120s) — check /models (not /health)
        print("    [auto-fix] waiting for server to come back...")
        for i in range(24):  # 24 x 5s = 120s max
            time.sleep(5)
            health = api("/models", timeout=5)
            if "error" not in health:
                print(f"    [auto-fix] server back up after {(i+1)*5}s")
                return {"action": "restarted", "pid": proc.pid, "waited_s": (i + 1) * 5}

    except OSError as e:
        return {"action": "failed", "error": str(e)}
    else:
        return {
            "action": "restarted_but_unhealthy",
            "pid": proc.pid,
            "note": "server did not come back yet",
        }


def main():
    # CLI entry point: CLI flags override module-level defaults. Global mutation
    # here is intentional and the only mutation point for these constants.
    global DB_PATH, TOK_S_THRESHOLD, LATENCY_THRESHOLD, GEN_TIMEOUT  # noqa: PLW0603
    parser = argparse.ArgumentParser(description="Superlog-lite: monitor ik_llama:8083")
    parser.add_argument("--no-auto-fix", action="store_true", help="disable auto-restart on critical incidents")
    parser.add_argument("--db", type=str, default=None, help=f"path to incidents.db (default: {DB_PATH})")
    parser.add_argument("--threshold-tok", type=float, default=None, help=f"tok/s threshold (default {TOK_S_THRESHOLD})")
    parser.add_argument("--threshold-latency", type=float, default=None, help=f"latency threshold s (default {LATENCY_THRESHOLD})")
    parser.add_argument("--gen-timeout", type=float, default=None, help=f"generation probe timeout s (default {GEN_TIMEOUT})")
    args = parser.parse_args()
    if args.db:
        DB_PATH = Path(args.db)
    if args.threshold_tok is not None:
        TOK_S_THRESHOLD = args.threshold_tok
    if args.threshold_latency is not None:
        LATENCY_THRESHOLD = args.threshold_latency
    if args.gen_timeout is not None:
        GEN_TIMEOUT = args.gen_timeout

    init_db()

    print("=" * 60)
    print("SUPERLOG-LITE: ik_llama:8083 Monitoring Demo")
    print("=" * 60)

    # Run health check
    checks = check_server()

    print(f"\nTimestamp: {checks['timestamp']}")
    print("\nCHECKS:")
    print(f"  models: {checks['checks']['models']}")
    if "slot" in checks["checks"]:
        print(f"  slot: {checks['checks']['slot']}")
    gen = checks["checks"]["generation"]
    if gen.get("skipped"):
        print(f"  generation: SKIPPED ({gen['skipped']})")
    else:
        print(
            f"  generation: tok_s={gen.get('tok_s', 0):.1f}, latency={gen.get('latency_s', 0):.2f}s, "
            f"tokens={gen.get('completion_tokens', 0)}"
        )

    # Classify incidents
    incidents = classify_incident(checks)

    if not incidents:
        print("\nNO INCIDENTS — server healthy")
        print(f"   tok/s: {gen['tok_s']:.1f} (threshold: {TOK_S_THRESHOLD})")
        print(f"   latency: {gen['latency_s']:.2f}s (threshold: {LATENCY_THRESHOLD}s)")
    else:
        print(f"\n{len(incidents)} INCIDENT(S) DETECTED:")
        for inc in incidents:
            print(f"\n  • {inc['error_type']} [{inc['severity']}]")
            print(f"    fingerprint: {inc['fingerprint']}")
            print(f"    message: {inc['message']}")

            # Store in memory
            result = store_incident(inc)
            print(f"    recurrence: {result['is_recurrence']}, run_count: {result['run_count']}")
            if result["prior_findings"]:
                print(f"    prior_findings: {result['prior_findings'][:200]}...")

            # Auto-fix on critical
            if inc["severity"] == "critical" and not args.no_auto_fix:
                fix = auto_fix(inc)
                print(f"    auto_fix: {fix}")
            elif args.no_auto_fix and inc["severity"] == "critical":
                print("    auto_fix: skipped (--no-auto-fix)")

    # Show incident memory
    try:
        with sqlite3.connect(DB_PATH, timeout=5.0) as conn:
            rows = conn.execute(
                "SELECT fingerprint, error_type, run_count, first_seen, last_seen FROM incidents"
            ).fetchall()
            if rows:
                print(f"\nINCIDENT MEMORY ({len(rows)} total):")
                for r in rows:
                    print(f"  {r[0]}  {r[1]:<20} runs={r[2]}  first={r[3][:19]}  last={r[4][:19]}")
    except sqlite3.Error as e:
        print(f"\n[warn] cannot read incident memory: {e}")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
