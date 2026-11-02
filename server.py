#!/usr/bin/env python3
"""NotebookLM HTTP MCP Server (FastMCP)

将 notebooklm-py 封装为带 Bearer 鉴权的 streamable HTTP MCP 服务。
鉴权 key 从 .env 读取 (MCP_API_KEY)，客户端请求头需带:
    Authorization: Bearer <MCP_API_KEY>

.env 配置项:
    MCP_API_KEY=<必填, 鉴权 key>
    MCP_HOST=127.0.0.1        # 监听地址 (配反向代理时保持 127.0.0.1)
    MCP_PORT=8422
    MCP_DOWNLOAD_DIR=~/.notebooklm/downloads   # artifact 下载目录
    NOTEBOOKLM_PROFILE=default                 # notebooklm 账号 profile
    NOTEBOOKLM_HOME=~/.notebooklm              # notebooklm 数据目录

启动:  python server.py     (或 uv run server.py)
"""

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Literal, Optional

from fastmcp import FastMCP, Context
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

# ---------------------------------------------------------------- .env

def load_dotenv(path: str = None):
    p = Path(path or Path(__file__).parent / ".env")
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip("'\"")
        os.environ.setdefault(k, v)   # 不覆盖已有环境变量


load_dotenv()

API_KEY = os.environ.get("MCP_API_KEY", "").strip()
HOST = os.environ.get("MCP_HOST", "127.0.0.1").strip()
PORT = int(os.environ.get("MCP_PORT", "8422"))
PATH_PREFIX = os.environ.get("MCP_PATH_PREFIX", "/mcp").strip() or "/mcp"
DOWNLOAD_DIR = Path(os.environ.get("MCP_DOWNLOAD_DIR",
                                   "~/.notebooklm/downloads")).expanduser()
PROFILE = os.environ.get("NOTEBOOKLM_PROFILE", "default")

if not API_KEY:
    sys.exit("错误: 请在 .env 中设置 MCP_API_KEY (参考 .env.example)")

os.environ.setdefault("NOTEBOOKLM_PROFILE", PROFILE)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- 鉴权

auth = StaticTokenVerifier(
    tokens={API_KEY: {"client_id": "notebooklm-mcp",
                      "scopes": ["notebooklm"]}},
    required_scopes=["notebooklm"],
)

mcp = FastMCP(name="notebooklm", auth=auth,
              instructions="NotebookLM 操作: 笔记本/来源管理、问答、"
                           "生成与下载播客/测验/思维导图等 artifact。"
                           "多数工具的 notebook_id 可省略(使用 set_active_notebook"
                           " 设定的当前笔记本)。")

# ---------------------------------------------------------------- NotebookLM 客户端 (全局单例, 检测 cookie 文件变化自动重建)

_client = None
_client_mtime = None
_client_lock = asyncio.Lock()
_active_notebook_id: Optional[str] = None


def _storage_state_path() -> Path:
    home = Path(os.environ.get(
        "NOTEBOOKLM_HOME", "~/.notebooklm")).expanduser()
    return home / "profiles" / \
        os.environ.get("NOTEBOOKLM_PROFILE", "default") / \
        "storage_state.json"


async def get_client():
    global _client, _client_mtime
    p = _storage_state_path()
    mtime = p.stat().st_mtime if p.exists() else 0.0
    if _client is not None and _client_mtime != mtime:
        # cookie 文件被 update_ui.py 更新过: 丢弃旧 client, 下次重建
        try:
            await _client.__aexit__(None, None, None)
        except Exception:
            pass
        _client = None
    if _client is None:
        async with _client_lock:
            if _client is None:
                from notebooklm import NotebookLMClient
                c = await NotebookLMClient.from_storage()
                _client = await c.__aenter__()   # 进入生命周期上下文(常驻)
                _client_mtime = _storage_state_path().stat().st_mtime \
                    if _storage_state_path().exists() else 0.0
    return _client


