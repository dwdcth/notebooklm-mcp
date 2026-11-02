#!/usr/bin/env python3
"""NotebookLM cookie 更新界面 (纯标准库, 零额外依赖)

用途: Google 会话失效时, 把新的 cookie 粘贴进来一键更新, 无需 SSH。
访问: http://127.0.0.1:8423/?key=<MCP_API_KEY>   (key 同 .env 的 MCP_API_KEY)

支持三种粘贴格式 (自动识别):
  1. storage_state.json 全文          {"cookies": [...], "origins": []}
  2. Cookie-Editor 等扩展导出的数组   [{"name":"SID","value":"..."}, ...]
  3. 浏览器 DevTools 复制的原始 cookie: "SID=xxx; HSID=yyy; ..."
保存前校验必需 cookie (SID + __Secure-1PSIDTS), 自动备份旧文件,
保存后立即联网验证, 并提示 MCP server 已热加载。

安全: 仅监听 127.0.0.1; 远程请走 SSH 隧道 (ssh -L 8423:127.0.0.1:8423 vps)。
"""

import html
import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
CLI = BASE / ".venv" / "bin" / "notebooklm"

PORT = int(os.environ.get("COOKIE_UI_PORT", "8423"))
HOST = os.environ.get("COOKIE_UI_HOST", "127.0.0.1")


def load_env():
    for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


load_env()

# 子路径前缀 (反代场景, 如 /nlm-ui); 必须在 load_env() 之后读取
PREFIX = os.environ.get("UI_PATH_PREFIX", "").strip().rstrip("/")
API_KEY = os.environ.get("MCP_API_KEY", "").strip()
PROFILE = os.environ.get("NOTEBOOKLM_PROFILE", "default")
STORAGE = Path(os.environ.get(
    "NOTEBOOKLM_HOME", "~/.notebooklm")).expanduser() / "profiles" / PROFILE \
    / "storage_state.json"

REQUIRED = ["SID", "__Secure-1PSIDTS"]

# ---------------------------------------------------------------- cookie 解析

def parse_input(text: str) -> dict:
    """三种格式 -> storage_state dict; 识别失败抛 ValueError。"""
    t = text.strip()
    if t.startswith("{"):
        d = json.loads(t)
        if isinstance(d.get("cookies"), list):
            return d
        raise ValueError("JSON 缺少 cookies 字段")
    if t.startswith("["):
        arr = json.loads(t)
        if not isinstance(arr, list) or not arr:
            raise ValueError("数组为空或格式不对")
        return {"cookies": [normalize_cookie(c) for c in arr], "origins": []}
    # 原始 header 格式: k=v; k2=v2
    if "=" in t and "[" not in t[:1]:
        cookies = []
        for part in re.split(r"[;\n]+", t):
            if "=" not in part:
                continue
            name, _, val = part.strip().partition("=")
            name, val = name.strip(), val.strip()
            if name:
                cookies.append({
                    "name": name, "value": val, "domain": ".google.com",
                    "path": "/", "expires": int(time.time()) + 3600 * 24 * 180,
                    "httpOnly": True, "secure": True, "sameSite": "Lax",
                })
        if cookies:
            return {"cookies": cookies, "origins": []}
    raise ValueError("无法识别格式: 请粘贴 storage_state.json 全文、"
                     "Cookie-Editor 导出的 JSON 数组, 或 'a=1; b=2' 形式")


def normalize_cookie(c: dict) -> dict:
    out = {
        "name": c.get("name", ""), "value": c.get("value", ""),
        "domain": c.get("domain") or ".google.com",
        "path": c.get("path") or "/",
        "expires": c.get("expirationDate") or c.get("expires")
        or int(time.time()) + 3600 * 24 * 180,
        "httpOnly": bool(c.get("httpOnly", True)),
        "secure": bool(c.get("secure", True)),
        "sameSite": c.get("sameSite") or "Lax",
    }
    if out["sameSite"] not in ("Strict", "Lax", "None", None):
        out["sameSite"] = "Lax"
    return out


def cookie_names(state: dict):
    return [c.get("name", "") for c in state.get("cookies", [])]


# ---------------------------------------------------------------- 状态与验证

def file_status() -> dict:
    if not STORAGE.exists():
        return {"exists": False, "path": str(STORAGE)}
    try:
        d = json.loads(STORAGE.read_text(encoding="utf-8"))
        names = cookie_names(d)
        return {"exists": True, "path": str(STORAGE),
                "cookies": len(d.get("cookies", [])),
                "has_required": all(r in names for r in REQUIRED),
                "missing": [r for r in REQUIRED if r not in names],
                "updated_at": time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(STORAGE.stat().st_mtime))}
    except Exception as e:
        return {"exists": True, "path": str(STORAGE), "error": str(e)}


