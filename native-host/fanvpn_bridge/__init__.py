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
from .errors import BridgeError, ErrorCode

__all__ = [
    "BridgeError",
    "EgressRequest",
    "ErrorCode",
    "Header",
    "HealthSnapshot",
    "HealthSnapshotProvider",
    "MessageChannel",
    "RequestDispatcher",
    "ResolvedRoute",
    "ResponseSink",
    "RouteResolver",
]
