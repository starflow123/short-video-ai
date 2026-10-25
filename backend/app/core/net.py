"""出站地址校验：阻止 base_url / 下载地址指向本机或内网（SSRF）。"""
from __future__ import annotations

import ipaddress
import logging
import socket
import time
from urllib.parse import urlsplit

from app.core.config import settings

logger = logging.getLogger(__name__)

# 明显是内网 / 服务发现用的主机名，解析不出来时也要拦
_INTERNAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home.arpa", ".svc", ".cluster.local")
_DNS_ATTEMPTS = 3


class UnsafeUrlError(ValueError):
    pass


def _is_blocked_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _looks_internal(host: str) -> bool:
    return "." not in host or host.lower().endswith(_INTERNAL_SUFFIXES)


def _resolve(host: str, port: int) -> tuple[set[str], bool]:
    """返回 (解析到的地址集合, 是否彻底解析失败)。容器内 DNS 偶发超时，重试几次再判死。"""
    for attempt in range(_DNS_ATTEMPTS):
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror:
            if attempt < _DNS_ATTEMPTS - 1:
                time.sleep(0.4 * (attempt + 1))
                continue
            return set(), True
        return {str(info[4][0]).split("%", 1)[0] for info in infos}, False
    return set(), True


def validate_outbound_url(url: str, *, allow_private: bool | None = None) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrlError("只允许 http/https 地址")
    host = parsed.hostname
    if not host:
        raise UnsafeUrlError("地址缺少主机名")
    permitted = settings.allow_private_outbound if allow_private is None else allow_private
    if permitted:
        return url
    lowered = host.lower()
    if lowered == "localhost" or lowered.endswith(".localhost"):
        raise UnsafeUrlError("不允许访问本机 / 内网地址")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses, unresolved = _resolve(lowered, port)
    if unresolved:
        # 内网名解析不出来仍然按违规处理；公网域名只是 DNS 抽风，放行后由连接层给出真实网络错误
        if _looks_internal(lowered):
            raise UnsafeUrlError(f"域名无法解析：{host}")
        logger.warning("出站地址校验跳过（DNS 未解析）：%s", url)
        return url
    for address in addresses:
        try:
            blocked = _is_blocked_ip(address)
        except ValueError:
            continue
        if blocked:
            raise UnsafeUrlError("不允许访问本机 / 内网地址")
    return url