def verify_online() -> dict:
    """跑 notebooklm auth check --test --json。"""
    try:
        r = subprocess.run([str(CLI), "auth", "check", "--test", "--json"],
                           capture_output=True, text=True, timeout=90,
                           env={**os.environ, "NOTEBOOKLM_PROFILE": PROFILE})
        try:
            d = json.loads(r.stdout)
            return {"ok": d.get("status") == "ok",
                    "notebook_count": d.get("notebook_count"),
                    "detail": d}
        except json.JSONDecodeError:
            return {"ok": False, "detail": (r.stdout or r.stderr)[:500]}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def save_state(state: dict) -> dict:
    names = cookie_names(state)
    missing = [r for r in REQUIRED if r not in names]
    if missing:
        return {"ok": False,
                "error": "缺少必需 cookie: %s (提取时请确保浏览器已登录 "
                         "notebooklm.google.com)" % ", ".join(missing)}
    STORAGE.parent.mkdir(parents=True, exist_ok=True)
    if STORAGE.exists():
        bak = STORAGE.with_name("storage_state.json.bak-%s"
                                % time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(STORAGE, bak)
    tmp = STORAGE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(STORAGE)
    os.chmod(STORAGE, 0o600)
    return {"ok": True, "backed_up": True, "cookies": len(names)}


# ---------------------------------------------------------------- HTTP

PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NotebookLM Cookie 更新</title>
<style>
 :root{color-scheme:light dark}
 body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;max-width:720px;
      margin:40px auto;padding:0 16px;line-height:1.6}
 h1{font-size:1.3rem}
 .card{border:1px solid #8884;border-radius:10px;padding:14px 18px;margin:14px 0}
 .ok{color:#2e9e5b}.bad{color:#d0453c}.muted{color:#888}
 textarea{width:100%;height:180px;font-family:ui-monospace,monospace;
          font-size:12px;border:1px solid #8886;border-radius:8px;padding:10px;
          box-sizing:border-box}
 button{background:#2563eb;color:#fff;border:0;border-radius:8px;
        padding:10px 22px;font-size:1rem;cursor:pointer;margin-top:8px}
 button.sec{background:#555}
 pre{background:#8881;border-radius:8px;padding:10px;font-size:12px;
     overflow:auto;white-space:pre-wrap}
</style></head><body>
<h1>🔒 NotebookLM Cookie 更新</h1>
<div class="card" id="status">加载中…</div>
<div class="card">
 <b>粘贴新 cookie</b>，三种格式任选：<span class="muted">
 storage_state.json 全文 / Cookie-Editor 导出的 JSON 数组 /
 DevTools 复制的 <code>SID=xxx; HSID=yyy; …</code></span>
 <textarea id="payload" placeholder='{"cookies": [...], "origins": []}
或 [{"name":"SID","value":"..."}, ...]
或 SID=xxxx; HSID=yyyy; __Secure-1PSIDTS=zzzz; ...'></textarea>
 <button onclick="save()">保存并验证</button>
 <button class="sec" onclick="verify()">仅验证当前 cookie</button>
 <div id="result"></div>
</div>
<script>
async function stat(){const r=await fetch('status?key='+KEY);
const d=await r.json();const el=document.getElementById('status');
if(!d.exists){el.innerHTML='❌ 未找到 storage_state.json<br><span class="muted">'
  +d.path+'</span>';return}
el.innerHTML=(d.has_required?'✅ cookie 文件正常':'⚠️ 缺少必需 cookie: '
  +d.missing.join(', '))+' — '+d.cookies+' 个 cookie, 更新于 '
  +d.updated_at+'<br><span class="muted">'+d.path+'</span>';}
async function save(){const p=document.getElementById('payload').value.trim();
if(!p){alert('请先粘贴 cookie');return}
const r=await fetch('update?key='+KEY,{method:'POST',body:p});
const d=await r.json();
document.getElementById('result').innerHTML='<pre>'+JSON.stringify(d,null,2)
  .replace(/</g,'&lt;')+'</pre>';
stat();}
async function verify(){const r=await fetch('verify?key='+KEY);
const d=await r.json();
 document.getElementById('result').innerHTML='<pre>'+JSON.stringify(d,
   (k,v)=>k==='detail'?undefined:v,null,2)+'\\nstatus: '+(d.ok?'ok':'FAILED')
   +(d.notebook_count!=null?(' / notebooks: '+d.notebook_count):'')+'</pre>';}
const KEY=new URLSearchParams(location.search).get('key');
if(!KEY){document.body.innerHTML='<h1>403</h1>缺少 ?key= 参数';}
else stat();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[%s] %s" % (time.strftime("%H:%M:%S"), fmt % args))

    def _authed(self):
        q = urllib.parse.urlparse(self.path).query
        key = urllib.parse.parse_qs(q).get("key", [""])[0]
        return key == API_KEY

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._authed():
            return self._send(403, '{"error": "key 不对"}')
        # 剥掉反代配置的子路径前缀 (如 /nlm-ui) 再路由
        raw = urllib.parse.urlparse(self.path).path
        path = raw[len(PREFIX):] if PREFIX and raw.startswith(PREFIX) else raw
        if path in ("", "/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/status":
            return self._send(200, json.dumps(file_status(),
                                              ensure_ascii=False))
        if path == "/verify":
            return self._send(200, json.dumps(verify_online(),
                                              ensure_ascii=False))
        self._send(404, '{"error": "not found"}')

    def do_POST(self):
        if not self._authed():
            return self._send(403, '{"error": "key 不对"}')
        raw = urllib.parse.urlparse(self.path).path
        path = raw[len(PREFIX):] if PREFIX and raw.startswith(PREFIX) else raw
        if path != "/update":
            return self._send(404, '{"error": "not found"}')
        n = int(self.headers.get("Content-Length", 0))
        text = self.rfile.read(n).decode("utf-8", "replace")
        try:
            state = parse_input(text)
        except Exception as e:
            return self._send(400, json.dumps(
                {"ok": False, "error": "解析失败: %s" % e}, ensure_ascii=False))
        res = save_state(state)
        if res.get("ok"):
            res["verify"] = verify_online()
            res["hint"] = "MCP server (8422) 会在下一次调用时自动加载新 cookie"
        return self._send(200, json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    if not API_KEY:
        raise SystemExit("错误: .env 中没有 MCP_API_KEY")
    if not CLI.exists():
        raise SystemExit("错误: 找不到 %s" % CLI)
    print("Cookie 更新界面 -> http://%s:%d%s/?key=<MCP_API_KEY>"
          % (HOST, PORT, PREFIX))
    print("storage: %s" % STORAGE)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
