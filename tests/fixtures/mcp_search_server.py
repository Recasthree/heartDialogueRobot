"""Real stdio MCP fixture; no external network or credentials."""
from mcp.server import MCPServer

server = MCPServer("readonly-stdio-fixture")


@server.tool()
async def search_pubmed(query: str) -> dict:
    return {"status": "no_record", "evidence": [], "fixture": "real_stdio", "query": query}


if __name__ == "__main__":
    server.run("stdio")
