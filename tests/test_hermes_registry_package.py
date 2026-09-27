import json
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "packaging" / "hermes-registry" / "mcp" / "myworld"


def test_hermes_registry_manifest_points_at_pinned_myworld_world():
    manifest = json.loads((PACKAGE_ROOT / "manifest.json").read_text())

    assert manifest["schemaVersion"] == "1"
    assert manifest["type"] == "mcp"
    assert manifest["id"] == "shashank-yadav/myworld"
    assert manifest["version"] == "0.2.1"
    assert manifest["transport"] == "stdio"
    assert manifest["command"] == "uvx"
    assert manifest["args"] == ["myworld==0.2.1", "world", "invoice-review"]
    assert manifest["configSchema"]["additionalProperties"] is False
    assert (PACKAGE_ROOT / manifest["icon"]).is_file()
