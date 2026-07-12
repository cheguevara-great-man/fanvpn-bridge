"""Safe local route resolution without open-proxy behavior."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .config import RouteConfig
from .contracts import ResolvedRoute
from .errors import BridgeError, ErrorCode


class RouteTable:
    """Maps an explicit local route name to one configured HTTPS upstream."""

    def __init__(self, routes: Mapping[str, RouteConfig]) -> None:
        self._routes = dict(routes)

    def resolve(self, route_name: str, request_target: str) -> ResolvedRoute:
        route = self._routes.get(route_name)
        if route is None:
            raise BridgeError(ErrorCode.ROUTE_NOT_FOUND, f"Unknown route: {route_name}")

        target = urlsplit(request_target)
        if target.scheme or target.netloc or target.fragment:
            raise BridgeError(
                ErrorCode.UPSTREAM_NOT_ALLOWED,
                "Only origin-form request targets are accepted",
            )
        if not target.path.startswith("/") or target.path.startswith("//"):
            raise BridgeError(
                ErrorCode.UPSTREAM_NOT_ALLOWED,
                "Request path must start with one slash",
            )

        forwarded_path = self._remove_prefix(target.path, route.remove_path_prefix)
        base = urlsplit(route.upstream_base_url)
        base_path = base.path.rstrip("/")
        upstream_path = f"{base_path}{forwarded_path}"
        upstream = SplitResult(
            scheme=base.scheme,
            netloc=base.netloc,
            path=upstream_path,
            query=target.query,
            fragment="",
        )
        upstream_url = urlunsplit(upstream)
        check = urlsplit(upstream_url)
        if check.scheme != base.scheme or check.netloc != base.netloc:
            raise BridgeError(ErrorCode.UPSTREAM_NOT_ALLOWED, "Resolved upstream escaped its route")
        return ResolvedRoute(
            name=route.name,
            upstream_base_url=route.upstream_base_url,
            upstream_url=upstream_url,
        )

    def resolve_local_target(self, local_target: str) -> ResolvedRoute:
        """Resolve ``/{route}/{upstream-path}`` from a local HTTP request."""

        parsed = urlsplit(local_target)
        if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/"):
            raise BridgeError(ErrorCode.UPSTREAM_NOT_ALLOWED, "Invalid local request target")
        segments = parsed.path.split("/", 2)
        if len(segments) < 3 or not segments[1]:
            raise BridgeError(ErrorCode.ROUTE_NOT_FOUND, "Local path must include a route name")
        route_name = segments[1]
        upstream_target = f"/{segments[2]}"
        if parsed.query:
            upstream_target = f"{upstream_target}?{parsed.query}"
        return self.resolve(route_name, upstream_target)

    def resolve_probe(self, route_name: str) -> ResolvedRoute:
        route = self._routes.get(route_name)
        if route is None:
            raise BridgeError(ErrorCode.ROUTE_NOT_FOUND, f"Unknown route: {route_name}")
        return self.resolve(route_name, route.probe_path)

    @staticmethod
    def _remove_prefix(path: str, prefix: str) -> str:
        if not prefix:
            return path
        if path == prefix:
            return "/"
        marker = f"{prefix}/"
        if not path.startswith(marker):
            raise BridgeError(
                ErrorCode.UPSTREAM_NOT_ALLOWED,
                f"Request path does not match required prefix {prefix}",
            )
        return path[len(prefix) :]
