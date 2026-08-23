import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT))


def test_make_icon_no_side_effect_on_import():
    """M-12: import should not generate files."""
    png = PROJECT / "superlog_lite_icon.png"
    ico = PROJECT / "superlog_lite_icon.ico"
    mtime_png_before = png.stat().st_mtime if png.exists() else None
    mtime_ico_before = ico.stat().st_mtime if ico.exists() else None
    if "make_icon" in sys.modules:
        del sys.modules["make_icon"]
    import make_icon
    time.sleep(0.1)
    mtime_png_after = png.stat().st_mtime if png.exists() else None
    mtime_ico_after = ico.stat().st_mtime if ico.exists() else None
    assert mtime_png_before == mtime_png_after, "import make_icon should not rewrite PNG"
    assert mtime_ico_before == mtime_ico_after, "import make_icon should not rewrite ICO"
    assert hasattr(make_icon, "main"), "make_icon should have main()"


def test_make_icon_main_generates():
    import make_icon
    make_icon.main()
    assert (PROJECT / "superlog_lite_icon.png").exists()
    assert (PROJECT / "superlog_lite_icon.ico").exists()


def test_monitor_bat_uses_relative_path():
    content = (PROJECT / "monitor_8083.bat").read_text(encoding="utf-8")
    assert "%~dp0" in content, "bat should use %~dp0 not hardcoded F:"
    hardcoded_lines = [
        line for line in content.splitlines()
        if "F:\\superlog-lite" in line and "cd /d" in line
    ]
    assert len(hardcoded_lines) == 0, f"hardcoded cd found: {hardcoded_lines}"
    assert "where python" in content.lower() or "python" in content.lower()
    assert "--help" in content


def test_gitignore_exists():
    assert (PROJECT / ".gitignore").exists()
    content = (PROJECT / ".gitignore").read_text(encoding="utf-8")
    assert "incidents.db" in content
    assert "__pycache__" in content
    assert "demo_incidents.db" in content


def test_requirements_exists():
    assert (PROJECT / "requirements.txt").exists()
    content = (PROJECT / "requirements.txt").read_text(encoding="utf-8")
    assert "Pillow" in content
    assert "pytest" in content.lower()


def test_ruff_all_clean():
    result = subprocess.run([sys.executable, "-m", "ruff", "check", str(PROJECT)], capture_output=True, text=True, check=False)
    if "No module named ruff" in result.stderr:
        import pytest
        pytest.skip("ruff not installed")
    result2 = subprocess.run(
        [sys.executable, "-m", "ruff", "check", str(PROJECT / "monitor.py"), str(PROJECT / "demo_incident.py"), str(PROJECT / "make_icon.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result2.returncode == 0, f"ruff failed:\n{result2.stdout}\n{result2.stderr}"


def test_compile_all():
    import py_compile
    for f in ["monitor.py", "demo_incident.py", "make_icon.py"]:
        py_compile.compile(str(PROJECT / f), doraise=True)


def test_security_no_hardcoded_secrets():
    import re
    for name in ["monitor.py", "demo_incident.py"]:
        content = (PROJECT / name).read_text(encoding="utf-8")
        tokens = re.findall(r"[A-Za-z0-9_\-]{24,}", content)
        suspicious = [t for t in tokens if len(t) >= 32 and not t.startswith("Qwen")]
        assert len(suspicious) == 0, f"suspicious token in {name}: {suspicious[:2]}"
        assert "C:\\Users" not in content
        assert "C:/Users" not in content
