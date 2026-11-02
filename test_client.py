"""端到端测试: 连接本机 MCP server, 调用真实 NotebookLM 工具。"""
import asyncio, os
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

KEY = None
for line in open(".env"):
    if line.startswith("MCP_API_KEY="):
        KEY = line.strip().split("=", 1)[1]

transport = StreamableHttpTransport(
    url="http://127.0.0.1:8422/mcp",
    headers={"Authorization": "Bearer %s" % KEY})

async def main():
    async with Client(transport) as c:
        tools = await c.list_tools()
        print("tools(%d):" % len(tools), sorted(t.name for t in tools))
        r = await c.call_tool("auth_status", {})
        print("auth_status:", r.data)
        r = await c.call_tool("list_notebooks", {})
        d = r.data
        print("list_notebooks: count =", d["count"])
        for n in d["notebooks"][:3]:
            print("  -", n.get("id", "")[:12], n.get("title"))
        first = d["notebooks"][0]
        r = await c.call_tool("set_active_notebook",
                              {"notebook_id": first["id"]})
        print("set_active:", r.data.get("title"))
        r = await c.call_tool("list_sources", {})
        print("list_sources: count =", r.data["count"])
        r = await c.call_tool("ask",
                              {"question": "这个笔记本里有哪些来源？一句话概括。"})
        ans = r.data.get("answer", r.data)
        print("ask:", str(ans)[:150])

asyncio.run(main())
