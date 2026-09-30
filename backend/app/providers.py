"""模型服务商配置与调用。

- 配置（endpoint/模型名/价格）存 SQLite settings；API Key 只存 Windows Credential
  Manager（keyring），绝不入库、不写文件、不进日志。
- 出站请求安全策略（安全约束）：仅允许 http/https，且目标主机必须解析到公网地址；
  localhost、环回、私有和保留地址一律拒绝。
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx
import keyring

from . import db as store

KEY_SERVICE = "AxolClassNote"
KEYRING_MAX = 4096  # 防御性上限，避免把超大内容当 key 存进去

PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek",
        "api": "openai",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "input_price": 2.0,   # 元 / 百万输入 token，仅用于费用估算
    },
    "openai_compatible": {
        "label": "OpenAI 兼容",
        "api": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "input_price": 0.0,
    },
    "anthropic_compatible": {
        "label": "Anthropic 兼容",
        "api": "anthropic",
        "base_url": "https://api.anthropic.com",
        "model": "claude-3-5-haiku-latest",
        "input_price": 0.0,
    },
}

TIMEOUT = httpx.Timeout(180.0, connect=15.0)


def validate_endpoint(url: str) -> str:
    """校验并规范化出站端点（安全约束）：

    - 仅允许 http/https；
    - 主机必须解析到公网地址（拒绝 localhost/环回/私有/保留地址）；
    - 返回值由校验通过的 scheme/host/port/path 重建，丢弃 userinfo/query/fragment，
      后续请求只允许使用该返回值，不得回用原始配置串。
    """
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"仅允许 http/https 端点，当前: {parsed.scheme or '(空)'}")
    host = parsed.hostname
    if not host:
        raise ValueError("端点缺少主机名")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise ValueError(f"无法解析主机 {host}: {e}") from e
    seen: set[str] = set()
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(
                f"拒绝非公网地址 {ip}：不允许向 localhost/内网/保留地址发送模型请求"
            )
        seen.add(str(ip))
    if not seen:
        raise ValueError(f"主机 {host} 没有可用的解析结果")
    host_for_url = f"[{host}]" if ":" in host else host
    netloc = f"{host_for_url}:{parsed.port}" if parsed.port else host_for_url
    return f"{parsed.scheme}://{netloc}{parsed.path.rstrip('/')}"


def get_provider_cfg(conn, pid: str) -> dict:
    if pid not in PROVIDERS:
        raise ValueError(f"未知服务商: {pid}")
    cfg = dict(PROVIDERS[pid])
    s = store.get_settings(conn)
    for field in ("base_url", "model", "input_price"):
        v = s.get(f"provider:{pid}:{field}")
        if v not in (None, ""):
            cfg[field] = float(v) if field == "input_price" else v
    cfg["has_key"] = has_key(pid)
    return cfg


def has_key(pid: str) -> bool:
    try:
        return keyring.get_password(KEY_SERVICE, pid) is not None
    except keyring.errors.KeyringError:
        return False


def set_key(pid: str, key: str) -> None:
    if pid not in PROVIDERS:
        raise ValueError(f"未知服务商: {pid}")
    key = key.strip()
    if not key or len(key) > KEYRING_MAX:
        raise ValueError("API Key 为空或超出长度限制")
    keyring.set_password(KEY_SERVICE, pid, key)


def delete_key(pid: str) -> None:
    if pid not in PROVIDERS:
        raise ValueError(f"未知服务商: {pid}")
    try:
        keyring.delete_password(KEY_SERVICE, pid)
    except keyring.errors.PasswordDeleteError:
        pass


def call_model(pid: str, system: str, user: str) -> str:
    """向用户配置的外部模型 API 发起一次生成请求。

    出站边界校验（SSRF 防护，请求前于本函数内完成）：
    1. 端点仅允许 http/https；
    2. 主机名必须解析到公网 IP（拒绝 localhost/环回/私有/保留地址，含云元数据地址）；
    3. 连接目标直接使用校验过的 IP 字面量（防 DNS rebinding），HTTP 层以 Host 头、
       TLS 层以 sni_hostname 扩展保留原主机名，证书校验不受影响；
    4. 丢弃 userinfo/query/fragment，禁止重定向（follow_redirects=False）。
    """
    with store.db() as conn:
        cfg = get_provider_cfg(conn, pid)
    key = keyring.get_password(KEY_SERVICE, pid)
    if not key:
        raise ValueError(f"服务商 {cfg['label']} 尚未保存 API Key")

    parsed = urlparse(cfg["base_url"].strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"仅允许 http/https 端点，当前: {parsed.scheme or '(空)'}")
    host = parsed.hostname
    if not host:
        raise ValueError("端点缺少主机名")
    try:
        addr_infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise ValueError(f"无法解析主机 {host}: {e}") from e

    verified_ip: str | None = None
    verified_v4: str | None = None
    for info in addr_infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(
                f"拒绝非公网地址 {ip}：不允许向 localhost/内网/保留地址发送模型请求"
            )
        if verified_ip is None:
            verified_ip = str(ip)
        if ip.version == 4 and verified_v4 is None:
            verified_v4 = str(ip)
    if verified_ip is None:
        raise ValueError(f"主机 {host} 没有可用的解析结果")
    # 双栈主机优先用 IPv4 直连，避免本机无 IPv6 路由时连不上
    connect_ip = verified_v4 or verified_ip

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    # 连接目标 = 已校验 IP；原主机名经 Host 头与 sni_hostname 保留
    url = f"{parsed.scheme}://{connect_ip}:{port}{parsed.path.rstrip('/')}"
    headers: dict[str, str] = {"Host": host}
    model = cfg["model"]

    if cfg["api"] == "anthropic":
        url = f"{url}/v1/messages"
        headers |= {"x-api-key": key, "anthropic-version": "2023-06-01",
                    "content-type": "application/json"}
        payload = {"model": model, "max_tokens": 8192, "system": system,
                   "messages": [{"role": "user", "content": user}]}
    else:
        url = f"{url}/chat/completions"
        headers |= {"Authorization": f"Bearer {key}",
                    "content-type": "application/json"}
        payload = {"model": model, "temperature": 0.2, "stream": False, "max_tokens": 8192,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}]}

    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as client:
            req = client.build_request("POST", url, headers=headers, json=payload,
                                       extensions={"sni_hostname": host})
            resp = client.send(req)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as e:
        detail = e.response.text[:300]
        raise ValueError(f"模型 API 返回 {e.response.status_code}：{detail}") from e
    except httpx.HTTPError as e:
        raise ValueError(f"模型 API 请求失败：{e}") from e

    if cfg["api"] == "anthropic":
        parts = [c.get("text", "") for c in data.get("content", []) if c.get("type") == "text"]
        text = "".join(parts).strip()
    else:
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
    if not text:
        raise ValueError("模型 API 返回了空内容")
    return text
