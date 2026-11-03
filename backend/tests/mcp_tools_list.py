"""
Standalone script — lists Gmail MCP tools via Smithery Connect.

Smithery Connect flow (Streamable HTTP, not deprecated SSE):
  1. PUT  /namespaces/{name}                      — create namespace (idempotent)
  2. PUT  /connect/{namespace}/{conn_id}          — create / upsert connection
  3. Check status:
       "auth_required"  → print setup URL and exit (OAuth needed)
       "input_required" → print missing fields and exit
       "connected"      → proceed
  4. POST /connect/{namespace}/{conn_id}/mcp      — list tools via Streamable HTTP

Run:
    uv run python tests/mcp_tools_list.py
"""

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

SMITHERY_API_KEY: str = os.environ["SMITHERY_API_KEY"]

SMITHERY_BASE = "https://api.smithery.ai"
GMAIL_MCP_URL = "https://server.smithery.ai/gmail"

NAMESPACE = "chief-of-staff"
CONNECTION_ID = "gmail-main"

AUTH_HEADERS = {
    "Authorization": f"Bearer {SMITHERY_API_KEY}",
    "Content-Type": "application/json",
}


def format_tools_for_openrouter(tools: list) -> list[dict]:
    """Convert MCP tools to OpenAI-compatible tool format for OpenRouter."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.inputSchema,
            },
        }
        for tool in tools
    ]


async def call_tool(session: ClientSession, tool_name: str, arguments: dict):
    result = await session.call_tool(tool_name, arguments)
    return result.content


def ensure_namespace() -> None:
    """Create the Smithery namespace if it does not already exist."""
    url = f"{SMITHERY_BASE}/namespaces/{NAMESPACE}"
    response = httpx.put(url, headers=AUTH_HEADERS, timeout=15)
    if response.status_code not in (200, 201, 409):
        response.raise_for_status()
    print(f"Namespace '{NAMESPACE}': ok (status {response.status_code})")


def upsert_connection() -> dict:
    """Create or update the Gmail connection via Smithery Connect REST API."""
    url = f"{SMITHERY_BASE}/connect/{NAMESPACE}/{CONNECTION_ID}"
    response = httpx.put(url, json={"mcpUrl": GMAIL_MCP_URL}, headers=AUTH_HEADERS, timeout=15)
    response.raise_for_status()
    return response.json()


async def list_tools_via_mcp() -> list:
    """Open a Streamable-HTTP MCP session and return all available tools."""
    mcp_endpoint = f"{SMITHERY_BASE}/connect/{NAMESPACE}/{CONNECTION_ID}/mcp"

    async with streamablehttp_client(mcp_endpoint, headers=AUTH_HEADERS) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools_result = await session.list_tools()
            tools = tools_result.tools

            print(f"\nFound {len(tools)} tools:\n")
            for tool in tools:
                print(f"  - {tool.name}: {tool.description}")

            openrouter_tools = format_tools_for_openrouter(tools)
            print(f"\nFormatted for OpenRouter ({len(openrouter_tools)} tools):\n")
            for t in openrouter_tools:
                print(f"  - {t['function']['name']}")

            return tools


async def main() -> None:
    print(f"1. Ensuring namespace '{NAMESPACE}' exists…")
    ensure_namespace()

    print(f"\n2. Creating / fetching connection '{CONNECTION_ID}' for {GMAIL_MCP_URL}…")
    connection = upsert_connection()

    status = connection.get("status", {})
    state = status.get("state", "unknown")
    print(f"   Connection state: {state}")

    if state == "auth_required":
        setup_url = status.get("setupUrl", "")
        print("\nGmail requires OAuth authorisation.")
        print(f"Visit this URL to connect your Gmail account:\n\n  {setup_url}\n")
        print("Re-run this script after completing authorisation.")
        sys.exit(0)

    if state == "input_required":
        missing = status.get("missing", {})
        print(f"\nServer needs more configuration. Missing fields: {missing}")
        setup_url = status.get("setupUrl", "")
        if setup_url:
            print(f"Configure at: {setup_url}")
        sys.exit(1)

    if state == "error":
        print(f"\nConnection error: {status.get('message', 'unknown')}")
        sys.exit(1)

    if state != "connected":
        print(f"\nUnexpected connection state: {state}")
        print(connection)
        sys.exit(1)

    print("\n3. Listing tools via MCP Streamable HTTP…")
    await list_tools_via_mcp()


if __name__ == "__main__":
    asyncio.run(main())