def obj_to_dict(o: Any) -> Any:
    """pydantic / dataclass / 普通对象 -> 可 JSON 化的 dict。"""
    if o is None or isinstance(o, (str, int, float, bool)):
        return o
    if isinstance(o, (list, tuple)):
        return [obj_to_dict(x) for x in o]
    if isinstance(o, dict):
        return {k: obj_to_dict(v) for k, v in o.items()}
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    if hasattr(o, "_asdict"):
        return obj_to_dict(o._asdict())
    import dataclasses
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return obj_to_dict(dataclasses.asdict(o))
    if hasattr(o, "__dict__"):
        return {k: obj_to_dict(v) for k, v in vars(o).items()
                if not k.startswith("_")}
    return str(o)


def nb(notebook_id: Optional[str]) -> str:
    """解析目标笔记本: 显式 id > 当前激活笔记本。"""
    nid = notebook_id or _active_notebook_id
    if not nid:
        raise ValueError("未指定 notebook_id, 且没有激活的笔记本; "
                         "请先调用 set_active_notebook 或传 notebook_id")
    return nid


async def safe(fn):
    """统一异常包装: 返回结构化错误而不让 server 崩溃。"""
    try:
        return await fn()
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}


def pick(d: dict, *keys):
    return {k: d[k] for k in keys if d.get(k) is not None}


# ---------------------------------------------------------------- 工具: 认证与笔记本

@mcp.tool
async def auth_status() -> dict:
    """检查 NotebookLM 认证状态与当前激活笔记本。"""
    async def _run():
        c = await get_client()
        email = c.get_account_email()
        if hasattr(email, "__await__"):
            email = await email
        return {"connected": bool(c.is_connected),
                "email": email,
                "active_notebook_id": _active_notebook_id,
                "profile": PROFILE}
    return await safe(_run)


@mcp.tool
async def list_notebooks() -> dict:
    """列出所有笔记本 (id/标题/创建时间/源数量)。"""
    async def _run():
        c = await get_client()
        nbs = await c.notebooks.list()
        return {"count": len(nbs),
                "notebooks": [obj_to_dict(n) for n in nbs]}
    return await safe(_run)


@mcp.tool
async def create_notebook(title: str) -> dict:
    """创建新笔记本并设为激活。"""
    async def _run():
        global _active_notebook_id
        c = await get_client()
        nbobj = await c.notebooks.create(title)
        _active_notebook_id = nbobj.id
        return obj_to_dict(nbobj)
    return await safe(_run)


@mcp.tool
async def set_active_notebook(notebook_id: str) -> dict:
    """设定当前激活笔记本 (后续工具可省略 notebook_id)。返回其信息。"""
    async def _run():
        global _active_notebook_id
        c = await get_client()
        nbobj = await c.notebooks.get(notebook_id)
        _active_notebook_id = nbobj.id
        return obj_to_dict(nbobj)
    return await safe(_run)


@mcp.tool
async def rename_notebook(notebook_id: str, new_title: str) -> dict:
    """重命名笔记本。"""
    async def _run():
        c = await get_client()
        return obj_to_dict(await c.notebooks.rename(notebook_id, new_title))
    return await safe(_run)


@mcp.tool
async def delete_notebook(notebook_id: str, confirm: bool = False) -> dict:
    """删除笔记本 (危险操作, 必须传 confirm=true)。"""
    async def _run():
        if not confirm:
            return {"error": "危险操作: 请确认后传 confirm=true 再执行"}
        c = await get_client()
        await c.notebooks.delete(notebook_id)
        global _active_notebook_id
        if _active_notebook_id == notebook_id:
            _active_notebook_id = None
        return {"deleted": notebook_id}
    return await safe(_run)


# ---------------------------------------------------------------- 工具: 来源

@mcp.tool
async def list_sources(notebook_id: Optional[str] = None) -> dict:
    """列出笔记本的来源 (网页/文件/文本等)。"""
    async def _run():
        c = await get_client()
        srcs = await c.sources.list(nb(notebook_id))
        return {"count": len(srcs),
                "sources": [obj_to_dict(s) for s in srcs]}
    return await safe(_run)


