"""Safe, payload-free error categories for customer Agent execution."""

from __future__ import annotations

import re
from typing import Any

SAFE_AGENT_ERROR_CATEGORIES = frozenset(
    {
        "AgentTimeout",
        "ConnectError",
        "ConnectTimeout",
        "ConnectionUnavailable",
        "DecodingError",
        "HTTPStatusError",
        "InvalidResponse",
        "JSONDecodeError",
        "NetworkError",
        "PoolTimeout",
        "ProtocolError",
        "ProviderError",
        "ProxyError",
        "ReadError",
        "ReadTimeout",
        "RemoteProtocolError",
        "TimeoutException",
        "TooManyRedirects",
        "ValidationError",
        "WriteError",
        "WriteTimeout",
    }
)

_PROVIDER_ERROR_PREFIX = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*(?::|$)")
_EXECUTION_ERROR_CATEGORIES = {
    "AgentExecutionTimeoutError": "AgentTimeout",
    "AgentConnectionUnavailableError": "ConnectionUnavailable",
}


def safe_agent_error_category(
    *,
    provider_error: Any = None,
    reported_type: Any = None,
    execution_type: Any = None,
) -> str:
    """Return one allow-listed category without retaining provider text.

    ``provider_error`` may contain URLs, payloads, credentials, or arbitrary
    exception messages.  Only its leading exception class is considered and
    only when that class is explicitly allow-listed.  No caller should retain
    the original value after invoking this boundary.
    """

    if isinstance(provider_error, str):
        match = _PROVIDER_ERROR_PREFIX.match(provider_error[:128])
        if match is not None and match.group(1) in SAFE_AGENT_ERROR_CATEGORIES:
            return match.group(1)

    if isinstance(reported_type, str):
        candidate = reported_type.strip()
        if candidate in SAFE_AGENT_ERROR_CATEGORIES:
            return candidate

    if isinstance(execution_type, str):
        mapped = _EXECUTION_ERROR_CATEGORIES.get(execution_type)
        if mapped is not None:
            return mapped

    return "ProviderError"
