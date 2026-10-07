from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional
import asyncio
import httpx


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: Optional[str] = None
    source: str = "local"


class ToolWithFallback:
    """Timeout/retry boundary for future external medical tools."""

    def __init__(self, primary: Callable[..., Awaitable[Any]], fallback: Callable[..., Awaitable[Any]], max_retries: int = 2, timeout: float = 10.0):
        self.primary = primary
        self.fallback = fallback
        self.max_retries = max_retries
        self.timeout = timeout

    async def call(self, **kwargs):
        for _ in range(self.max_retries):
            try:
                return await __import__("asyncio").wait_for(self.primary(**kwargs), timeout=self.timeout)
            except (asyncio.TimeoutError, OSError, httpx.HTTPError):
                continue
        try:
            return await asyncio.wait_for(self.fallback(**kwargs), timeout=self.timeout)
        except (asyncio.TimeoutError, OSError, httpx.HTTPError) as exc:
            return ToolResult(ok=False, error=type(exc).__name__)

