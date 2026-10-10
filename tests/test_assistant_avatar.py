from __future__ import annotations
import base64, struct, zlib
import json
from pathlib import Path
import pytest
from nerya.agent.assistant_avatar import valid_avatar, PNG_PREFIX, PRESET_ROLES
from nerya.agent.assistants import AssistantError, AssistantStore
from nerya.core.paths import WorkspacePaths

pytestmark = pytest.mark.smoke

def png(width=2, height=2, pixels=None):
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value) & 0xffffffff)
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    raw = pixels if pixels is not None else (b"\x00" + bytes([32, 64, 128, 255]) * width) * height
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")

def uri(data):
    return PNG_PREFIX + base64.b64encode(data).decode()

@pytest.mark.parametrize("value", ["", "N", "🧭", "🐱", uri(png())])
def test_supported_avatar_values(value):
    assert valid_avatar(value)

@pytest.mark.parametrize("value", ["data:image/svg+xml;base64,PHN2Zz4=", "data:image/jpeg;base64,YQ==", PNG_PREFIX + "!!!", uri(b"\x89PNG\r\n\x1a\n"), uri(png()[:-1]), uri(png()+b"extra"), uri(png(257,1)), uri(png(2,2,b"x"*1000000)), PNG_PREFIX + "A"*140000])
def test_invalid_or_unbounded_avatars_rejected(value):
    assert not valid_avatar(value)

@pytest.mark.parametrize("role", sorted(PRESET_ROLES))
def test_existing_portrait_preset_persists(tmp_path, role):
    store = AssistantStore(WorkspacePaths(tmp_path))
    avatar = f"role:{role}"
    saved = store.save({"name": "Portrait test", "avatar": avatar})
    assert AssistantStore(WorkspacePaths(tmp_path)).get(saved["id"])["avatar"] == avatar

@pytest.mark.parametrize("value", ["role:unknown", "role:../lead", "role:Lead", "role:", "role:lead.png", "role:https://x"])
def test_invalid_portrait_identifier_rejected(value):
    assert not valid_avatar(value)

def test_preset_contract_matches_existing_frontend_asset_library():
    dashboard = Path(__file__).resolve().parents[1] / "dashboard"
    manifest = json.loads((dashboard / "public/branding/agents/manifest.json").read_text())
    assert PRESET_ROLES == {item["role"] for item in manifest["files"]}
    contract = (dashboard / "lib/roleAvatars.ts").read_text().split("] as const);")[0]
    for role in PRESET_ROLES:
        assert f'"{role}"' in contract
        assert (dashboard / "public/branding/agents" / f"{role}.png").is_file()


def test_avatar_save_roundtrip_preserves_hidden_profile_configuration(tmp_path):
    store = AssistantStore(WorkspacePaths(tmp_path))
    before = store.save({"name":"Avatar test", "role":"Research", "instructions":"Be concise", "model_provider":"fixture", "model_id":"custom", "memory_allowed":False, "shell_allowed":False, "tool_policy":{"deny":["run_shell"]}})
    avatar = uri(png())
    after = store.save({"name":"New name", "avatar":avatar, "instructions":"New preference"}, aid=before["id"], expected_revision=before["revision"])
    for key in ["role","enabled","model_provider","model_id","model_tier","tool_policy","research_allowed","memory_allowed","browser_allowed","files_allowed","shell_allowed"]:
        assert after[key] == before[key]
    assert AssistantStore(WorkspacePaths(tmp_path)).get(before["id"])["avatar"] == avatar
    with pytest.raises(AssistantError, match="invalid_assistant_avatar"):
        store.save({"avatar":PNG_PREFIX+"broken"}, aid=before["id"], expected_revision=after["revision"])
    assert store.get(before["id"])["revision"] == after["revision"]
