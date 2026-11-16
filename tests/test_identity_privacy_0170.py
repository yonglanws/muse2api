"""0.17.0: identity vibe-line controls and the product-improvements
privacy passthrough, built on probes 21-27.

Facts encoded here (recon 2026-11-17):
- the injector re-reads IDENTITY.md EVERY turn (probe 23b: strip the vibe
  line mid-thread and the tone reverts in the SAME thread);
- the vibe line only injects when its value matches one of the three
  upstream ids (probe 25: free-form values are dropped);
- upstream has no vibe delete — stripping the line is the only off switch;
- the name is signup-only (probes 22/26) and the model actively guards its
  identity against non-DB-recorded file claims (probe 24).
"""
from __future__ import annotations

from muse2api.accounts.model import Account
from muse2api.config import Settings
from muse2api.drivers.protocol.driver import ProtocolDriver
from muse2api.errors import UpstreamError
from test_prompt_stack_0160 import _parity_driver, _ParityClient

VIBE_FILE = (
    "# IDENTITY.md\n\n"
    "<!-- muse2api-managed:begin:v2 -->\n"
    "managed rules\n"
    "<!-- muse2api-managed:end:v2 -->\n\n"
    "- **Vibe:** dry_witty\n"
)


class _IdentityClient(_ParityClient):
    """FakeClient with an in-memory IDENTITY.md and training settings."""

    def __init__(self, identity_file: str | None = VIBE_FILE) -> None:
        super().__init__()
        self.files = {"IDENTITY.md": identity_file} if identity_file else {}
        self.identity_payload = {"name": "Muse", "name_set": False,
                                 "path": "IDENTITY.md"}
        self.training = {"training_enabled": True,
                         "training_objection": {"status": "not_filed",
                                                "objected_at_ms": None}}
        self.training_calls: list[bool] = []

    async def read_text_file(self, path: str) -> str | None:
        return self.files.get(path)

    async def write_text_file(self, path: str, text: str) -> None:
        self.files[path] = text

    async def identity_get(self):
        return dict(self.identity_payload)

    async def identity_set(self, *, name=None, vibe=None):
        if vibe:
            self.identity_payload["vibe"] = vibe
            lines = self.files.get("IDENTITY.md", "").rstrip("\n").splitlines()
            lines = [ln for ln in lines
                     if not ln.strip().startswith("- **Vibe:**")]
            lines.append(f"- **Vibe:** {vibe}")
            self.files["IDENTITY.md"] = "\n".join(lines) + "\n"
        return {"vibe": vibe}

    async def product_improvements_get(self):
        return dict(self.training)

    async def product_improvements_set(self, *, training_enabled: bool):
        self.training_calls.append(training_enabled)
        self.training = {**self.training, "training_enabled": training_enabled}
        return {"ok": True}


# ---------------------------------------------------------------- vibe line
async def test_identity_get_reports_vibe_line_diagnostics():
    driver = _parity_driver(Settings(), _IdentityClient(), None)
    got = await driver.identity_get(Account(id="acc"))
    assert got["gateway"] == {"vibe_line": "dry_witty", "vibe_line_valid_id": True}


async def test_identity_get_flags_freeform_vibe_line():
    client = _IdentityClient(VIBE_FILE.replace("dry_witty", "grumpy pirate"))
    driver = _parity_driver(Settings(), client, None)
    got = await driver.identity_get(Account(id="acc"))
    # probe 25: the injector drops values outside the three ids
    assert got["gateway"] == {"vibe_line": "grumpy pirate",
                              "vibe_line_valid_id": False}


async def test_identity_get_without_file_reports_no_line():
    driver = _parity_driver(Settings(), _IdentityClient(identity_file=None),
                            None)
    got = await driver.identity_get(Account(id="acc"))
    assert got["gateway"] == {"vibe_line": None, "vibe_line_valid_id": False}


async def test_identity_vibe_none_strips_line():
    client = _IdentityClient()
    driver = _parity_driver(Settings(), client, None)
    result = await driver.identity_set(Account(id="acc"), vibe="none")
    assert result == {"vibe": None}
    text = client.files["IDENTITY.md"]
    assert "**Vibe:**" not in text
    assert "managed rules" in text  # harness section untouched
    assert "\n\n\n" not in text  # blank runs collapsed


async def test_identity_vibe_none_is_noop_without_line():
    client = _IdentityClient(VIBE_FILE.replace("- **Vibe:** dry_witty\n", ""))
    driver = _parity_driver(Settings(), client, None)
    result = await driver.identity_set(Account(id="acc"), vibe="none")
    assert result == {"vibe": None}
    # no write happened: the file text is unchanged
    assert client.files["IDENTITY.md"] == VIBE_FILE.replace(
        "- **Vibe:** dry_witty\n", "")


async def test_identity_vibe_set_still_uses_upstream():
    client = _IdentityClient()
    driver = _parity_driver(Settings(), client, None)
    result = await driver.identity_set(Account(id="acc"), vibe="warm_friendly")
    assert result == {"vibe": "warm_friendly"}
    assert client.files["IDENTITY.md"].endswith("- **Vibe:** warm_friendly\n")


async def test_identity_diagnostics_survives_fs_errors():
    class _BrokenFileClient(_IdentityClient):
        async def read_text_file(self, path: str) -> str | None:
            raise UpstreamError("fs read failed")

    driver = _parity_driver(Settings(), _BrokenFileClient(), None)
    got = await driver.identity_get(Account(id="acc"))
    assert got["gateway"] == {}


# ------------------------------------------------------- product improvements
async def test_product_improvements_passthrough():
    client = _IdentityClient()
    driver = _parity_driver(Settings(), client, None)
    account = Account(id="acc")
    got = await driver.product_improvements_get(account)
    assert got["training_enabled"] is True
    await driver.product_improvements_set(account, training_enabled=False)
    assert client.training_calls == [False]
    got = await driver.product_improvements_get(account)
    assert got["training_enabled"] is False


def test_vibe_line_regex_shapes():
    driver = ProtocolDriver(Settings())
    matches = driver.VIBE_LINE_RE.findall(
        "- **Vibe:** dry_witty\nx\n  - **Vibe:**   short_concise  \n")
    assert matches == ["dry_witty", "short_concise"]
    assert driver.VIBE_LINE_RE.findall("- **VibeX:** no") == []
