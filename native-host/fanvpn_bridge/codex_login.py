"""One-shot Codex OAuth login whose token exchange travels through the Bridge."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import BinaryIO, Callable, Sequence, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener


DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_ISSUER = "https://auth.openai.com"
DEFAULT_BRIDGE_URL = "http://127.0.0.1:18888"
DEFAULT_CALLBACK_PORTS = (1455, 1457)
DEFAULT_TIMEOUT_SECONDS = 600
_TOKEN_RESPONSE_LIMIT = 1024 * 1024
_SCOPES = "openid profile email offline_access api.connectors.read api.connectors.invoke"
_CLIENT_ID_ENV = "CODEX_APP_SERVER_LOGIN_CLIENT_ID"


class CodexLoginError(RuntimeError):
    """Safe, user-facing login failure that never contains OAuth secrets."""


@dataclass(frozen=True, slots=True)
class PkceCodes:
    verifier: str
    challenge: str


@dataclass(frozen=True, slots=True)
class ExchangedTokens:
    id_token: str
    access_token: str
    refresh_token: str


@dataclass(frozen=True, slots=True)
class CodexLoginResult:
    auth_path: Path
    backup_path: Path | None
    callback_port: int


BrowserOpener = Callable[[str], bool]
TokenExchanger = Callable[[str, str, str], ExchangedTokens]


def generate_pkce() -> PkceCodes:
    verifier = _base64url(secrets.token_bytes(64))
    challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return PkceCodes(verifier=verifier, challenge=challenge)


def build_authorize_url(
    *,
    redirect_uri: str,
    pkce: PkceCodes,
    state: str,
    client_id: str = DEFAULT_CLIENT_ID,
    issuer: str = DEFAULT_ISSUER,
) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": _SCOPES,
            "code_challenge": pkce.challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "state": state,
            "originator": "codex_cli_rs",
        }
    )
    return f"{issuer.rstrip('/')}/oauth/authorize?{query}"


def exchange_code_via_bridge(
    *,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    bridge_url: str = DEFAULT_BRIDGE_URL,
    client_id: str = DEFAULT_CLIENT_ID,
    timeout: float = 60,
) -> ExchangedTokens:
    token_url = f"{bridge_url.rstrip('/')}/auth-openai/oauth/token"
    _require_loopback_http_url(token_url)
    body = urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
        }
    ).encode("ascii")
    request = Request(
        token_url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with _direct_opener().open(request, timeout=timeout) as response:
            payload = _read_limited(response, _TOKEN_RESPONSE_LIMIT)
    except HTTPError as error:
        detail = _safe_oauth_error(_read_limited(error, _TOKEN_RESPONSE_LIMIT))
        suffix = f": {detail}" if detail else ""
        raise CodexLoginError(f"Token exchange returned HTTP {error.code}{suffix}") from None
    except (URLError, TimeoutError, OSError) as error:
        raise CodexLoginError(
            "Could not reach the local Bridge token route; verify /ready and auth-openai"
        ) from error

    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CodexLoginError("Token exchange returned invalid JSON") from error
    if not isinstance(value, dict):
        raise CodexLoginError("Token exchange returned an invalid object")
    fields: dict[str, str] = {}
    for name in ("id_token", "access_token", "refresh_token"):
        item = value.get(name)
        if not isinstance(item, str) or not item or len(item) > _TOKEN_RESPONSE_LIMIT:
            raise CodexLoginError(f"Token exchange response is missing {name}")
        fields[name] = item
    _decode_jwt_payload(fields["id_token"])
    return ExchangedTokens(**fields)


def build_auth_document(tokens: ExchangedTokens) -> dict[str, object]:
    claims = _decode_jwt_payload(tokens.id_token)
    auth_claims = claims.get("https://api.openai.com/auth")
    account_id = auth_claims.get("chatgpt_account_id") if isinstance(auth_claims, dict) else None
    if not isinstance(account_id, str) or not account_id:
        account_id = None
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": tokens.id_token,
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "account_id": account_id,
        },
        "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def save_auth_document(codex_home: Path, document: dict[str, object]) -> tuple[Path, Path | None]:
    codex_home = codex_home.expanduser().resolve()
    codex_home.mkdir(parents=True, exist_ok=True)
    auth_path = codex_home / "auth.json"
    backup_path: Path | None = None
    if auth_path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = codex_home / f"auth.json.before-browser-login.{stamp}.bak"
        counter = 1
        while backup_path.exists():
            backup_path = codex_home / f"auth.json.before-browser-login.{stamp}.{counter}.bak"
            counter += 1
        shutil.copy2(auth_path, backup_path)
        _restrict_auth_file(backup_path)

    serialized = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".auth.",
            suffix=".tmp",
            dir=codex_home,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, auth_path)
        temporary = None
        _restrict_auth_file(auth_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return auth_path, backup_path


def run_codex_login(
    *,
    codex_home: Path,
    bridge_url: str = DEFAULT_BRIDGE_URL,
    client_id: str | None = None,
    callback_ports: Sequence[int] = DEFAULT_CALLBACK_PORTS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    browser_path: Path | None = None,
    browser_opener: BrowserOpener | None = None,
    token_exchanger: TokenExchanger | None = None,
) -> CodexLoginResult:
    _check_bridge_ready(bridge_url)
    client_id = client_id or os.environ.get(_CLIENT_ID_ENV) or DEFAULT_CLIENT_ID
    pkce = generate_pkce()
    state = _base64url(secrets.token_bytes(32))
    server = _bind_callback_server(callback_ports)
    callback_port = int(server.server_address[1])
    redirect_uri = f"http://localhost:{callback_port}/auth/callback"
    auth_url = build_authorize_url(
        redirect_uri=redirect_uri,
        pkce=pkce,
        state=state,
        client_id=client_id,
    )
    exchanger = token_exchanger or (
        lambda code, callback, verifier: exchange_code_via_bridge(
            code=code,
            redirect_uri=callback,
            code_verifier=verifier,
            bridge_url=bridge_url,
            client_id=client_id,
        )
    )
    server.session = _LoginSession(
        state=state,
        redirect_uri=redirect_uri,
        code_verifier=pkce.verifier,
        codex_home=codex_home,
        exchanger=exchanger,
    )
    opener = browser_opener or (lambda url: _open_chrome(url, browser_path))
    try:
        if not opener(auth_url):
            raise CodexLoginError("Google Chrome could not be opened for Codex login")
        deadline = time.monotonic() + timeout_seconds
        server.timeout = min(1.0, max(0.05, timeout_seconds))
        while not server.session.completed.is_set() and time.monotonic() < deadline:
            server.handle_request()
        if not server.session.completed.is_set():
            raise CodexLoginError("Codex login timed out before the browser callback completed")
        if server.session.error is not None:
            raise server.session.error
        if server.session.result is None:
            raise CodexLoginError("Codex login ended without credentials")
        return CodexLoginResult(
            auth_path=server.session.result[0],
            backup_path=server.session.result[1],
            callback_port=callback_port,
        )
    finally:
        server.server_close()


@dataclass(slots=True)
class _LoginSession:
    state: str
    redirect_uri: str
    code_verifier: str
    codex_home: Path
    exchanger: TokenExchanger
    completed: threading.Event = field(default_factory=threading.Event)
    result: tuple[Path, Path | None] | None = None
    error: CodexLoginError | None = None

class _CallbackServer(HTTPServer):
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int]) -> None:
        self.session: _LoginSession
        super().__init__(address, _CallbackHandler)


class _CallbackHandler(BaseHTTPRequestHandler):
    server_version = "BrowserAIBridgeLogin/1.0"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        server = cast(_CallbackServer, self.server)
        parsed = urlsplit(self.path)
        if parsed.path == "/cancel":
            server.session.error = CodexLoginError("Codex login was cancelled")
            server.session.completed.set()
            self._send_page(200, "登录已取消", "可以关闭此页面。")
            return
        if parsed.path != "/auth/callback":
            self._send_page(404, "页面不存在", "此本地页面只处理 Codex 登录回调。")
            return
        values = parse_qs(parsed.query, keep_blank_values=True)
        returned_state = _single_query_value(values, "state")
        if returned_state is None or not hmac.compare_digest(returned_state, server.session.state):
            self._send_page(400, "登录校验失败", "state 不匹配，请返回 Codex 重新开始登录。")
            return
        oauth_error = _single_query_value(values, "error")
        if oauth_error:
            server.session.error = CodexLoginError("Authorization failed in the browser")
            server.session.completed.set()
            self._send_page(400, "登录未完成", "OpenAI 没有返回授权码。")
            return
        code = _single_query_value(values, "code")
        if not code:
            server.session.error = CodexLoginError("Authorization callback did not include a code")
            server.session.completed.set()
            self._send_page(400, "登录未完成", "回调中缺少授权码。")
            return
        try:
            tokens = server.session.exchanger(
                code,
                server.session.redirect_uri,
                server.session.code_verifier,
            )
            document = build_auth_document(tokens)
            server.session.result = save_auth_document(server.session.codex_home, document)
        except CodexLoginError as error:
            server.session.error = error
            self._send_page(502, "Token Exchange 失败", str(error))
        except Exception as error:
            server.session.error = CodexLoginError(
                f"Could not save Codex credentials ({type(error).__name__})"
            )
            self._send_page(500, "保存登录信息失败", "未修改现有 Codex 登录文件。")
        else:
            self._send_page(200, "Codex 登录成功", "可以关闭此页面并重新打开 VS Code。")
        finally:
            server.session.completed.set()

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send_page(self, status: int, title: str, message: str) -> None:
        body = (
            "<!doctype html><meta charset='utf-8'><title>Codex 登录</title>"
            "<style>body{font-family:system-ui;margin:4rem;max-width:48rem}"
            "h1{color:#173b6c}p{line-height:1.7}</style>"
            f"<h1>{_html_escape(title)}</h1><p>{_html_escape(message)}</p>"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


def _check_bridge_ready(bridge_url: str) -> None:
    _require_loopback_http_url(bridge_url)
    try:
        ready = _get_local_json(f"{bridge_url.rstrip('/')}/ready", timeout=5)
        routes = _get_local_json(f"{bridge_url.rstrip('/')}/routes", timeout=5)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
        raise CodexLoginError("Browser AI Bridge is not ready on 127.0.0.1") from error
    if ready.get("ready") is not True:
        raise CodexLoginError("Browser AI Bridge is running but its Chrome executor is not ready")
    route_names = routes.get("routes")
    if not isinstance(route_names, list) or "auth-openai" not in route_names:
        raise CodexLoginError("The running Bridge does not include the auth-openai route")


def _get_local_json(url: str, *, timeout: float) -> dict[str, object]:
    request = Request(url, headers={"Accept": "application/json"})
    with _direct_opener().open(request, timeout=timeout) as response:
        raw = _read_limited(response, 1024 * 1024)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def _bind_callback_server(ports: Sequence[int]) -> _CallbackServer:
    errors: list[OSError] = []
    for port in ports:
        if not isinstance(port, int) or not 0 <= port <= 65535:
            raise CodexLoginError("Invalid OAuth callback port")
        try:
            return _CallbackServer(("127.0.0.1", port))
        except OSError as error:
            errors.append(error)
    raise CodexLoginError("OAuth callback ports 1455 and 1457 are already in use") from (
        errors[-1] if errors else None
    )


def _open_chrome(url: str, browser_path: Path | None) -> bool:
    executable = browser_path.expanduser().resolve() if browser_path else _find_chrome()
    if executable is None or not executable.is_file():
        location = f" at {executable}" if executable is not None else ""
        raise CodexLoginError(f"Google Chrome was not found{location}")
    subprocess.Popen(
        [str(executable), url],
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    return True


def _find_chrome() -> Path | None:
    candidates = [
        Path(root) / "Google/Chrome/Application/chrome.exe"
        for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
        if (root := os.environ.get(name))
    ]
    on_path = shutil.which("chrome.exe") or shutil.which("chrome")
    if on_path:
        candidates.append(Path(on_path))
    return next((path for path in candidates if path.is_file()), None)


def _restrict_auth_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if os.name != "nt" or not os.environ.get("USERNAME"):
        return
    try:
        subprocess.run(
            [
                "icacls.exe",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{os.environ['USERNAME']}:(R,W)",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _decode_jwt_payload(token: str) -> dict[str, object]:
    parts = token.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        raise CodexLoginError("OpenAI returned an invalid ID token")
    try:
        padding = "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(parts[1] + padding)
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CodexLoginError("OpenAI returned an invalid ID token") from error
    if not isinstance(value, dict):
        raise CodexLoginError("OpenAI returned an invalid ID token")
    return value


def _safe_oauth_error(raw: bytes) -> str | None:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    error = value.get("error")
    if isinstance(error, str) and error and all(
        character.isalnum() or character in "_-" for character in error
    ):
        return error[:80]
    if isinstance(error, dict):
        error_type = error.get("type")
        if isinstance(error_type, str) and error_type and all(
            character.isalnum() or character in "_-" for character in error_type
        ):
            return error_type[:80]
    return None


def _read_limited(response: BinaryIO, limit: int) -> bytes:
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise CodexLoginError("OAuth response exceeded the size limit")
    return raw


def _direct_opener():
    return build_opener(ProxyHandler({}))


def _require_loopback_http_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise CodexLoginError("Bridge URL must be an HTTP loopback address")
    if parsed.username or parsed.password or parsed.fragment:
        raise CodexLoginError("Bridge URL contains unsupported components")


def _single_query_value(values: dict[str, list[str]], name: str) -> str | None:
    items = values.get(name)
    return items[0] if items and len(items) == 1 else None


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )
