"""HenrikDev VALORANT API client with bounded concurrency and retries."""

import asyncio
import logging

import aiohttp


logger = logging.getLogger(__name__)


class ValorantAPIError(RuntimeError):
    def __init__(self, status, detail):
        self.status = status
        self.detail = detail
        super().__init__(f"API {status}: {detail}")


class HenrikDevClient:
    def __init__(self, base_url, api_key, max_concurrency=3, max_attempts=3):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_attempts = max_attempts
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._session = None

    def bind_session(self, session):
        """Use the bot-owned session so shutdown remains centralized."""
        self._session = session

    async def get(self, path, **params):
        if self._session is None or self._session.closed:
            raise RuntimeError("VALORANT API session is not ready")

        for attempt in range(self.max_attempts):
            retry_delay = None
            try:
                async with self._semaphore:
                    async with self._session.get(
                        self.base_url + path,
                        headers={"Authorization": self.api_key},
                        params=params,
                    ) as response:
                        try:
                            body = await response.json(content_type=None)
                        except (aiohttp.ContentTypeError, ValueError):
                            body = await response.text()

                        if response.status == 429 and attempt + 1 < self.max_attempts:
                            try:
                                retry_delay = max(0.0, min(float(response.headers.get("Retry-After", "1")), 30.0))
                            except ValueError:
                                retry_delay = 1.0
                        elif response.status in (500, 502, 503, 504) and attempt + 1 < self.max_attempts:
                            retry_delay = min(0.5 * (2 ** attempt), 4.0)
                        elif response.status != 200:
                            if isinstance(body, dict):
                                detail = body.get("errors") or body
                            else:
                                detail = body
                            raise ValorantAPIError(response.status, detail)
                        else:
                            if not isinstance(body, dict) or "data" not in body:
                                raise ValorantAPIError(response.status, "response did not contain a data field")
                            return body["data"]
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt + 1 >= self.max_attempts:
                    raise
                retry_delay = min(0.5 * (2 ** attempt), 4.0)

            if retry_delay is not None:
                logger.warning("Retrying VALORANT API request after %.1fs (attempt %d/%d)",
                               retry_delay, attempt + 2, self.max_attempts)
                await asyncio.sleep(retry_delay)

        raise RuntimeError("VALORANT API request exhausted retries")
