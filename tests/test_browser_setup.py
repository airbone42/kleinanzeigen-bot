"""Browser setup: system Chromium wins, sandbox flags only as root."""
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import bot.main as main


def _run_setup(tmp_path: Path, euid: int) -> dict:
    (tmp_path / "config.yaml").write_text(
        yaml.dump({"browser": {"arguments": ["--no-sandbox", "--headless"]}})
    )
    with patch.object(main.settings, "kleinanzeigen_config_path", tmp_path), patch.object(
        main.settings, "chromium_path", "/bin/sh"
    ), patch("bot.main.os.geteuid", return_value=euid, create=True):
        main._setup_browser()
    return yaml.safe_load((tmp_path / "config.yaml").read_text())["browser"]


def test_unprivileged_keeps_sandbox(tmp_path: Path) -> None:
    browser = _run_setup(tmp_path, euid=1000)
    assert browser["binary_location"] == "/bin/sh"
    assert not set(main.SANDBOX_FLAGS) & set(browser["arguments"])
    assert "--headless" not in browser["arguments"]


def test_root_disables_sandbox(tmp_path: Path) -> None:
    browser = _run_setup(tmp_path, euid=0)
    assert set(main.SANDBOX_FLAGS) <= set(browser["arguments"])


@pytest.mark.parametrize("configured", ["/does/not/exist"])
def test_missing_configured_chromium_is_none(configured: str) -> None:
    with patch.object(main.settings, "chromium_path", configured):
        assert main._find_chromium() is None


def _lock(profile: Path, target: str) -> None:
    profile.mkdir(exist_ok=True)
    for name, dest in (("SingletonLock", target), ("SingletonSocket", "/tmp/x/s"), ("SingletonCookie", "1")):
        (profile / name).symlink_to(dest)


def test_foreign_host_lock_is_removed(tmp_path: Path) -> None:
    _lock(tmp_path, "docker-2593")
    main._clear_stale_profile_lock(tmp_path)
    assert not any(p.name.startswith("Singleton") for p in tmp_path.iterdir())


def test_dead_local_pid_lock_is_removed(tmp_path: Path) -> None:
    import socket

    _lock(tmp_path, f"{socket.gethostname()}-999999999")
    main._clear_stale_profile_lock(tmp_path)
    assert not (tmp_path / "SingletonLock").is_symlink()


def test_live_local_lock_is_kept(tmp_path: Path) -> None:
    import os
    import socket

    _lock(tmp_path, f"{socket.gethostname()}-{os.getpid()}")
    main._clear_stale_profile_lock(tmp_path)
    assert (tmp_path / "SingletonLock").is_symlink()


def test_no_lock_is_noop(tmp_path: Path) -> None:
    main._clear_stale_profile_lock(tmp_path)
