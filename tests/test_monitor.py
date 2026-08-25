import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT))
import monitor  # noqa: E402


@pytest.fixture(autouse=True)


def _never_touch_real_bats(tmp_path, monkeypatch):
    """SAFETY: in tests auto_fix must never reach a real production bat.
    Guards against regressions where code reads PORT_RESTART_BATS but a test
    only patches the legacy RESTART_BAT (real incident: a test spawned the
    actual barozp restart bat and killed live servers).
    """
    fake = tmp_path / "fake_restart.bat"
    fake.write_bytes(b"echo fake")
    monkeypatch.setattr(monitor, "PORT_RESTART_BATS", {8083: str(fake), 8080: str(fake)})
    monkeypatch.setattr(monitor, "RESTART_BAT", str(fake))
    monkeypatch.setattr(monitor, "_CREATE_NEW_CONSOLE", 0)


def _concurrent_worker(db_path_str: str, fp: str, writes: int) -> None:
    """Spawn-process entry point: re-import monitor and write to the shared DB."""
    sys.path.insert(0, str(PROJECT))
    import monitor as m
    m.DB_PATH = Path(db_path_str)
    inc = {"fingerprint": fp, "error_type": "low_throughput", "message": "degraded", "severity": "warning"}
    for _ in range(writes):
        m.store_incident(inc)


def test_fingerprint_bucketing_low_throughput():
    """C-02: 8.2 vs 8.3 should give SAME fingerprint (bucketed)."""
    fp1 = monitor.fingerprint("low_throughput", "tok_s=8.2")
    fp2 = monitor.fingerprint("low_throughput", "tok_s=8.3")
    fp3 = monitor.fingerprint("low_throughput", "tok_s=10.4")
    assert fp1 == fp2 == fp3, f"bucketing failed: {fp1} {fp2} {fp3}"
    fp_other = monitor.fingerprint("high_latency", "tok_s=8.2")
    assert fp1 != fp_other


def test_fingerprint_bucketing_high_latency():
    fp1 = monitor.fingerprint("high_latency", "45.2s")
    fp2 = monitor.fingerprint("high_latency", "45.3s")
    fp3 = monitor.fingerprint("high_latency", "100s")
    assert fp1 == fp2 == fp3


def test_fingerprint_generation_error_normalized():
    fp1 = monitor.fingerprint("generation_error", "timeout after 5s")
    fp2 = monitor.fingerprint("generation_error", "timeout after 5.01s")
    fp3 = monitor.fingerprint("generation_error", "timeout after 10s")
    assert fp1 == fp2 == fp3
    fp_diff = monitor.fingerprint("generation_error", "connection refused")
    assert fp1 != fp_diff


def test_classify_both_triggers():
    """C-01: tok_s low AND latency high should produce TWO incidents (not elif)."""
    checks = {
        "checks": {
            "models": {"ok": True, "count": 1, "ids": ["test"]},
            "generation": {"tok_s": 5.0, "latency_s": 45.0, "completion_tokens": 50, "ok": True},
        }
    }
    incidents = monitor.classify_incident(checks)
    types = {i["error_type"] for i in incidents}
    assert "low_throughput" in types, f"low_throughput missing: {incidents}"
    assert "high_latency" in types, f"high_latency missing (elif bug): {incidents}"
    assert len(incidents) == 2


def test_classify_low_only():
    checks = {
        "checks": {
            "models": {"ok": True, "count": 1, "ids": ["test"]},
            "generation": {"tok_s": 5.0, "latency_s": 10.0, "completion_tokens": 50, "ok": True},
        }
    }
    incidents = monitor.classify_incident(checks)
    assert len(incidents) == 1 and incidents[0]["error_type"] == "low_throughput"


def test_classify_high_only():
    checks = {
        "checks": {
            "models": {"ok": True, "count": 1, "ids": ["test"]},
            "generation": {"tok_s": 20.0, "latency_s": 45.0, "completion_tokens": 50, "ok": True},
        }
    }
    incidents = monitor.classify_incident(checks)
    assert len(incidents) == 1 and incidents[0]["error_type"] == "high_latency"


def test_classify_healthy():
    checks = {
        "checks": {
            "models": {"ok": True, "count": 1, "ids": ["test"]},
            "generation": {"tok_s": 20.0, "latency_s": 10.0, "completion_tokens": 50, "ok": True},
        }
    }
    assert monitor.classify_incident(checks) == []


def test_classify_generation_error():
    checks = {
        "checks": {
            "models": {"ok": True, "count": 1, "ids": ["test"]},
            "generation": {"error": "model not found", "tok_s": 0, "latency_s": 1},
        }
    }
    incidents = monitor.classify_incident(checks)
    assert len(incidents) == 1 and incidents[0]["error_type"] == "generation_error"


