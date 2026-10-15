"""Static checks of workflow exports. Not a replacement for importing into n8n."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "n8n" / "workflows"
files = sorted(ROOT.glob("*.json"))
assert len(files) == 2, "Expected exactly two n8n workflow files"
for file in files:
    workflow = json.loads(file.read_text())
    assert workflow["nodes"] and workflow["connections"]
    names = {node["name"] for node in workflow["nodes"]}
    for name, output in workflow["connections"].items():
        assert name in names
        for targets in output["main"]:
            for target in targets:
                assert target["node"] in names
    assert all(not node.get("credentials") for node in workflow["nodes"])
    print("Valid structure:", file.name)
