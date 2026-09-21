"""Normalize optional response metadata for OpenAI-compatible endpoints."""

from __future__ import annotations

import json

import httpx
from pydantic import JsonValue, TypeAdapter

_JSON = TypeAdapter(JsonValue)


class CompatibleTransport(httpx.AsyncBaseTransport):
    """Keep SDK validation while filling its required, nullable fingerprint field."""

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self._transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._transport.handle_async_request(request)
        if not response.is_success or not request.url.path.endswith("/chat/completions"):
            return response
        await response.aread()
        try:
            payload = _JSON.validate_json(response.content)
        except ValueError:
            return response
        if not isinstance(payload, dict) or "system_fingerprint" in payload:
            return response
        payload["system_fingerprint"] = None
        headers = dict(response.headers)
        # aread() has decoded the original body; the replacement is plain JSON.
        headers.pop("content-encoding", None)
        headers.pop("content-length", None)
        return httpx.Response(
            response.status_code,
            headers=headers,
            content=json.dumps(payload).encode("utf-8"),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._transport.aclose()