def test_classify_server_unreachable():
    checks = {
        "checks": {
            "models": {"ok": False, "error": "HTTP 502", "count": 0, "ids": []},
            "generation": {"tok_s": 20, "latency_s": 5, "completion_tokens": 50, "ok": True},
        }
    }
    incidents = monitor.classify_incident(checks)
    assert any(i["error_type"] == "server_unreachable" for i in incidents)


def test_classify_single_incident_when_models_fail():
    """Audit P1: server unreachable must yield exactly ONE incident (no
    duplicate generation_error / low_throughput from a skipped probe)."""
    checks = {
        "checks": {
            "models": {"ok": False, "error": "[Errno 10061] connection refused", "count": 0, "ids": []},
            "slot": {"ok": False, "status": "not_checked", "note": "server_unreachable"},
            "generation": {"tok_s": 0, "latency_s": 0.0, "completion_tokens": 0, "skipped": "server_unreachable"},
        }
    }
    incidents = monitor.classify_incident(checks)
    assert len(incidents) == 1, f"expected single incident, got: {incidents}"
    assert incidents[0]["error_type"] == "server_unreachable"
    assert incidents[0]["severity"] == "critical"


def test_check_server_skips_all_probes_when_models_fail():
    """Audit P1: when /v1/models fails, neither /health nor the generation
    probe may run — no GEN_TIMEOUT wait, no duplicate incidents."""
    calls = []
    def side_effect(path, body=None, timeout=120, base=None):
        calls.append(path)
        return {"error": "[Errno 10061] connection refused"}
    with patch.object(monitor, "api", side_effect=side_effect):
        result = monitor.check_server()
    assert result["checks"]["models"]["ok"] is False
    assert result["checks"]["generation"].get("skipped") == "server_unreachable"
    assert calls == ["/models"], f"only /models must be called, got: {calls}"


def test_classify_server_busy_warning():
    """Busy slot → server_busy warning, NOT critical generation_error."""
    checks = {
        "checks": {
            "models": {"ok": True, "count": 1, "ids": ["test"]},
            "slot": {"ok": False, "status": "no slot available", "slots_idle": 0, "slots_processing": 1},
            "generation": {"tok_s": 0, "latency_s": 0.0, "completion_tokens": 0, "skipped": "slot_busy: no slot available"},
        }
    }
    incidents = monitor.classify_incident(checks)
    assert len(incidents) == 1, f"expected single server_busy: {incidents}"
    inc = incidents[0]
    assert inc["error_type"] == "server_busy"
    assert inc["severity"] == "warning"


def test_classify_slot_ok_no_busy():
    checks = {
        "checks": {
            "models": {"ok": True, "count": 1, "ids": ["test"]},
            "slot": {"ok": True, "status": "ok"},
            "generation": {"tok_s": 20.0, "latency_s": 10.0, "completion_tokens": 50, "ok": True},
        }
    }
    assert monitor.classify_incident(checks) == []


def test_fingerprint_server_busy_stable():
    fp1 = monitor.fingerprint("server_busy", "")
    fp2 = monitor.fingerprint("server_busy", "")
    assert fp1 == fp2


def test_check_server_skips_probe_when_slot_busy():
    """When /health says no slot available, the generation probe must not run."""
    calls = []
    def side_effect(path, body=None, timeout=120, base=None):
        calls.append(path)
        if path.endswith("/models"):
            return {"data": [{"id": "m"}]}
        if path.endswith("/health"):
            return {"status": "no slot available", "slots_idle": 0, "slots_processing": 1}
        return {}
    with patch.object(monitor, "api", side_effect=side_effect):
        result = monitor.check_server()
    assert result["checks"]["slot"]["ok"] is False
    assert result["checks"]["generation"].get("skipped")
    assert not any(c.endswith("/chat/completions") for c in calls), f"probe must be skipped when slot busy: {calls}"
    assert any(c.endswith("/health") and "/v1/health" not in c for c in calls), f"/health must hit server root: {calls}"


def test_check_server_slot_ok_runs_probe():
    def side_effect(path, body=None, timeout=120, base=None):
        if path.endswith("/models"):
            return {"data": [{"id": "m"}]}
        if path.endswith("/health"):
            return {"status": "ok"}
        return {"choices": [], "usage": {}}
    with patch.object(monitor, "api", side_effect=side_effect):
        result = monitor.check_server()
    assert result["checks"]["slot"]["ok"] is True
    assert "skipped" not in result["checks"]["generation"]


