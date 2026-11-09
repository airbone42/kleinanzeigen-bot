"""Orphan Chromium on the browser profile is killed before a new session."""
import subprocess
import sys
import time
from pathlib import Path

from services.kleinanzeigen import KleinanzeigenService


def test_kills_process_holding_profile(tmp_path: Path) -> None:
    svc = KleinanzeigenService.__new__(KleinanzeigenService)
    svc.config_path = tmp_path
    profile = (tmp_path / "browser-profile").resolve()
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", f"--user-data-dir={profile}"])
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        time.sleep(0.5)
        svc._kill_orphan_browsers()
        assert victim.wait(timeout=5) != 0
        assert bystander.poll() is None
    finally:
        bystander.kill()
        victim.kill()
