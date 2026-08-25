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

# Multi-port failover: priority order, first reachable port is monitored;
# remaining ports are fallbacks checked when higher-priority ones are down.
DEFAULT_PORTS = [int(p.strip()) for p in os.getenv("SUPERLOG_PORTS", "8083,8080").split(",") if p.strip()]

# Restart bat per port — env overrides, fallback to sibling dirs for backward compat.
# NOTE: these bats kill "competitor" llama-servers on sibling ports before starting,
# so auto-fixing a down primary while a fallback is serving would kill the fallback.
_default_restart = str(Path(__file__).parent.parent / "barozp-opus-8083" / "run_barozp_8083_mtp.bat")
_default_restart_8080 = str(Path(__file__).parent.parent / "ik_llama.cpp" / "run_ik_qwen38.bat")
PORT_RESTART_BATS = {
    8083: os.getenv("SUPERLOG_RESTART_BAT", _default_restart),
    8080: os.getenv("SUPERLOG_RESTART_BAT_8080", _default_restart_8080),
}
RESTART_BAT = PORT_RESTART_BATS.get(8083, _default_restart)
RESTART_CWD = os.getenv("SUPERLOG_RESTART_CWD", str(Path(RESTART_BAT).parent) if RESTART_BAT else "")


def db_for_port(port: int, primary_port: int, db_override=None) -> Path:
    """Per-port incident DB. Primary keeps legacy incidents.db; others get incidents_<port>.db."""
    if db_override:
        return Path(db_override)
    if port == primary_port:
        return DB_PATH
    return DB_PATH.parent / f"incidents_{port}.db"

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


def api(path, body=None, timeout=120, base=None):
    """Make API call to local LLM server. Returns dict; on error {"error":..., "status":...}.

    base overrides the module-level BASE (multi-port monitoring).
    """
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
    # Endpoint is a local-LLM endpoint (http://localhost:port) configured at startup;
    # not user-supplied input, so file:// risk does not apply. S310 silenced.
    b = base if base is not None else BASE
    url = path if path.startswith("http") else b + path
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


def measure_tok_s(max_tokens=100, timeout=None, base=None):
    """Measure tokens/second with a real generation test (dynamic model)."""
    # Try to discover model from /v1/models
    model = DEFAULT_MODEL_FALLBACK
    try:
        models_resp = api("/models", timeout=10, base=base)
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
        base=base,
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


def _server_root(base=None) -> str:
    """Server root URL: strip a trailing /v1 from base (health lives above it)."""
    b = (base if base is not None else BASE).rstrip("/")
    return b[:-3] if b.endswith("/v1") else b


def check_server(base=None):
    """Full health check: /models + generation test (against given base)."""
    result = {
        "timestamp": now_iso(),
        "checks": {},
    }

    # Check 1: /v1/models reachable (api never throws, no try needed)
    models = api("/models", timeout=10, base=base)
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
    health = api(_server_root(base) + "/health", timeout=5, base=base)
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
        result["checks"]["generation"] = measure_tok_s(DEFAULT_MAX_TOKENS, base=base)
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