def test_measure_gen_timeout_passthrough():
    captured = {}
    def side_effect(path, body=None, timeout=120, base=None):
        captured[path] = timeout
        if path == "/models":
            return {"data": [{"id": "m"}]}
        return {"choices": [{"message": {"content": "hello"}}], "usage": {"completion_tokens": 3}}
    with patch.object(monitor, "api", side_effect=side_effect):
        monitor.measure_tok_s(10)
        assert captured["/chat/completions"] == monitor.GEN_TIMEOUT
        monitor.measure_tok_s(10, timeout=7)
        assert captured["/chat/completions"] == 7


def test_store_incident_recurrence(tmp_path):
    """C-03: store should handle recurrence via ON CONFLICT without crashing."""
    db = tmp_path / "test_incidents.db"
    with patch.object(monitor, "DB_PATH", db):
        monitor.init_db()
        inc = {"fingerprint": monitor.fingerprint("low_throughput", ""), "error_type": "low_throughput", "message": "degraded", "severity": "warning"}
        r1 = monitor.store_incident(inc)
        assert r1["is_recurrence"] is False and r1["run_count"] == 1
        r2 = monitor.store_incident(inc)
        assert r2["is_recurrence"] is True and r2["run_count"] == 2
        assert r2["prior_findings"] is not None
        with sqlite3.connect(db, timeout=5.0) as conn:
            cnt = conn.execute("SELECT run_count FROM incidents WHERE fingerprint=?", (inc["fingerprint"],)).fetchone()[0]
            assert cnt == 2
            runs = conn.execute("SELECT count(*) FROM agent_runs WHERE incident_id=?", (inc["fingerprint"],)).fetchone()[0]
            assert runs == 2
            ended = conn.execute("SELECT ended_at FROM agent_runs LIMIT 1").fetchone()[0]
            assert ended is not None


def test_store_incident_no_double_timestamp(tmp_path):
    """C-12: first_seen and last_seen should be equal on first insert (single now)."""
    db = tmp_path / "test_incidents2.db"
    with patch.object(monitor, "DB_PATH", db):
        monitor.init_db()
        inc = {"fingerprint": monitor.fingerprint("high_latency", ""), "error_type": "high_latency", "message": "45s", "severity": "warning"}
        monitor.store_incident(inc)
        with sqlite3.connect(db, timeout=5.0) as conn:
            first, last = conn.execute("SELECT first_seen, last_seen FROM incidents WHERE fingerprint=?", (inc["fingerprint"],)).fetchone()
            assert first == last, f"first {first} != last {last} — two datetime.now() bug"


def test_store_incident_concurrent_processes_no_lost_updates(tmp_path):
    """Audit follow-up: parallel processes writing the SAME fingerprint must not
    lose updates — INSERT + IntegrityError + UPDATE keeps run_count exact."""
    import multiprocessing as mp
    db = tmp_path / "race.db"
    with patch.object(monitor, "DB_PATH", db):
        monitor.init_db()
    fp = monitor.fingerprint("low_throughput", "")
    workers, writes = 4, 5
    ctx = mp.get_context("spawn")  # same semantics on Linux CI and Windows
    procs = [ctx.Process(target=_concurrent_worker, args=(str(db), fp, writes)) for _ in range(workers)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
    assert all(p.exitcode == 0 for p in procs), f"worker crashed: {[p.exitcode for p in procs]}"
    with sqlite3.connect(db, timeout=5.0) as conn:
        cnt, run_count = conn.execute(
            "SELECT count(*), sum(run_count) FROM incidents WHERE fingerprint=?", (fp,)
        ).fetchone()
        runs = conn.execute("SELECT count(*) FROM agent_runs").fetchone()[0]
    assert cnt == 1, f"fingerprint must be a single row, got {cnt}"
    assert run_count == workers * writes, f"lost updates: {run_count} != {workers * writes}"
    assert runs == workers * writes, f"agent_runs lost: {runs} != {workers * writes}"


def test_init_db_wal(tmp_path):
    db = tmp_path / "wal.db"
    with patch.object(monitor, "DB_PATH", db):
        monitor.init_db()
        with sqlite3.connect(db, timeout=5.0) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal"


def test_measure_no_usage_returns_error():
    mock_resp = {"choices": [], "usage": {}}
    with patch.object(monitor, "api") as mock_api:
        def side_effect(path, body=None, timeout=10, base=None):
            if path == "/models":
                return {"data": [{"id": "test-model"}]}
            if path == "/chat/completions":
                return mock_resp
            return {}
        mock_api.side_effect = side_effect
        result = monitor.measure_tok_s(100)
        assert "error" in result, f"should be error when no tokens: {result}"
        assert result["tok_s"] == 0


def test_measure_with_text_fallback():
    mock_resp = {
        "choices": [{"message": {"content": "hello world " * 20}}],
        "usage": {"completion_tokens": 0},
    }
    with patch.object(monitor, "api") as mock_api:
        with patch.object(monitor.time, "time", side_effect=[1000.0, 1002.0]):
            mock_api.side_effect = lambda path, body=None, timeout=10, base=None: {"data": [{"id": "m"}]} if path == "/models" else mock_resp
            result = monitor.measure_tok_s(100)
        assert result.get("completion_tokens", 0) > 0, f"should estimate tokens: {result}"
        assert result["tok_s"] > 0
        assert result.get("estimated") is True, "text-length fallback must be flagged as estimated"


def test_api_http_error_status():
    """C-09: api should return status on HTTPError."""
    import urllib.error
    with patch("urllib.request.urlopen") as mock_urlopen:
        err = urllib.error.HTTPError("http://test", 500, "Internal", {}, None)
        err.read = MagicMock(return_value=b"server error")
        mock_urlopen.side_effect = err
        res = monitor.api("/models", timeout=5)
        assert "error" in res
        assert res.get("status") == 500


def test_api_success():
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"data": [{"id": "m"}]}'
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp
        res = monitor.api("/models")
        assert res["data"][0]["id"] == "m"


