from agents.mcp import MCPServerStdio


async def email_mcp(credentials_path: str) -> MCPServerStdio:
    gmail_params = {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-google-workspace"],
        "env": {
            "GOOGLE_CREDENTIALS": credentials_path,
        },
    }
    server = MCPServerStdio(params=gmail_params)
    await server.__aenter__()
    gmail_tools = await server.list_tools()
    return gmail_tools, await server.__aexit__(None, None, None)