def auto_fix(incident, port=None, db_path=None):
    """Restart the LLM server on critical incidents (with cooldown).

    port selects the per-port restart bat; db_path selects the cooldown DB.
    """
    # Only auto-fix critical incidents (not warnings like low_throughput)
    if incident["severity"] != "critical":
        print(f"    [auto-fix] skipped (severity={incident['severity']})")
        return {"action": "skipped", "reason": f"severity={incident['severity']}"}

    target_bat = PORT_RESTART_BATS.get(port or 8083, RESTART_BAT)
    cooldown_db = db_path or DB_PATH

    # Cooldown: avoid restart storm. Measure from the last ACTUAL restart
    # attempt (agent_runs.status='restart_attempted'), NOT from the incident's
    # last_seen — store_incident refreshes last_seen on every monitoring run,
    # so a persistent incident would keep elapsed≈0 forever and never restart.
    try:
        with sqlite3.connect(cooldown_db, timeout=5.0) as conn:
            row = conn.execute(
                "SELECT ended_at FROM agent_runs WHERE incident_id=? AND status='restart_attempted' "
                "ORDER BY id DESC LIMIT 1",
                (incident["fingerprint"],),
            ).fetchone()
            if row:
                try:
                    last_restart = _parse_ts(row[0])
                    elapsed = (datetime.now(timezone.utc) - last_restart).total_seconds()
                    if elapsed < RESTART_COOLDOWN_S:
                        msg = f"cooldown {RESTART_COOLDOWN_S}s, last restart {int(elapsed)}s ago"
                        print(f"    [auto-fix] skipped (cooldown: {msg})")
                        return {"action": "skipped", "reason": f"cooldown: {msg}"}
                except (ValueError, sqlite3.Error) as e:
                    logger.debug("cooldown check failed: %s", e)
    except (sqlite3.Error, ValueError) as e:
        logger.debug("cooldown DB access failed: %s", e)

    # Record the restart attempt BEFORE spawning (storm guard for concurrent runs)
    now_ts = now_iso()
    try:
        with sqlite3.connect(cooldown_db, timeout=5.0) as conn:
            conn.execute(
                "INSERT INTO agent_runs (incident_id, started_at, ended_at, status, actions_json) "
                "VALUES (?, ?, ?, 'restart_attempted', ?)",
                (incident["fingerprint"], now_ts, now_ts, json.dumps({"bat": target_bat})),
            )
            conn.commit()
    except sqlite3.Error as e:
        logger.debug("restart attempt bookkeeping failed: %s", e)

    print("    [auto-fix] RESTARTING server...")
    print(f"    [auto-fix] bat: {target_bat}")

    if not Path(target_bat).exists():
        return {"action": "failed", "error": f"restart bat not found: {target_bat}"}

    restart_cwd = str(Path(target_bat).parent)

    try:
        # Use cmd /c for reliable .bat execution on Windows.
        # `cmd` is a hardcoded Windows shell binary (always in PATH), not
        # user-supplied input. S603/S607 do not apply.
        proc = subprocess.Popen(  # noqa: S603
            [r"C:\Windows\System32\cmd.exe", "/c", target_bat],
            creationflags=_CREATE_NEW_CONSOLE,
            cwd=restart_cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"    [auto-fix] PID={proc.pid}")

        # Wait for server to come back (up to 120s) — check /models (not /health)
        restart_base = f"http://localhost:{port or 8083}/v1"
        print("    [auto-fix] waiting for server to come back...")
        for i in range(24):  # 24 x 5s = 120s max
            time.sleep(5)
            health = api("/models", timeout=5, base=restart_base)
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


def _print_checks(tag, checks):
    print(f"\nTimestamp: {checks['timestamp']}  [{tag}]")
    print("CHECKS:")
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


def _show_memory(db_file):
    """Print incident memory summary for one per-port DB."""
    try:
        with sqlite3.connect(db_file, timeout=5.0) as conn:
            rows = conn.execute(
                "SELECT fingerprint, error_type, run_count, first_seen, last_seen FROM incidents"
            ).fetchall()
            if rows:
                print(f"\nINCIDENT MEMORY ({db_file.name}) — {len(rows)} total:")
                for r in rows:
                    print(f"  {r[0]}  {r[1]:<20} runs={r[2]}  first={r[3][:19]}  last={r[4][:19]}")
    except sqlite3.Error as e:
        print(f"\n[warn] cannot read incident memory ({db_file.name}): {e}")


