#!/usr/bin/env python3
"""One-time desktop OAuth with PKCE. Save refresh token locally; never print it."""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import secrets
import tempfile
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
from dotenv import dotenv_values

from google_api import SCOPE, TOKEN_URL, GoogleError


def save_refresh_token(path: Path, token: str, original: str):
    if not re.fullmatch(r"[A-Za-z0-9_./~+\-=]+", token):
        raise GoogleError("刷新令牌格式无效；未写入配置")
    if path.read_text() != original:
        raise GoogleError("授权期间 .env 被修改；保留现有配置，请重新授权")
    lines, replaced = [], False
    for line in original.splitlines():
        if re.match(r"^\s*GOOGLE_REFRESH_TOKEN\s*=", line):
            if not replaced:
                lines.append("GOOGLE_REFRESH_TOKEN=" + token)
                replaced = True
        else:
            lines.append(line)
    if not replaced:
        lines.append("GOOGLE_REFRESH_TOKEN=" + token)
    fd, temporary = tempfile.mkstemp(prefix=".oauth-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def authorize(path: Path):
    original = path.read_text()
    values = dotenv_values(path, interpolate=False)
    client_id = values.get("GOOGLE_CLIENT_ID")
    client_secret = values.get("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret or "REPLACE" in client_id or "REPLACE" in client_secret:
        raise GoogleError("请先在本地 .env 填写 Desktop OAuth 的 GOOGLE_CLIENT_ID 与 GOOGLE_CLIENT_SECRET")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    callback = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Callback query contains an authorization code; never log it.

        def do_GET(self):
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            valid = parts.path == "/" and secrets.compare_digest(query.get("state", [""])[0], state)
            if not valid:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Invalid authorization callback.")
                return
            callback["code"] = query.get("code", [None])[0]
            callback["error"] = "error" in query
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("已收到授权结果，请回到终端。".encode())

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server.timeout = 1
        redirect_uri = f"http://127.0.0.1:{server.server_port}"
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
            "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        if not webbrowser.open(url):
            raise GoogleError("无法打开系统浏览器；请在有浏览器的本地电脑执行授权脚本")
        print("请在系统浏览器中完成 Google 日历只读授权；令牌将直接保存到本地 .env。")
        deadline = time.monotonic() + 300
        while not callback and time.monotonic() < deadline:
            server.handle_request()
        if not callback or callback.get("error") or not callback.get("code"):
            raise GoogleError("授权未完成、被拒绝或已超时；未写入配置")
    response = requests.post(TOKEN_URL, data={
        "client_id": client_id, "client_secret": client_secret,
        "code": callback["code"], "code_verifier": verifier,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
    }, timeout=(30, 30), allow_redirects=False)
    if response.status_code != 200:
        raise GoogleError("Google 授权码兑换失败；未写入配置，不输出响应正文")
    payload = response.json()
    if SCOPE not in payload.get("scope", "").split() or not payload.get("refresh_token"):
        raise GoogleError("未获得日历只读权限或 refresh token；未写入配置")
    save_refresh_token(path, payload["refresh_token"], original)
    print("授权成功。刷新令牌已写入本地 .env，权限设为 0600；未在终端显示令牌。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(".env"))
    args = parser.parse_args()
    os.umask(0o077)
    try:
        authorize(args.env)
        return 0
    except GoogleError as error:
        print(str(error))
    except Exception as error:
        print(f"授权失败（{type(error).__name__}）；未显示可能含秘密的异常详情。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
