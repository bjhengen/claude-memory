#!/usr/bin/env python3
"""
MCP HTTP Bridge for Claude Desktop.

Bridges stdio (Claude Desktop) to HTTP MCP server (claude-memory).
Uses mcp-proxy package for proper protocol handling.

Usage in claude_desktop_config.json:
{
  "mcpServers": {
    "claude-memory": {
      "command": "python3",
      "args": ["/Users/bhengen/dev/claude-memory/scripts/mcp-bridge.py"]
    }
  }
}
"""

import asyncio
import sys
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.server import Server
from mcp.server.stdio import stdio_server

MCP_URL = "https://memory.friendly-robots.com/mcp"


async def main():
    # Connect to remote HTTP MCP server
    async with sse_client(MCP_URL) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as client:
            await client.initialize()

            # Create local stdio server that proxies to remote
            server = Server("claude-memory-bridge")

            # Proxy all tool calls to remote
            @server.list_tools()
            async def list_tools():
                result = await client.list_tools()
                return result.tools

            @server.call_tool()
            async def call_tool(name: str, arguments: dict):
                result = await client.call_tool(name, arguments)
                return result.content

            # Run stdio server
            async with stdio_server() as (read, write):
                await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