def test_check_server_no_dead_try():
    """C-10: check_server should not have dead try/except."""
    with patch.object(monitor, "api") as mock_api:
        mock_api.side_effect = lambda path, body=None, timeout=10, base=None: {"data": [{"id": "x"}]}
        result = monitor.check_server()
        assert "checks" in result
        assert "models" in result["checks"]


def test_auto_fix_skips_warning(tmp_path):
    db = tmp_path / "autofix.db"
    with patch.object(monitor, "DB_PATH", db):
        monitor.init_db()
        inc = {"fingerprint": "abc", "error_type": "low_throughput", "severity": "warning", "message": "low"}
        res = monitor.auto_fix(inc)
        assert res["action"] == "skipped"


def test_auto_fix_uses_models_not_health(tmp_path):
    """C-06: auto_fix should check /models, not /health."""
    db = tmp_path / "autofix2.db"
    with patch.object(monitor, "DB_PATH", db):
        monitor.init_db()
        inc = {"fingerprint": monitor.fingerprint("server_unreachable", "err"), "error_type": "server_unreachable", "severity": "critical", "message": "down"}
        with patch.object(monitor, "RESTART_BAT", str(tmp_path / "fake.bat")), \
             patch.object(monitor, "RESTART_CWD", str(tmp_path)), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("subprocess.Popen") as mock_popen, \
             patch.object(monitor, "api") as mock_api, \
             patch("time.sleep", return_value=None):
            mock_popen.return_value.pid = 1234
            mock_api.return_value = {"data": [{"id": "m"}]}
            res = monitor.auto_fix(inc)
            calls = [c[0][0] for c in mock_api.call_args_list]
            assert "/models" in calls, f"auto_fix should poll /models, got {calls}"
            assert res["action"] == "restarted"


def test_auto_fix_cmd_c_for_bat(tmp_path):
    db = tmp_path / "autofix3.db"
    with patch.object(monitor, "DB_PATH", db):
        monitor.init_db()
        inc = {"fingerprint": monitor.fingerprint("server_unreachable", "err2"), "error_type": "server_unreachable", "severity": "critical", "message": "down2"}
        fake_bat = tmp_path / "run.bat"
        fake_bat.write_text("echo hi")
        with patch.object(monitor, "RESTART_BAT", str(fake_bat)), \
             patch.object(monitor, "RESTART_CWD", str(tmp_path)), \
             patch("subprocess.Popen") as mock_popen, \
             patch.object(monitor, "api", return_value={"data": []}), \
             patch("time.sleep", return_value=None):
            mock_popen.return_value.pid = 999
            monitor.auto_fix(inc)
            cmd = mock_popen.call_args[0][0]
            # Updated to use full path for security (S607)
            assert cmd[:2] == [r"C:\Windows\System32\cmd.exe", "/c"]


