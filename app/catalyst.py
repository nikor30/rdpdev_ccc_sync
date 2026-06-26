"""Thin synchronous client for the Cisco Catalyst Center (DNA Center) REST API."""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class CatalystError(RuntimeError):
    pass


class CatalystClient:
    """Handles token auth and the few endpoints we need.

    Auth flow: POST Basic credentials to /dna/system/api/v1/auth/token -> {"Token": ...}
    Subsequent calls carry X-Auth-Token.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        verify_ssl: bool = True,
        timeout: int = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._auth = (username, password)
        self._token: str | None = None
        self._client = httpx.Client(verify=verify_ssl, timeout=timeout)

    # -- low level ------------------------------------------------------------

    def authenticate(self) -> None:
        url = f"{self.base_url}/dna/system/api/v1/auth/token"
        try:
            resp = self._client.post(url, auth=self._auth)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise CatalystError(f"Authentication failed: {exc}") from exc
        token = resp.json().get("Token")
        if not token:
            raise CatalystError("Authentication response did not contain a token")
        self._token = token

    def _headers(self) -> dict[str, str]:
        return {"X-Auth-Token": self._token or "", "Content-Type": "application/json"}

    def _get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        if not self._token:
            self.authenticate()
        url = f"{self.base_url}{path}"
        resp = self._client.get(url, headers=self._headers(), params=params)
        if resp.status_code == 401:  # token expired -> re-auth once
            self.authenticate()
            resp = self._client.get(url, headers=self._headers(), params=params)
        try:
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise CatalystError(f"GET {path} failed: {exc}") from exc
        return resp.json()

    # -- endpoints ------------------------------------------------------------

    def get_devices(self, page_size: int = 500) -> list[dict]:
        """All network devices (paginated, 1-based offset)."""
        out: list[dict] = []
        offset = 1
        while True:
            data = self._get(
                "/dna/intent/api/v1/network-device",
                {"limit": page_size, "offset": offset},
            )
            batch = data.get("response", []) or []
            out.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size
        return out

    def get_sites(self, page_size: int = 500) -> list[dict]:
        """The full site hierarchy (areas, buildings, floors)."""
        out: list[dict] = []
        offset = 1
        while True:
            data = self._get(
                "/dna/intent/api/v1/site",
                {"limit": page_size, "offset": offset},
            )
            batch = data.get("response", []) or []
            out.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size
        return out

    def get_site_members(self, site_id: str) -> list[dict]:
        """Network devices that are members of a given site."""
        data = self._get(
            f"/dna/intent/api/v1/site-member/{site_id}/member",
            {"memberType": "networkdevice"},
        )
        return data.get("response", []) or []

    def probe(self) -> dict[str, Any]:
        """Cheap authenticated sanity check for the "Test connection" button.

        Authenticates and hits the network-device endpoint with a tiny page to
        prove the token works and the inventory API is reachable. The device
        count (a separate, best-effort call) is included when available.
        """
        self.authenticate()
        self._get("/dna/intent/api/v1/network-device", {"limit": 1, "offset": 1})
        info: dict[str, Any] = {}
        try:
            count = self._get("/dna/intent/api/v1/network-device/count")
            info["device_count"] = count.get("response")
        except CatalystError:
            info["device_count"] = None
        return info

    def close(self) -> None:
        self._client.close()
