"""公网制作依赖共用的读取边界；不落第二份素材，不传连接凭证。"""
from __future__ import annotations

import gzip
import hashlib
import http.client
import ipaddress
from pathlib import Path
import socket
import ssl
from urllib.parse import urlsplit
import zlib


def validate_url(url: str):
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("remote_resource_url_invalid: use an HTTP(S) URL without credentials")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError(f"remote_resource_scope_denied: {parsed.hostname} is not a public address")
    return parsed, addresses[0][4][0], port


def fetch(url: str, *, timeout: float = 30, root: Path | None = None,
          max_bytes: int | None = None) -> dict:
    """直接连接本次验证的 IP；重定向交回浏览器，下一跳重新验证，避免 DNS 重绑定。"""
    parsed, address, port = validate_url(url)

    class Connection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.create_connection((address, port), timeout=timeout)
            if parsed.scheme == "https":
                try:
                    self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=parsed.hostname)
                except BaseException:
                    self.sock.close()
                    raise

    connection = Connection(parsed.hostname, port, timeout=timeout)
    try:
        connection.request("GET", (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""),
            headers={"Accept-Encoding": "identity", "User-Agent": "Mozilla/5.0 MURAL-Renderer"})
        response = connection.getresponse()
        body = response.read() if max_bytes is None else response.read(max_bytes + 1)
        if max_bytes is not None and len(body) > max_bytes:
            raise ValueError("remote_resource_too_large: response exceeds the byte budget")
        headers = {key.lower(): value for key, value in response.getheaders()
                   if key.lower() not in {"set-cookie", "connection", "transfer-encoding", "content-length"}}
        encoding = headers.pop("content-encoding", "").lower()
        if max_bytes is not None and encoding in {"gzip", "deflate"}:
            decoder = zlib.decompressobj(31 if encoding == "gzip" else zlib.MAX_WBITS)
            body = decoder.decompress(body, max_bytes + 1)
            if len(body) > max_bytes or decoder.unconsumed_tail:
                raise ValueError("remote_resource_too_large: decoded response exceeds the byte budget")
            if not decoder.eof or decoder.unused_data:
                raise ValueError("remote_resource_encoding_invalid: incomplete or concatenated compressed response")
        elif encoding == "gzip":
            body = gzip.decompress(body)
        elif encoding == "deflate":
            body = zlib.decompress(body)
        elif encoding not in {"", "identity"}:
            raise ValueError(f"remote_resource_encoding_unsupported: {encoding}")
        return {"status": response.status, "headers": headers, "body": body,
                "sha256": hashlib.sha256(body).hexdigest()}
    finally:
        connection.close()


def fulfill(route, *, hashes=None, errors=None, root=None):
    try:
        if route.request.method != "GET":
            raise ValueError("remote_resource_method_denied: rendering dependencies are read-only")
        result = fetch(route.request.url, root=root)
        if hashes is not None:
            hashes[route.request] = result["sha256"]
        route.fulfill(status=result["status"], headers=result["headers"], body=result["body"])
    except (OSError, ValueError, http.client.HTTPException) as exc:
        if errors is not None:
            errors.append(f"remote_resource_failed: {route.request.url}: {exc}")
        route.abort()
