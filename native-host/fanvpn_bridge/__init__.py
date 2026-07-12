"""FanVPN Bridge v2 native-host package.

Only stable contracts live here during the architecture phase. Runtime modules
will be added as independently testable implementation slices.
"""

from .contracts import (
    EgressRequest,
    Header,
    HealthSnapshot,
    HealthSnapshotProvider,
    MessageChannel,
    RequestDispatcher,
    ResolvedRoute,
    ResponseSink,
    RouteResolver,
)
from .config import BridgeConfig, ProtocolConfig, RouteConfig, load_config, parse_config
from .dispatcher import NativeDispatcher
from .errors import BridgeError, ErrorCode
from .http_server import BridgeHTTPServer, create_http_server
from .routing import RouteTable

__all__ = [
    "BridgeError",
    "BridgeConfig",
    "BridgeHTTPServer",
    "EgressRequest",
    "ErrorCode",
    "Header",
    "HealthSnapshot",
    "HealthSnapshotProvider",
    "MessageChannel",
    "NativeDispatcher",
    "ProtocolConfig",
    "RequestDispatcher",
    "ResolvedRoute",
    "RouteConfig",
    "RouteTable",
    "ResponseSink",
    "RouteResolver",
    "load_config",
    "parse_config",
    "create_http_server",
]
