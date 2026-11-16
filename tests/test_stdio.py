"""A real MCP client talks to a real server process over stdio."""

import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def server(config_path: str, cwd: str) -> StdioServerParameters:
    env = {**os.environ, "SEO_MCP_CONFIG": config_path}
    return StdioServerParameters(
        command=sys.executable, args=["-m", "seo_mcp.server"], env=env, cwd=cwd
    )


def payload(result) -> dict:
    return json.loads(result.content[0].text)


READ_TOOLS = ["get_seo_data", "list_sites", "query_api"]
WRITE_TOOLS = ["submit_urls"]


async def test_four_tools_and_an_honest_result(write_config, repo, tmp_path):
    path = write_config()
    with open(tmp_path / "stderr.log", "w") as errlog:
        async with stdio_client(server(str(path), str(repo)), errlog=errlog) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                sites = payload(await session.call_tool("list_sites", {}))
                data = await session.call_tool("get_seo_data", {"sources": ["bing_links"]})
                data = payload(data)
                urls = {"urls": ["https://www.example.com/jobs/1"]}
                submit = payload(await session.call_tool("submit_urls", urls))
    assert sorted(tool.name for tool in tools) == sorted(READ_TOOLS + WRITE_TOOLS)
    kinds = {tool.name: tool.annotations for tool in tools}
    assert all(kinds[name].read_only_hint for name in READ_TOOLS)
    assert all(kinds[name].read_only_hint is False for name in WRITE_TOOLS)
    assert all(kinds[name].destructive_hint is False for name in WRITE_TOOLS)
    assert sites["rows"][0]["host"] == "www.example.com"
    assert data["site"] == "www.example.com"
    assert data["status"] == "not_connected"
    # The example site has no opt-in, so the write refuses and sends nothing.
    assert submit["status"] == "refused"
    assert "submit_urls" in submit["reason"]