def test_auto_fix_cooldown_counts_from_last_attempt(tmp_path):
    """Cooldown measures from last ACTUAL restart attempt, not incident last_seen.

    Regression guard: store_incident refreshes last_seen on every monitoring
    run, so a persistent incident would block restart forever (elapsed=0).
    """
    db = tmp_path / "cooldown.db"
    with patch.object(monitor, "DB_PATH", db):
        monitor.init_db()
        fp = monitor.fingerprint("server_unreachable", "err")
        inc = {"fingerprint": fp, "error_type": "server_unreachable", "severity": "critical", "message": "down"}
        monitor.store_incident(inc)
        monitor.store_incident(inc)  # recurrence bumps run_count AND last_seen
        # Simulate a restart attempt made right now
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO agent_runs (incident_id, started_at, ended_at, status, actions_json) VALUES (?, ?, ?, ?, ?)",
                (fp, now, now, "restart_attempted", "{}"),
            )
            conn.commit()
        res = monitor.auto_fix(inc)
        assert res["action"] == "skipped" and "cooldown" in res["reason"]


def test_auto_fix_proceeds_when_no_prior_attempts(tmp_path):
    """Fresh critical incident (no restart history) must actually restart."""
    db = tmp_path / "fresh.db"
    with patch.object(monitor, "DB_PATH", db):
        monitor.init_db()
        fp = monitor.fingerprint("server_unreachable", "err3")
        inc = {"fingerprint": fp, "error_type": "server_unreachable", "severity": "critical", "message": "down"}
        monitor.store_incident(inc)
        with patch("subprocess.Popen") as mock_popen, \
             patch.object(monitor, "api", return_value={"data": []}), \
             patch("time.sleep", return_value=None):
            mock_popen.return_value.pid = 777
            res = monitor.auto_fix(inc)
        assert res["action"] == "restarted", res
        # attempt must be recorded for future cooldown checks
        with sqlite3.connect(db) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM agent_runs WHERE incident_id=? AND status='restart_attempted'", (fp,)
            ).fetchone()[0]
        assert n == 1


def test_monitor_help():
    """D: --help should exit 0."""
    import subprocess
    result = subprocess.run([sys.executable, str(PROJECT / "monitor.py"), "--help"], capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert "monitor" in result.stdout.lower() or "usage" in result.stdout.lower()


def test_no_hardcoded_f_in_monitor():
    """C-08: no hardcoded F:\\ assignments in code logic."""
    content = (PROJECT / "monitor.py").read_text(encoding="utf-8")
    lines = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "help=" in line:
            continue
        if ("F:\\\\" in line or "F:/" in line) and "=" in line and "F:" in line:
                lines.append(line)
    assert len(lines) == 0, f"found hardcoded F:\\ lines: {lines}"


def test_ruff_clean():
    """E: ruff should pass on main modules."""
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", str(PROJECT / "monitor.py"), str(PROJECT / "demo_incident.py"), str(PROJECT / "make_icon.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    if "No module named ruff" in result.stderr:
        import pytest
        pytest.skip("ruff not installed")
    assert result.returncode == 0, f"ruff failed:\n{result.stdout}\n{result.stderr}"


# --- Multi-port failover tests ---


def test_db_for_port_primary_and_fallback():
    """Primary port keeps legacy incidents.db; fallback gets incidents_<port>.db."""
    assert monitor.db_for_port(8083, 8083) == monitor.DB_PATH
    assert monitor.db_for_port(8080, 8083) == monitor.DB_PATH.parent / "incidents_8080.db"
    # explicit override wins
    assert monitor.db_for_port(8080, 8083, db_override="x.db") == Path("x.db")


def test_api_base_param_overrides_base():
    """api(base=...) must target the given server, not module BASE."""
    captured = {}
    class FakeResp:
        def read(self):
            return b'{"data": []}'
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return FakeResp()
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        monitor.api("/models", timeout=5, base="http://localhost:8080/v1")
    assert captured["url"].startswith("http://localhost:8080/v1"), captured["url"]


def test_check_server_passes_base():
    """check_server(base=...) probes the given base (models + health)."""
    seen = []
    def side_effect(path, body=None, timeout=120, base=None):
        seen.append((path, base))
        if path.endswith("/models"):
            return {"data": [{"id": "m"}]}
        if path.endswith("/health"):
            return {"status": "ok"}
        return {"choices": [{"message": {"content": "x" * 40}}], "usage": {"completion_tokens": 10}}
    with patch.object(monitor, "api", side_effect=side_effect):
        result = monitor.check_server(base="http://localhost:8080/v1")
    assert result["checks"]["models"]["ok"] is True
    assert any(b == "http://localhost:8080/v1" for _, b in seen)


def test_port_restart_bats_has_both_ports():
    """Per-port restart bats configured for both monitored ports."""
    assert 8083 in monitor.PORT_RESTART_BATS
    assert 8080 in monitor.PORT_RESTART_BATS
