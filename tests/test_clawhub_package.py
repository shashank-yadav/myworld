import json
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "packaging" / "clawhub" / "openclaw-myworld"


def test_clawhub_package_metadata_points_at_pinned_myworld_world():
    package = json.loads((PACKAGE_ROOT / "package.json").read_text())
    manifest = json.loads((PACKAGE_ROOT / "openclaw.plugin.json").read_text())

    assert package["name"] == "@shashank-yadav/openclaw-myworld"
    assert package["version"] == "0.2.1"
    assert package["openclaw"]["compat"]["pluginApi"]
    assert package["openclaw"]["build"]["openclawVersion"]

    server = manifest["mcpServers"]["myworld"]
    assert server["transport"] == "stdio"
    assert server["command"] == "uvx"
    assert server["args"] == ["myworld==0.2.1", "world", "invoice-review"]
    assert "world_task" in server["toolFilter"]["include"]
    assert "gmail__search_emails" in server["toolFilter"]["include"]
    assert "drive__modify_sheet_values" in server["toolFilter"]["include"]
