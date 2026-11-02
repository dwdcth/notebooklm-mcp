# notebooklm-mcp

将 [notebooklm-py](https://github.com/teng-lin/notebooklm-py) 封装为 **带 Bearer 鉴权的 HTTP MCP 服务**（FastMCP，streamable HTTP），让任意 MCP 客户端（Claude Code / Cursor / Cherry Studio 等）直接操作你的 NotebookLM：管理笔记本与来源、基于来源问答、生成并下载播客/测验/思维导图/报告等 artifact。

## 已在本机验证

- cookie 从本地 Chrome 直接提取（`notebooklm login --browser-cookies chrome`），无需在浏览器里重新登录
- MCP 鉴权：无 key / 错 key → 401，正确 key → 200
- 真实调用：`list_notebooks`（26 个）、`set_active_notebook`、`list_sources`（9 个来源）、`ask`（返回基于来源的真实回答）

## 安装（已完成的过程，供重建参考）

```bash
cd ~/notebooklm-mcp
uv venv && uv pip install "notebooklm-py[browser]" fastmcp rookie-cookies

# 从本地 Chrome 提取已登录的 Google cookie (Chrome 需处于登录状态)
.venv/bin/notebooklm login --browser-cookies chrome
.venv/bin/notebooklm auth check --test --json   # 期望 "status": "ok"

cp .env.example .env   # 填 MCP_API_KEY (openssl rand -base64 32)
```

## 启动

```bash
.venv/bin/python server.py       # MCP 服务: http://127.0.0.1:8422/mcp
.venv/bin/python update_ui.py    # Cookie 更新界面: http://127.0.0.1:8423/?key=<MCP_API_KEY>
# 或常驻: systemctl --user enable --now notebooklm-mcp
```

服务地址 `http://127.0.0.1:8422/mcp`，请求头需带 `Authorization: Bearer <MCP_API_KEY>`。

## Cookie 更新界面 (update_ui.py)

会话失效时打开 `http://127.0.0.1:8423/?key=<MCP_API_KEY>`，把新 cookie 粘进输入框一键保存。支持三种格式（自动识别）：

1. `storage_state.json` 全文
2. Cookie-Editor 等浏览器扩展导出的 JSON 数组
3. 浏览器 DevTools 复制的原始 cookie 文本 `SID=xxx; HSID=yyy; ...`

保存前自动校验必需 cookie（`SID` + `__Secure-1PSIDTS`，缺失直接拒绝并提示），旧文件自动备份为 `storage_state.json.bak-时间戳`，权限强制 600；保存后立即联网验证并在页面显示结果。**MCP server 无需重启**——它检测到 cookie 文件变化后会在下一次调用时自动重建客户端。页面另有"仅验证当前 cookie"按钮（跑 `auth check --test`）。

## MCP 客户端配置示例

Claude Code / 通用 mcp.json：

```json
{
  "mcpServers": {
    "notebooklm": {
      "type": "http",
      "url": "http://127.0.0.1:8422/mcp",
      "headers": { "Authorization": "Bearer <你的 MCP_API_KEY>" }
    }
  }
}
```

远程（VPS）访问用 SSH 隧道，不要把端口暴露公网：

```bash
ssh -N -L 8422:127.0.0.1:8422 vps
# 本地仍用 http://127.0.0.1:8422/mcp
```

## 工具一览（17 个）

| 分类 | 工具 |
|---|---|
| 认证 | `auth_status` |
| 笔记本 | `list_notebooks` `create_notebook` `set_active_notebook` `rename_notebook` `delete_notebook`(需 confirm) |
| 来源 | `list_sources` `add_source_url` `add_source_text` `add_source_file` `get_source_fulltext` `delete_source` |
| 问答 | `ask` |
| Artifact | `generate_artifact`(audio/video/quiz/flashcards/mind_map/report/data_table/slide_deck/infographic) `list_artifacts` `download_artifact` `wait_artifact` |

多数工具的 `notebook_id` 可省略——先 `set_active_notebook` 设定当前笔记本即可。所有工具异常都以 `{"error": ...}` 返回，服务不崩。

## 文件说明

```
server.py        MCP 服务 (FastMCP + StaticTokenVerifier, .env 读 key, cookie 热重载)
update_ui.py     Cookie 更新 Web 界面 (纯标准库, 端口 8423, 同一 key 鉴权)
test_client.py   端到端测试 (连本机服务调真实工具)
.env             实际配置 (含 key, 权限 600, 勿提交)
.env.example     配置模板
notebooklm-mcp.service  systemd user service 模板
```

## 维护注意

- **cookie 过期**：Google 会话几天到几周失效。日常续期是纯 HTTP（RotateCookies，server 运行时自动做，无需浏览器）；彻底失效后从 DevTools/扩展复制新 cookie 到更新界面（8423），或本机 Chrome 在登录态时 `notebooklm login --browser-cookies chrome`
- **安全**：`.env` 与 `~/.notebooklm/profiles/*/storage_state.json` 都等于 Google 账号钥匙，保持 600 权限，不要提交到仓库；8422/8423 都只监听 127.0.0.1，远程一律走 SSH 隧道
- 非官方 API（Google 可能随时变更）；重度调用可能被限流

---

## 公网部署 (nginx/openresty + pm2, 通用示例)

```
MCP 客户端 ──https──> nginx (your-domain.example) ──> pm2: nlm-mcp(8422)
浏览器   ──https──> /nlm-ui ──> pm2: nlm-ui(8423) ──> notebooklm.google.com
```

> **⚠️ 部署环境需要干净的网络出口。** NotebookLM 有区域/反滥用门禁，会拒绝
> 数据中心/机房 IP（请求被重定向到营销页并报 `location=unsupported`）。
> 实测常见情况：同一台服务器的 **IPv6 出口被拒而 IPv4 出口放行**（系统解析
> 默认优先 IPv6，导致默认走 v6 被拦）。此时在 `.env` 设
> `NOTEBOOKLM_IP_PREFER=4` 强制 IPv4 即可，无需代理；v4 也被拒时再考虑
> 住宅网络/自备代理（`.env` 设 `HTTPS_PROXY=http://...`）。
> 快速判断: `curl -4 -I https://notebooklm.google.com` 与 `curl -6 -I ...` 对比。

- **MCP 端点**: `https://your-domain.example/nlm-mcp`（Streamable HTTP, `Authorization: Bearer <key>`）
- **Cookie 界面**: `https://your-domain.example/nlm-ui/?key=<key>`
- 服务器上 clone 本仓库，`uv venv && uv sync --frozen`；`.env` 参考 `.env.example` 配置
  （子路径部署设 `MCP_PATH_PREFIX=/nlm-mcp`、`UI_PATH_PREFIX=/nlm-ui`）
- 进程管理: `pm2 start .venv/bin/python --name nlm-mcp -- server.py`（nlm-ui 同理），`pm2 save`

### nginx / openresty 反代配置示例

两个 location 都用 `^~` 前缀匹配（优先级高于正则 location，避免被站点已有的 `location ~ /` 兜底抢走）；
`proxy_pass` 不带 URI，原始路径原样透传给后端（后端通过 `MCP_PATH_PREFIX` / `UI_PATH_PREFIX` 挂到同一路径）。
MCP 走 Streamable HTTP（响应是流式 event-stream），必须关缓冲并放宽超时，否则长任务（生成播客/视频）会被卡断。

`nlm-mcp.conf`（→ 8422，MCP 端点）:

```nginx
location ^~ /nlm-mcp {
    proxy_pass http://127.0.0.1:8422;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Port $server_port;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $http_connection;
    proxy_buffering off;          # 流式响应不缓冲 (关键)
    proxy_cache off;
    proxy_read_timeout 3600s;     # 长生成任务不超时 (关键)
    proxy_send_timeout 3600s;
}
```

`nlm-ui.conf`（→ 8423，cookie 更新界面）:

```nginx
location ^~ /nlm-ui {
    proxy_pass http://127.0.0.1:8423;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Port $server_port;
    proxy_http_version 1.1;
    client_max_body_size 10m;     # storage_state.json 可能较大
    proxy_read_timeout 120s;      # 在线验证 (auth check) 需要外网往返
}
```

对应后端 `.env`: `MCP_PATH_PREFIX=/nlm-mcp`、`UI_PATH_PREFIX=/nlm-ui`。
直接裸跑（不经反代）则两者留空/保持默认（`/mcp`、根路径）。改完配置记得 `nginx -t && nginx -s reload`。
