"""HTTP session for the Fabric Engine OpenAPI (``transport="https"``).

The switch serves the API at ``https://<switch>:9443/rest/openapi`` once
``openapi local-mgmt enable`` is configured (Premier license). Requests carry
a token from ``POST /v0/operation/auth-token/:generate`` in the
``X-Auth-Token`` header; it is valid for 24 hours. The schema also has
``auth-token/:disable`` and ``:enable``, without a description; they may
switch token authentication off for the whole switch, so they are never
called.

Error bodies contain a ``debugLog`` with the decoded token (user, source
address, expiry); it is never passed on.
"""

import ipaddress
import logging
from typing import Any, Dict, List, Optional, Union

import requests
import urllib3
from napalm.base.exceptions import CommandErrorException, ConnectionException

logger = logging.getLogger(__name__)

BASE_PATH = "/rest/openapi"
TOKEN_PATH = "/v0/operation/auth-token/:generate"
CLI_PATH = "/v0/operation/system/cli"
SAVE_CONFIG_PATH = "/v0/operation/system/config/:save"
DEFAULT_PORT = 9443

# statusCode of a command the CLI operation skipped after a failed one
SKIPPED = 409

# Fields of an error body that are safe to show ("debugLog" is not)
ERROR_FIELDS = ("errorCode", "errorMessage", "resource", "status", "title", "detail")


def _host(hostname: str) -> str:
    try:
        if ipaddress.ip_address(hostname).version == 6:
            return f"[{hostname}]"
    except ValueError:
        pass
    return hostname


class OpenApiSession:
    """Token-authenticated HTTPS session to the switch's OpenAPI."""

    def __init__(
        self,
        hostname: str,
        username: str,
        password: str,
        port: int = DEFAULT_PORT,
        verify: Union[bool, str] = False,
        timeout: int = 60,
    ) -> None:
        self.base_url = f"https://{_host(hostname)}:{port}{BASE_PATH}"
        self.username = username
        self.password = password
        self.verify = verify
        self.timeout = timeout
        self.session: Optional[requests.Session] = None
        self.token: Optional[str] = None

    def open(self) -> None:
        if not self.verify:
            # like NAPALM's NX-API client: no warning on every request
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.session = requests.Session()
        self.session.verify = self.verify
        self.session.headers["Accept"] = "application/json"
        self._login()

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
        self.session = None
        self.token = None

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        if self.session is None:
            raise ConnectionException("OpenAPI session is not open, call open() first")
        logger.debug("%s %s%s", method, self.base_url, path)
        kwargs.setdefault("timeout", self.timeout)
        try:
            return self.session.request(method, self.base_url + path, **kwargs)
        except requests.RequestException as exc:
            raise ConnectionException(f"OpenAPI {method} {path} failed: {exc}") from exc

    def _login(self) -> None:
        self.token = None
        self.session.headers.pop("X-Auth-Token", None)  # type: ignore[union-attr]
        response = self._request(
            "POST", TOKEN_PATH, json={"username": self.username, "password": self.password}
        )
        if response.status_code != 200:
            raise ConnectionException(f"OpenAPI login failed: {self._error(response)}")
        token = response.json().get("token")
        if not token:
            raise ConnectionException("OpenAPI login failed: no token in the reply")
        self.token = token
        self.session.headers["X-Auth-Token"] = token  # type: ignore[union-attr]

    def _call(self, method: str, path: str, ok: tuple, **kwargs: Any) -> requests.Response:
        """Send a request; an expired token (401) is renewed once."""
        response = self._request(method, path, **kwargs)
        if response.status_code == 401:
            self._login()
            response = self._request(method, path, **kwargs)
        if response.status_code not in ok:
            raise CommandErrorException(f"OpenAPI {method} {path} failed: {self._error(response)}")
        return response

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """GET ``path`` (below ``/rest/openapi``) and return the decoded JSON."""
        return self._call("GET", path, (200,), params=params).json()

    def cli(
        self,
        commands: List[str],
        continue_on_failure: bool = True,
        timeout: Optional[int] = None,
    ) -> List[str]:
        """Run CLI commands (``POST /v0/operation/system/cli``), return their output.

        With ``continue_on_failure`` (default) every command runs. Without it
        the switch skips the commands after the first one it counts as failed
        (``statusCode`` 409, Fabric Engine 9.4); they are left out, so fewer
        outputs than commands are returned. Otherwise ``statusCode`` is not
        used: the switch counts any output containing "Error" as failed
        (``show sys-info`` prints ``System Error Info``), so the caller checks
        the output like CLI output over SSH. The output is the same as over
        SSH, without the ``Command Execution Time`` banner. Every request
        starts in privileged EXEC mode; pipes (``| include``) are rejected.
        """
        reply = self._call(
            "POST",
            CLI_PATH,
            (200, 207),
            params={"continue_on_failure": "true" if continue_on_failure else "false"},
            json=list(commands),
            timeout=timeout or self.timeout,
        ).json()
        # error messages come with "\r\n" (9.4: "^\r\n% Invalid input ..."), as over SSH: "\n"
        outputs = [
            entry.get("cliOutput", "").replace("\r\n", "\n")
            for entry in reply.get("data", [])
            if continue_on_failure or entry.get("statusCode") != SKIPPED
        ]
        if len(outputs) > len(commands) or (continue_on_failure and len(outputs) != len(commands)):
            raise CommandErrorException(
                f"OpenAPI CLI returned {len(outputs)} outputs for {len(commands)} commands"
            )
        return outputs

    def save_config(self, name: Optional[str] = None) -> None:
        """Save the running configuration (``POST /v0/operation/system/config/:save``).

        Without ``name`` to the boot configuration file (``save config``),
        otherwise to the file ``name`` (``save config file <name>``). The
        schema answers ``204`` on success.
        """
        body = {"name": name} if name else {}
        self._call("POST", SAVE_CONFIG_PATH, (200, 204), json=body)

    @staticmethod
    def _error(response: requests.Response) -> str:
        """Status and the safe fields of an error body (never ``debugLog``)."""
        try:
            body = response.json()
        except ValueError:
            return f"HTTP {response.status_code}"
        if not isinstance(body, dict):
            return f"HTTP {response.status_code}"
        details = {k: body[k] for k in ERROR_FIELDS if k in body}
        status = f"HTTP {response.status_code}"
        return f"{status} {details}" if details else status
