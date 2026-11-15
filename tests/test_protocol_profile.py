"""Native muse.ai instruction-file profile (IDENTITY/SOUL/USER/AGENTS.md)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from muse2api.config import Settings
from muse2api.drivers.protocol.profile import (
    MANAGED_MARKER,
    PROFILE_FILES,
    apply_native_profile,
    chameleon_files,
    desired_files,
    reset_native_profile,
)


class FakeFsClient:
    """Records fs writes; serves previously written (or seeded) content."""

    alive = True

    def __init__(self, seeds: dict[str, str] | None = None) -> None:
        self.files: dict[str, str] = dict(seeds or {})
        self.writes: list[tuple[str, str]] = []
        self.reads: list[str] = []

    async def read_text_file(self, path: str) -> str | None:
        self.reads.append(path)
        return self.files.get(path)

    async def write_text_file(self, path: str, text: str) -> None:
        self.writes.append((path, text))
        self.files[path] = text


def _apply(client: FakeFsClient, tmp_path: Path, account: str = "acc1"):
    return apply_native_profile(
        client, account_id=account, data_dir=tmp_path / "data")


def test_chameleon_files_cover_all_profile_files() -> None:
    files = chameleon_files()
    assert set(files) == set(PROFILE_FILES)
    for _name, text in files.items():
        assert MANAGED_MARKER in text
        assert "MEMORY.md" not in text or "never written" not in text
    # identity defers to the conversation persona
    assert "角色设定" in files["IDENTITY.md"]
    assert "Never assume the person" in files["USER.md"]


def test_apply_writes_all_files_and_backs_up_originals(tmp_path: Path) -> None:
    seeds = {
        "IDENTITY.md": "original identity",
        "SOUL.md": "original soul",
        "USER.md": "- **Name:** 小红\n",
        "AGENTS.md": "original agents",
    }
    client = FakeFsClient(seeds)
    summary = asyncio.run(_apply(client, tmp_path))
    assert summary == {name: "written" for name in PROFILE_FILES}
    assert {path for path, _ in client.writes} == set(PROFILE_FILES)
    for name, original in seeds.items():
        backup = tmp_path / "data" / "native_profile_backup" / "acc1" / name
        assert backup.is_file()
        assert backup.read_text(encoding="utf-8") == original
    # managed files keep the user's own content and append the section
    soul = client.files["SOUL.md"]
    assert soul.startswith("original soul")
    assert "Conversation personas (gateway)" in soul
    user = client.files["USER.md"]
    assert "**Name:** 小红" in user
    assert "Never assume the person" in user


def test_apply_is_idempotent(tmp_path: Path) -> None:
    client = FakeFsClient()
    asyncio.run(_apply(client, tmp_path))
    writes_after_first = len(client.writes)
    summary = asyncio.run(_apply(client, tmp_path))
    assert summary == {name: "kept" for name in PROFILE_FILES}
    assert len(client.writes) == writes_after_first


def test_apply_preserves_grown_user_content(tmp_path: Path) -> None:
    client = FakeFsClient()
    asyncio.run(_apply(client, tmp_path))
    client.files["USER.md"] += "- **Timezone:** Asia/Shanghai\n"
    summary = asyncio.run(_apply(client, tmp_path))
    assert summary["USER.md"] == "written"
    assert "- **Timezone:** Asia/Shanghai" in client.files["USER.md"]
    assert "Never assume the person" in client.files["USER.md"]


def test_reset_restores_originals_and_requires_backup(tmp_path: Path) -> None:
    client = FakeFsClient({"SOUL.md": "original soul"})
    asyncio.run(_apply(client, tmp_path))
    summary = asyncio.run(reset_native_profile(
        client, account_id="acc1", data_dir=tmp_path / "data"))
    assert summary["SOUL.md"] == "restored"
    assert summary["IDENTITY.md"] == "no-backup"
    assert client.files["SOUL.md"] == "original soul"


def test_desired_files_profile_dir_overrides(tmp_path: Path) -> None:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    (profile_dir / "identity.md").write_text("# IDENTITY.md\n\ncustom\n", encoding="utf-8")
    (profile_dir / "unknown.md").write_text("ignored\n", encoding="utf-8")
    files = desired_files(profile_dir)
    assert files["IDENTITY.md"].endswith("custom\n")
    assert MANAGED_MARKER not in files["IDENTITY.md"]
    assert MANAGED_MARKER in files["SOUL.md"]


def test_driver_applies_profile_on_connect(tmp_path: Path) -> None:
    from muse2api.accounts.model import Account
    from muse2api.drivers.protocol.driver import ProtocolDriver
    from muse2api.drivers.protocol.profile import profile_fingerprint

    settings = Settings(native_profile="chameleon", data_dir=tmp_path / "data")
    driver = ProtocolDriver(settings)
    client = FakeFsClient()
    account = Account(id="acc9", cookies={"session": "x"})

    class _State:
        connect_lock = asyncio.Lock()
        profile_fp = ""

    state = _State()
    asyncio.run(driver._apply_native_profile(account.id, state, client))
    assert {path for path, _ in client.writes} == set(PROFILE_FILES)
    assert state.profile_fp == profile_fingerprint(desired_files(None))
    # unchanged fingerprint: no more writes even after "reconnect"
    writes_after_first = len(client.writes)
    asyncio.run(driver._apply_native_profile(account.id, state, client))
    assert len(client.writes) == writes_after_first


def test_driver_skips_profile_when_off(tmp_path: Path) -> None:
    from muse2api.drivers.protocol.driver import ProtocolDriver

    settings = Settings(native_profile="off", data_dir=tmp_path / "data")
    driver = ProtocolDriver(settings)
    client = FakeFsClient()

    class _State:
        connect_lock = asyncio.Lock()
        profile_fp = ""

    asyncio.run(driver._apply_native_profile("acc1", _State(), client))
    assert client.writes == []


@pytest.mark.parametrize("mode", ["chameleon", "off"])
def test_settings_accepts_native_profile_modes(mode: str) -> None:
    assert Settings(native_profile=mode).native_profile == mode
