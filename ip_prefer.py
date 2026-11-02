"""IPv4/IPv6 优先控制。

环境变量 NOTEBOOKLM_IP_PREFER:
    4  -> DNS 解析只取 IPv4 (AF_INET) 结果
    6  -> 只取 IPv6 (AF_INET6)
    空/其他 -> 不干预 (系统默认, 通常按 RFC 6724 可能优先 IPv6)

原理: 在 socket.getaddrinfo 外包一层过滤。httpx/anyio 的域名解析最终都
走 socket.getaddrinfo (anyio 在工作线程里调用), 因此对整个 HTTP 栈生效。
查询显式指定了地址族或过滤后为空时保持原结果, 不会把解析搞挂。
"""

import os
import socket

_orig_getaddrinfo = socket.getaddrinfo
_applied = False


def _make_patched(want_family):
    def patched(host, port, family=0, type=0, proto=0, flags=0):
        res = _orig_getaddrinfo(host, port, family, type, proto, flags)
        if family == 0 and res:
            filtered = [r for r in res if r[0] == want_family]
            if filtered:
                return filtered
        return res
    return patched


def apply():
    """按 NOTEBOOKLM_IP_PREFER 安装 getaddrinfo 过滤 (幂等)。"""
    global _applied
    if _applied:
        return
    pref = os.environ.get("NOTEBOOKLM_IP_PREFER", "").strip()
    if pref not in ("4", "6"):
        return
    want = socket.AF_INET if pref == "4" else socket.AF_INET6
    socket.getaddrinfo = _make_patched(want)
    _applied = True
    print("IP_PREFER: IPv%s 优先 (getaddrinfo 已过滤)" % pref)
