"""Starts the endstar-unity MCP server exactly as .mcp.json does and calls its read-only tools.

    uv run --no-project --with "mcp>=1.10,<2" python tests/test_mcp.py
"""
import asyncio
import json
import os
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent


async def main():
    server = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]["endstar-unity"]
    # Like Claude Code: started in this folder, with .mcp.json's "env" added to the environment.
    params = StdioServerParameters(command=server["command"], args=server["args"], cwd=str(ROOT),
                                   env={**os.environ, **(server.get("env") or {})})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            print("TOOLS", ", ".join(t.name for t in tools))
            for name, args in [("unity_status", {}), ("unity_export_state", {}),
                               ("unity_editor_log", {"lines": 3, "contains": "Prop Asset"})]:
                result = await session.call_tool(name, args)
                text = result.content[0].text if result.content else "(no content)"
                print(f"--- {name} {'ERROR' if result.isError else 'ok'}\n{text}")


asyncio.run(main())