@mcp.tool
async def add_source_url(url: str, title: Optional[str] = None,
                         wait: bool = True,
                         notebook_id: Optional[str] = None) -> dict:
    """添加网页 URL 为来源 (默认等待索引完成)。"""
    async def _run():
        c = await get_client()
        s = await c.sources.add_url(nb(notebook_id), url, wait=wait,
                                    title=title)
        return obj_to_dict(s)
    return await safe(_run)


@mcp.tool
async def add_source_text(title: str, content: str,
                          notebook_id: Optional[str] = None) -> dict:
    """添加一段文本为来源。"""
    async def _run():
        c = await get_client()
        s = await c.sources.add_text(nb(notebook_id), title, content,
                                     wait=True)
        return obj_to_dict(s)
    return await safe(_run)


@mcp.tool
async def add_source_file(file_path: str, title: Optional[str] = None,
                          notebook_id: Optional[str] = None) -> dict:
    """添加服务器本地文件为来源 (PDF/Markdown/Word/EPUB/音视频等)。"""
    async def _run():
        p = Path(file_path).expanduser()
        if not p.exists():
            return {"error": "文件不存在: %s" % p}
        c = await get_client()
        s = await c.sources.add_file(nb(notebook_id), str(p), wait=True,
                                     title=title)
        return obj_to_dict(s)
    return await safe(_run)


@mcp.tool
async def get_source_fulltext(source_id: str, output_path: Optional[str] = None,
                              notebook_id: Optional[str] = None) -> dict:
    """获取来源的索引全文; output_path 省略时直接返回文本 (长文建议落盘)。"""
    async def _run():
        c = await get_client()
        ft = await c.sources.get_fulltext(nb(notebook_id), source_id)
        d = obj_to_dict(ft)
        text = d.get("text") or d.get("content") or ""
        if output_path:
            out = Path(output_path).expanduser()
            if not out.is_absolute():
                out = DOWNLOAD_DIR / out
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            return {"saved_to": str(out), "chars": len(text)}
        return {"chars": len(text), "text": text[:20000]}
    return await safe(_run)


@mcp.tool
async def delete_source(source_id: str, notebook_id: Optional[str] = None) -> dict:
    """删除笔记本中的一个来源。"""
    async def _run():
        c = await get_client()
        await c.sources.delete(nb(notebook_id), source_id)
        return {"deleted": source_id}
    return await safe(_run)


# ---------------------------------------------------------------- 工具: 问答

@mcp.tool
async def ask(question: str, notebook_id: Optional[str] = None) -> dict:
    """基于笔记本来源向 NotebookLM 提问, 返回答案与引用。"""
    async def _run():
        c = await get_client()
        r = await c.chat.ask(nb(notebook_id), question)
        d = obj_to_dict(r)
        return pick(d, "answer", "citations", "conversation_id",
                    "suggested_actions")
    return await safe(_run)


# ---------------------------------------------------------------- 工具: 生成与下载 artifact

ArtifactKind = Literal["audio", "video", "quiz", "flashcards", "mind_map",
                       "report", "data_table", "slide_deck", "infographic"]

_GEN_METHOD = {"audio": "generate_audio", "video": "generate_video",
               "quiz": "generate_quiz", "flashcards": "generate_flashcards",
               "mind_map": "generate_mind_map", "report": "generate_report",
               "data_table": "generate_data_table",
               "slide_deck": "generate_slide_deck",
               "infographic": "generate_infographic"}
_DL_METHOD = {"audio": "download_audio", "video": "download_video",
              "quiz": "download_quiz", "flashcards": "download_flashcards",
              "mind_map": "download_mind_map", "report": "download_report",
              "data_table": "download_data_table",
              "slide_deck": "download_slide_deck",
              "infographic": "download_infographic"}
_DL_EXT = {"audio": ".mp3", "video": ".mp4", "quiz": ".json",
           "flashcards": ".json", "mind_map": ".json", "report": ".md",
           "data_table": ".csv", "slide_deck": ".pdf",
           "infographic": ".png"}