def main():
    # CLI entry point: CLI flags override module-level defaults. Global mutation
    # here is intentional and the only mutation point for these constants.
    global DB_PATH, TOK_S_THRESHOLD, LATENCY_THRESHOLD, GEN_TIMEOUT  # noqa: PLW0603
    parser = argparse.ArgumentParser(
        description="Superlog-lite: monitor local LLM servers (multi-port failover)"
    )
    parser.add_argument(
        "--ports",
        type=str,
        default=",".join(str(p) for p in DEFAULT_PORTS),
        help=f"priority-ordered ports (default: {','.join(str(p) for p in DEFAULT_PORTS)}). "
        "First reachable port is monitored; the rest are failover targets.",
    )
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

    ports = [int(p.strip()) for p in args.ports.split(",") if p.strip().isdigit()]
    if not ports:
        ports = list(DEFAULT_PORTS)
    primary_port = ports[0]
    db_override = str(DB_PATH) if args.db else None

    init_db()

    print("=" * 60)
    print(f"SUPERLOG-LITE: monitoring ports {' -> '.join(str(p) for p in ports)} (failover order)")
    print("=" * 60)

    # --- Failover scan: first reachable port becomes the monitored one ---
    active_port = None
    checks_by_port = {}
    for port in ports:
        base = f"http://localhost:{port}/v1"
        checks_by_port[port] = check_server(base=base)
        if checks_by_port[port]["checks"]["models"]["ok"]:
            active_port = port
            break

    if active_port is not None:
        idx = ports.index(active_port)
        down_ports = ports[:idx]
        tag = f":{active_port}"
        if down_ports:
            tag += f" — FAILOVER ({' '.join(':' + str(p) + ' down' for p in down_ports)})"
        checks = checks_by_port[active_port]
        _print_checks(tag, checks)

        # Log unreachable incidents for every DOWN higher-priority port
        # into ITS OWN per-port DB ("отдельный лог по этому порту").
        for dp in down_ports:
            dp_db = Path(db_for_port(dp, primary_port, db_override=db_override))
            init_db(dp_db)
            dp_err = checks_by_port[dp]["checks"]["models"].get("error", "unknown")
            dp_inc = {
                "fingerprint": fingerprint("server_unreachable", f"port_{dp}_{dp_err}"),
                "error_type": "server_unreachable",
                "severity": "critical",
                "message": f":{dp} unreachable (failover active on :{active_port})",
            }
            r = store_incident(dp_inc, db_path=dp_db)
            print(f"\n  • :{dp} server_unreachable [critical] -> logged to {dp_db.name}")
            print(f"    recurrence: {r['is_recurrence']}, run_count: {r['run_count']}")

        # Classify incidents against the ACTIVE server only
        incidents = classify_incident(checks)
        db_file = Path(db_for_port(active_port, primary_port, db_override=db_override))
        if incidents:
            init_db(db_file)

        if not incidents:
            gen = checks["checks"]["generation"]
            print("\nNO INCIDENTS — server healthy")
            if not gen.get("skipped"):
                print(f"   tok/s: {gen.get('tok_s', 0):.1f} (threshold: {TOK_S_THRESHOLD})")
                print(f"   latency: {gen.get('latency_s', 0):.2f}s (threshold: {LATENCY_THRESHOLD}s)")
        else:
            print(f"\n{len(incidents)} INCIDENT(S) DETECTED on :{active_port}:")
            for inc in incidents:
                print(f"\n  • {inc['error_type']} [{inc['severity']}]")
                print(f"    fingerprint: {inc['fingerprint']}")
                print(f"    message: {inc['message']}")

                # Store in per-port memory
                result = store_incident(inc, db_path=db_file)
                print(f"    recurrence: {result['is_recurrence']}, run_count: {result['run_count']}")
                if result["prior_findings"]:
                    print(f"    prior_findings: {result['prior_findings'][:200]}...")

                # Auto-fix on critical — restarts THIS port's server
                if inc["severity"] == "critical" and not args.no_auto_fix:
                    fix = auto_fix(inc, port=active_port, db_path=db_file)
                    print(f"    auto_fix: {fix}")
                elif args.no_auto_fix and inc["severity"] == "critical":
                    print("    auto_fix: skipped (--no-auto-fix)")
    else:
        # --- All ports down: single critical incident, restart the PRIMARY ---
        print("\nALL PORTS DOWN:")
        for port in ports:
            err = checks_by_port[port]["checks"]["models"].get("error", "unknown")
            print(f"  :{port} -> {err[:100]}")

        err = checks_by_port[primary_port]["checks"]["models"].get("error", "all_ports_down")
        inc = {
            "fingerprint": fingerprint("server_unreachable", err),
            "error_type": "server_unreachable",
            "severity": "critical",
            "message": f"All monitored ports down: {ports}",
        }
        print("\n  • all_ports_down [critical]")
        print(f"    fingerprint: {inc['fingerprint']}")

        db_file = Path(db_for_port(primary_port, primary_port, db_override=db_override))
        init_db(db_file)
        result = store_incident(inc, db_path=db_file)
        print(f"    recurrence: {result['is_recurrence']}, run_count: {result['run_count']}")

        # Restart ONLY the primary bat. The bats kill sibling llama-servers
        # before starting, so restarting a fallback here would be destructive;
        # bringing the primary back also restores the normal priority order.
        # WARNING for the user: with a fallback still serving, the restart bat
        # will KILL it (competitor-kill) before starting the primary.
        if not args.no_auto_fix:
            print("    [auto-fix] restarting PRIMARY server")
            if len(ports) > 1:
                print("    [auto-fix] NOTE: primary's bat kills sibling llama-servers on other ports first")
            fix = auto_fix(inc, port=primary_port, db_path=db_file)
            print(f"    auto_fix: {fix}")
        else:
            print("    auto_fix: skipped (--no-auto-fix)")

    # --- Incident memory across all port DBs ---
    for port in ports:
        db_file = Path(db_for_port(port, primary_port, db_override=db_override))
        if db_file.exists():
            _show_memory(db_file)

    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
