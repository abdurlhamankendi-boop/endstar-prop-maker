"""Copies the Unity bridge (unity/Editor/*.cs) into the Endstar SDK project and tells it where this folder is.
Run once after setting up (Unity loads the scripts the next time it starts):

    uv run --no-project python tools/install_bridge.py

The SDK project comes from ENDSTAR_SDK_PROJECT: the environment, else the "env" of .mcp.json in this folder
(the same setting the MCP server uses), else this PC's default.
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
try:
    settings = json.loads((HERE.parent / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["endstar-unity"]
    for key, value in (settings.get("env") or {}).items():
        os.environ.setdefault(key, value)
except (OSError, ValueError, KeyError):
    pass   # no .mcp.json settings: the defaults in endstar_unity_mcp.py

sys.path.insert(0, str(HERE))
import endstar_unity_mcp as bridge  # noqa: E402

if __name__ == "__main__":
    if not (bridge.SDK_PROJECT / "Assets").is_dir():
        print(f"No Unity project at {bridge.SDK_PROJECT}: set ENDSTAR_SDK_PROJECT in .mcp.json to your Endstar SDK folder")
        sys.exit(1)
    problems = bridge.install_bridge()
    for p in problems:
        print("PROBLEM", p)
    if not problems:
        print(f"bridge installed in {bridge.SDK_PROJECT / 'Assets' / 'Editor' / 'SendToEndless'}")
    sys.exit(1 if problems else 0)