@mcp.tool
async def generate_artifact(
    kind: ArtifactKind,
    instructions: Optional[str] = None,
    language: Optional[str] = None,
    source_ids: Optional[list[str]] = None,
    quiz_difficulty: Optional[Literal["easy", "medium", "hard"]] = None,
    quiz_quantity: Optional[Literal["fewer", "default", "more"]] = None,
    report_format: Optional[Literal["briefing_doc", "study_guide",
                                    "blog_post", "custom"]] = None,
    wait: bool = True,
    timeout: float = 600.0,
    notebook_id: Optional[str] = None,
) -> dict:
    """生成 artifact (播客/视频/测验/闪卡/思维导图/报告/数据表/幻灯片/信息图)。

    wait=true 时阻塞到完成并返回 task 状态; mind_map 直接返回结果。
    language 传 BCP-47 如 'zh'。生成后用 download_artifact 下载。
    """
    async def _run():
        c = await get_client()
        method = getattr(c.artifacts, _GEN_METHOD[kind])
        kwargs = {}
        for k, v in [("instructions", instructions), ("language", language),
                     ("source_ids", source_ids)]:
            if v:
                kwargs[k] = v
        if kind == "quiz" or kind == "flashcards":
            if quiz_difficulty:
                kwargs["difficulty"] = quiz_difficulty
            if quiz_quantity:
                kwargs["quantity"] = quiz_quantity
        if kind == "report" and report_format:
            kwargs["report_format"] = report_format
        r = await method(nb(notebook_id), **kwargs)
        d = obj_to_dict(r)
        if kind == "mind_map":
            return d
        task_id = d.get("task_id")
        if wait and task_id:
            st = await c.artifacts.wait_for_completion(
                nb(notebook_id), task_id, timeout=timeout)
            d = obj_to_dict(st)
        return d
    return await safe(_run)


@mcp.tool
async def list_artifacts(kind: Optional[ArtifactKind] = None,
                         notebook_id: Optional[str] = None) -> dict:
    """列出笔记本已生成的 artifact。"""
    async def _run():
        c = await get_client()
        arts = await c.artifacts.list(nb(notebook_id))
        items = [obj_to_dict(a) for a in arts]
        if kind:
            items = [a for a in items
                     if kind in (a.get("type") or a.get("artifact_type")
                                 or "").lower().replace("-", "_")
                     or kind in str(a.get("kind", "")).lower()]
        return {"count": len(items), "artifacts": items}
    return await safe(_run)


@mcp.tool
async def download_artifact(kind: ArtifactKind,
                            artifact_id: Optional[str] = None,
                            output_path: Optional[str] = None,
                            notebook_id: Optional[str] = None) -> dict:
    """下载已生成的 artifact 到服务器下载目录, 返回保存路径。

    output_path 为相对 MCP_DOWNLOAD_DIR 的文件名 (缺省自动命名)。
    """
    async def _run():
        c = await get_client()
        out = Path(output_path).expanduser() if output_path else \
            Path("%s-%s%s" % (kind, artifact_id or "latest", _DL_EXT[kind]))
        if not out.is_absolute():
            out = DOWNLOAD_DIR / out
        out.parent.mkdir(parents=True, exist_ok=True)
        method = getattr(c.artifacts, _DL_METHOD[kind])
        saved = await method(nb(notebook_id), str(out), artifact_id)
        return {"saved_to": saved, "kind": kind}
    return await safe(_run)


@mcp.tool
async def wait_artifact(task_id: str, timeout: float = 600.0,
                        notebook_id: Optional[str] = None) -> dict:
    """等待一个生成任务完成 (配合 generate_artifact 的 wait=false)。"""
    async def _run():
        c = await get_client()
        st = await c.artifacts.wait_for_completion(nb(notebook_id), task_id,
                                                   timeout=timeout)
        return obj_to_dict(st)
    return await safe(_run)


# ---------------------------------------------------------------- 入口

if __name__ == "__main__":
    print("NotebookLM MCP server -> http://%s:%d%s  (Bearer 鉴权)" %
          (HOST, PORT, PATH_PREFIX))
    print("下载目录: %s | profile: %s" % (DOWNLOAD_DIR, PROFILE))
    mcp.run(transport="http", host=HOST, port=PORT, path=PATH_PREFIX)
