"""Small SFTPGo REST adapter; error bodies and credentials never escape this boundary."""

import json
import ssl
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from vmctl.config import Configuration
from vmctl.errors import VmctlError
from vmctl.fileshare.config import FileshareSettings, read_api_key


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise VmctlError("Fileshare API redirects are forbidden")


class SFTPGoClient:
    def __init__(self, config: Configuration, settings: FileshareSettings) -> None:
        self.settings = settings
        self.key = read_api_key(config, settings)
        try:
            context = ssl.create_default_context(cafile=str(config.path(settings.ca_file)))
        except (OSError, ssl.SSLError) as exc:
            raise VmctlError("Cannot load fileshare TLS CA certificate") from exc
        self.opener = build_opener(NoRedirect(), HTTPSHandler(context=context))

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        missing_ok: bool = False,
    ) -> Any:
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            self.settings.endpoint.rstrip("/") + "/api/v2/" + path,
            data=body,
            method=method,
            headers={"X-SFTPGO-API-KEY": self.key, "Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=self.settings.timeout_seconds) as response:
                data = response.read(4 * 1024 * 1024 + 1)
                if len(data) > 4 * 1024 * 1024:
                    raise VmctlError("Fileshare API response exceeds size limit")
                return json.loads(data) if data else None
        except HTTPError as exc:
            if missing_ok and exc.code == 404:
                return None
            raise VmctlError(f"Fileshare API {method} failed (HTTP {exc.code})") from None
        except (URLError, OSError, ValueError):
            raise VmctlError("Fileshare API request failed; verify service connectivity") from None
