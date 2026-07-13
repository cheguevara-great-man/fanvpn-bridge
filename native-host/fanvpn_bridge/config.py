"""Strict, secret-free configuration loading for FanVPN Bridge v2."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .errors import BridgeError, ErrorCode


LOOPBACK_HOST = "127.0.0.1"


@dataclass(frozen=True, slots=True)
class ProtocolConfig:
    max_chunk_bytes: int = 256 * 1024
    max_in_flight: int = 4
    request_timeout_seconds: float = 600.0


@dataclass(frozen=True, slots=True)
class RouteConfig:
    name: str
    upstream_base_url: str
    remove_path_prefix: str = ""
    probe_path: str = "/"
    request_header_allowlist: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    listen_host: str
    listen_port: int
    protocol: ProtocolConfig
    routes: Mapping[str, RouteConfig]


def load_config(path: str | Path) -> BridgeConfig:
    """Load and validate a JSON configuration file."""

    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeError(ErrorCode.CONFIG_INVALID, f"Cannot read config: {exc}") from exc
    return parse_config(raw)


def parse_config(raw: object) -> BridgeConfig:
    root = _require_object(raw, "config")
    _reject_unknown(root, {"listen", "protocol", "routes"}, "config")

    listen = _require_object(root.get("listen"), "listen")
    _reject_unknown(listen, {"host", "port"}, "listen")
    host = _require_string(listen.get("host"), "listen.host")
    if host != LOOPBACK_HOST:
        raise BridgeError(
            ErrorCode.CONFIG_INVALID,
            "listen.host must be 127.0.0.1",
        )
    port = _require_int(listen.get("port"), "listen.port", minimum=0, maximum=65535)

    protocol_raw = _require_object(root.get("protocol", {}), "protocol")
    _reject_unknown(
        protocol_raw,
        {"max_chunk_bytes", "max_in_flight", "request_timeout_seconds"},
        "protocol",
    )
    protocol = ProtocolConfig(
        max_chunk_bytes=_optional_int(
            protocol_raw,
            "max_chunk_bytes",
            256 * 1024,
            minimum=16 * 1024,
            maximum=256 * 1024,
        ),
        max_in_flight=_optional_int(
            protocol_raw,
            "max_in_flight",
            4,
            minimum=1,
            maximum=16,
        ),
        request_timeout_seconds=float(
            _optional_number(
                protocol_raw,
                "request_timeout_seconds",
                600,
                minimum=1,
                maximum=3600,
            )
        ),
    )

    routes_raw = _require_object(root.get("routes"), "routes")
    if not routes_raw:
        raise BridgeError(ErrorCode.CONFIG_INVALID, "At least one route is required")
    routes: dict[str, RouteConfig] = {}
    for name, value in routes_raw.items():
        if not isinstance(name, str) or not name or "/" in name or "\\" in name:
            raise BridgeError(ErrorCode.CONFIG_INVALID, f"Invalid route name: {name!r}")
        route_raw = _require_object(value, f"routes.{name}")
        _reject_unknown(
            route_raw,
            {
                "upstream_base_url",
                "remove_path_prefix",
                "probe_path",
                "request_header_allowlist",
            },
            f"routes.{name}",
        )
        base_url = _require_string(
            route_raw.get("upstream_base_url"),
            f"routes.{name}.upstream_base_url",
        ).rstrip("/")
        _validate_upstream_base_url(base_url, name)
        remove_prefix = route_raw.get("remove_path_prefix", "")
        probe_path = route_raw.get("probe_path", "/")
        if not isinstance(remove_prefix, str) or (remove_prefix and not remove_prefix.startswith("/")):
            raise BridgeError(
                ErrorCode.CONFIG_INVALID,
                f"routes.{name}.remove_path_prefix must be empty or start with /",
            )
        if not isinstance(probe_path, str) or not probe_path.startswith("/"):
            raise BridgeError(
                ErrorCode.CONFIG_INVALID,
                f"routes.{name}.probe_path must start with /",
            )
        header_allowlist = _optional_header_allowlist(
            route_raw.get("request_header_allowlist"),
            f"routes.{name}.request_header_allowlist",
        )
        routes[name] = RouteConfig(
            name=name,
            upstream_base_url=base_url,
            remove_path_prefix=remove_prefix.rstrip("/"),
            probe_path=probe_path,
            request_header_allowlist=header_allowlist,
        )

    return BridgeConfig(
        listen_host=host,
        listen_port=port,
        protocol=protocol,
        routes=routes,
    )


def _validate_upstream_base_url(value: str, route_name: str) -> None:
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise BridgeError(
            ErrorCode.CONFIG_INVALID,
            f"routes.{route_name}.upstream_base_url must be a credential-free HTTPS base URL",
        )


def _require_object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BridgeError(ErrorCode.CONFIG_INVALID, f"{field} must be an object")
    return value


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise BridgeError(ErrorCode.CONFIG_INVALID, f"{field} must be a non-empty string")
    return value


def _require_int(
    value: object,
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise BridgeError(
            ErrorCode.CONFIG_INVALID,
            f"{field} must be an integer from {minimum} to {maximum}",
        )
    return value


def _optional_int(
    values: Mapping[str, object],
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    return _require_int(values.get(key, default), f"protocol.{key}", minimum=minimum, maximum=maximum)


def _optional_number(
    values: Mapping[str, object],
    key: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    value = values.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
        raise BridgeError(
            ErrorCode.CONFIG_INVALID,
            f"protocol.{key} must be from {minimum} to {maximum}",
        )
    return float(value)


def _optional_header_allowlist(value: object, field: str) -> frozenset[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 64:
        raise BridgeError(ErrorCode.CONFIG_INVALID, f"{field} must be an array of at most 64 names")
    names: set[str] = set()
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or any(not (character.isalnum() or character in "!#$%&'*+-.^_`|~") for character in item)
        ):
            raise BridgeError(ErrorCode.CONFIG_INVALID, f"{field} contains an invalid HTTP header name")
        names.add(item.lower())
    return frozenset(names)


def _reject_unknown(values: Mapping[str, object], allowed: set[str], field: str) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise BridgeError(
            ErrorCode.CONFIG_INVALID,
            f"Unknown {field} fields: {', '.join(unknown)}",
        )
