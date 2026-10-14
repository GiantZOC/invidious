"""Throttled client for the Invidious /api/v1 surface.

Only the documented public API is used, so an upstream response-shape change
breaks this module alone.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx


class InvidiousError(RuntimeError):
    pass


class InvidiousClient:
    def __init__(
        self,
        base_url: str,
        token: str = "",
        min_interval: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.min_interval = max(0.0, min_interval)
        self._next_allowed = 0.0
        self._gate = asyncio.Lock()
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(30.0, connect=10.0),
            transport=transport,
            headers={"User-Agent": "invidious-local-sidecar/0.1"},
        )
        # Exposed for /x/health.
        self.last_request_at: float | None = None
        self.last_error: str | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        async with self._gate:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                await asyncio.sleep(wait)

            attempt = 0
            while True:
                attempt += 1
                try:
                    resp = await self._http.get(
                        path, params=params, headers=self._headers()
                    )
                except httpx.HTTPError as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    if attempt >= 3:
                        raise InvidiousError(self.last_error) from exc
                    await asyncio.sleep(2 * attempt)
                    continue

                self.last_request_at = time.time()
                self._next_allowed = time.monotonic() + self.min_interval

                if resp.status_code == 429 and attempt < 3:
                    await asyncio.sleep(10 * attempt)
                    continue

                if resp.status_code >= 400:
                    self.last_error = f"{resp.status_code} for {path}"
                    raise InvidiousError(self.last_error)

                return resp.json()

    # -- authenticated ----------------------------------------------------

    async def history_page(self, page: int = 1, max_results: int = 40) -> list[str]:
        """Bare list of watched video IDs, newest last in Invidious's storage."""
        data = await self._get(
            "/api/v1/auth/history",
            {"page": page, "max_results": max_results},
        )
        return [v for v in data if isinstance(v, str)]

    async def subscriptions(self) -> list[dict[str, Any]]:
        return await self._get("/api/v1/auth/subscriptions")

    async def feed(self, page: int = 1, max_results: int = 40) -> dict[str, Any]:
        return await self._get(
            "/api/v1/auth/feed", {"page": page, "max_results": max_results}
        )

    async def set_preferences(self, payload: dict[str, Any]) -> int:
        """POST a partial preferences document.

        Invidious swallows parse failures here and always answers 204, so the
        caller must re-read preferences to confirm the write landed.
        """
        resp = await self._http.post(
            "/api/v1/auth/preferences",
            json=payload,
            headers=self._headers(),
        )
        self.last_request_at = time.time()
        return resp.status_code

    async def preferences(self) -> dict[str, Any]:
        return await self._get("/api/v1/auth/preferences")

    async def mark_watched(self, video_id: str) -> int:
        resp = await self._http.post(
            f"/api/v1/auth/history/{video_id}", headers=self._headers()
        )
        self.last_request_at = time.time()
        return resp.status_code

    # -- public -----------------------------------------------------------

    async def video(self, video_id: str) -> dict[str, Any]:
        return await self._get(f"/api/v1/videos/{video_id}")
