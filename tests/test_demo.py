import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT))

import demo_incident  # noqa: E402
import monitor  # noqa: E402


def test_demo_imports_from_monitor():
    """C-13: demo should import fingerprint from monitor (single source)."""
    import inspect
    src = inspect.getsource(demo_incident)
    assert "from monitor import" in src, "demo should import from monitor"
    assert src.count("def fingerprint") == 0, "demo should not redefine fingerprint"


def test_demo_separate_db(tmp_path):
    """C-15: demo should default to demo_incidents.db, not incidents.db."""
    assert demo_incident.DEMO_DB_PATH.name == "demo_incidents.db"
    assert demo_incident.DEMO_DB_PATH != monitor.DB_PATH

    demo_db = tmp_path / "demo_test.db"
    real_db = tmp_path / "real_test.db"
    with patch.object(demo_incident, "DEMO_DB_PATH", demo_db), patch.object(monitor, "DB_PATH", real_db):
        monitor.init_db()
        demo_incident.init_db(demo_db)
        demo_incident.store_incident("low_throughput", "tok_s=8.2", db_path=demo_db)
        with sqlite3.connect(real_db, timeout=5.0) as conn:
            cnt_real = conn.execute("SELECT count(*) FROM incidents").fetchone()[0]
            assert cnt_real == 0, f"demo polluted real DB: {cnt_real}"
        with sqlite3.connect(demo_db, timeout=5.0) as conn:
            cnt_demo = conn.execute("SELECT count(*) FROM incidents").fetchone()[0]
            assert cnt_demo == 1


def test_demo_no_double_update(tmp_path):
    """C-14: store should not do double UPDATE for findings."""
    import inspect
    src = inspect.getsource(demo_incident.store_incident)
    assert src.count("UPDATE incidents SET findings") == 0, "double findings UPDATE should be removed"
    db = tmp_path / "double.db"
    demo_incident.init_db(db)
    r1 = demo_incident.store_incident("high_latency", "latency=45s", db_path=db)
    assert r1["run_count"] == 1 and not r1["is_recurrence"]
    r2 = demo_incident.store_incident("high_latency", "latency=45s", db_path=db)
    assert r2["run_count"] == 2 and r2["is_recurrence"]
    with sqlite3.connect(db, timeout=5.0) as conn:
        findings = conn.execute("SELECT findings FROM incidents WHERE fingerprint=?", (r1["fingerprint"],)).fetchone()[0]
        assert findings.count("Initial investigation") == 1


def test_demo_main_no_pollution(tmp_path):
    """Running demo_incident main with --db should not touch real DB."""
    import subprocess
    demo_db = tmp_path / "main_demo.db"
    result = subprocess.run(
        [sys.executable, str(PROJECT / "demo_incident.py"), "--db", str(demo_db)],
        capture_output=True,
        text=True,
        cwd=str(PROJECT),
        check=False,
    )
    assert result.returncode == 0, f"demo failed: {result.stderr}"
    assert demo_db.exists()
    assert "DEMO COMPLETE" in result.stdout
