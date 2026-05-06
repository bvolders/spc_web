import logging
import re
import ssl

import httpx

from homeassistant.helpers.httpx_client import get_async_client


# Page: any
RE_TITLE = re.compile(
    r"<title>([^<]+?)</title>",
    re.IGNORECASE
)

# Page: any after logging in
RE_SERIAL = re.compile(
    r"S/N:\s*([0-9A-Za-z]+)"
)
RE_SESSION = re.compile(
    r"(?:\?|&)session=(0x[0-9A-Fa-f]+)"
)

# Page: login
RE_LOGIN = re.compile(
    r"\baction=login\b",
    re.IGNORECASE
)
RE_DENIED = re.compile(
    r"\bAccess\s+denied\b",
    re.IGNORECASE
)

# Page: system_summary
RE_ARM_STATE = re.compile(
    r">All Areas</td><td[^>]*>([^<]+)</td>",
    re.IGNORECASE,
)
RE_IMPORTANT = re.compile(
    r"<font[^>]*color=red[^>]*><b>(.*?)</b></font>",
    re.IGNORECASE | re.DOTALL
)

# Page: status_zones
RE_ZONE = re.compile(
    r"<TR\s+HEIGHT=20>"
    # (1) zone id, (2) zone name
    r"\s*<TD\s+ALIGN=\"center\">(\d+)\s+([^<]+)</TD>"
    # (3) area id, (4) area name
    r"\s*<TD\s+ALIGN=\"center\">(\d+)\s+([^<]+)</TD>"
    # (5) zone type
    r"\s*<TD\s+ALIGN=\"center\">([^<]+)</TD>"
    # (6) (commented out) input state
    r".*?<!--.*?<font[^>]*>(?:<b>)?([^<]+)(?:</b>)?</font>.*?-->"
    # (7) status
    r"\s*<TD\s+ALIGN=\"center\"><FONT\s+COLOR=\w+>(?:<B>)?([^<]+)(?:</B>)?</FONT></TD>",
    re.IGNORECASE | re.DOTALL
)

LOGGER = logging.getLogger(__name__)


def create_spc_session(hass, url, userid, password, verify_ssl=True):
    """Create an instance of SPCSession using the default HASS httpx client.
    Use this when connecting to SPC over HTTP or when using modern TLS."""

    client = get_async_client(hass, verify_ssl=verify_ssl)
    return SPCSession(client, url, userid, password)


def create_legacy_ssl_spc_session(url, userid, password):
    """Create an instance of SPCSession using a custom httpx client that
    is configured for legacy TLS. This version can connect to the SPC
    panel over HTTPS directly.
    Await session.client.aclose() to release the underlying httpx client."""

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0),
        verify=get_legacy_ssl_context(),
    )
    return SPCSession(client, url, userid, password)


def get_legacy_ssl_context():
    """
    SSL context compatible with SPC panels.

    SPC requires:
    - TLS 1.2 only
    - invalid/self-signed cert
    - legacy RSA cipher (AES256-SHA)
    - legacy renegotiation
    """

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers("AES256-SHA")
    ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def parse_title(html):
    """Return [model, site] parsed from the HTML title, falling back to blanks."""
    re_match = RE_TITLE.search(html)
    if re_match:
        result = (re_match.group(1).split(" - ", 1) + [""])[:2]
    else:
        result = ("", "")
    return [s.strip() for s in result]


def parse_serial_number(html):
    re_match = RE_SERIAL.search(html)
    return (re_match.group(1) if re_match else "")


def parse_session_id(html):
    re_match = RE_SESSION.search(html)
    if re_match:
        return re_match.group(1)
    raise SPCParseError("Session ID not found in HTML")


def parse_system_summary_arm_state(html):
    re_match = RE_ARM_STATE.search(html)
    if re_match:
        return re_match.group(1).strip().lower()
    raise SPCParseError("Arm state not found in HTML")


def parse_system_summary_important_message(html):
    re_match = RE_IMPORTANT.search(html)
    if re_match:
        return re_match.group(1).strip()


def parse_status_zones(html):
    for m in RE_ZONE.finditer(html):
        yield {
            "zone_id": int(m.group(1)),
            "zone_name": m.group(2).strip(),
            "area_id": int(m.group(3)),
            "area_name": m.group(4).strip(),
            # Alarm, Entry/Exit, ...
            "zone_type": m.group(5).strip().lower(),
            # For inhibited zones, this shows the underlying status
            # Open, Closed, DISCON, ...
            "input": m.group(6).strip().lower(),
            # Normal, Tamper, Inhibit, ...
            "status": m.group(7).strip().lower(),
        }


def is_login_page(html):
    return bool(RE_LOGIN.search(html))


def is_login_access_denied(html):
    return bool(RE_DENIED.search(html))


class SPCError(Exception):
    pass


class SPCParseError(SPCError):
    pass


class SPCLoginError(SPCError):
    pass


class SPCCommandError(SPCError):
    pass


class SPCSession:
    """Represents a web session with the SPC panel."""

    def __init__(self, client, url, userid, password):
        self.client = client
        self.url = url.rstrip("/")
        self.creds = {
            "userid": userid,
            "password": password,
        }

        self.sid = ""               # session ID
        self.model = ""             # panel model name
        self.serial_number = ""     # panel serial number
        self.site = ""              # alarm site name

    async def _request(self, method, path, params=None, data=None):
        resp = await self.client.request(
            method, self.url + path,
            params={
                "language": "0",
            } | (params or {}),
            data=data,
        )

        resp.raise_for_status()
        html = resp.text

        self.model, self.site = parse_title(html)
        return html

    async def _do_with_login(self, do):
        if self.sid:
            html = await do()
            if not is_login_page(html):
                return html
        await self.login()
        return await do()

    async def login(self):
        """Log in and populate sid, serial, model, and site."""

        html = await self._request(
            "POST", "/login.htm",
            params={"action": "login"},
            data=self.creds,
        )

        if is_login_page(html):
            if is_login_access_denied(html):
                raise SPCLoginError("SPC login failed: access denied")
            raise SPCLoginError("SPC login failed: still on login page")

        self.sid = parse_session_id(html)
        self.serial_number = parse_serial_number(html)

    async def get_arm_state(self):
        """Fetch current arm state (all areas)."""

        async def do():
            return await self._request(
                "GET", "/secure.htm",
                params={
                    "session": self.sid,
                    "page": "system_summary",
                },
            )

        html = await self._do_with_login(do)
        return parse_system_summary_arm_state(html)

    async def set_arm_state(self, arm_state):
        """Send a command to change the arm state (all areas).
        Returns the new arm state (as returned by SPC)."""

        if arm_state == "unset":
            data = {"unset_all_areas": "Unset"}
        elif arm_state == "fullset":
            data = {"fullset_area1": "Fullset"}
        elif arm_state == "forceset":
            data = {"fullset_force1": "Force set"}
        elif arm_state == "partset_a":
            # Part-set level A — perimeter only (door/window contacts armed,
            # PIRs disabled). Used as "armed_night" in HA. The button's
            # value attribute is locale-dependent ("KONTAKTEN" in Dutch,
            # "Partset" in English etc.) — the panel only checks form-field
            # presence, value contents are ignored.
            data = {"partset_a_area1": "Partset"}
        elif arm_state == "partset_b":
            data = {"partset_b_area1": "Partset"}
        else:
            raise SPCCommandError(f"{arm_state}: unknown arm state")

        async def do():
            return await self._request(
                "POST", "/secure.htm",
                params={
                    "session": self.sid,
                    "page": "system_summary",
                    "action": "update",
                },
                data=data,
            )

        html = await self._do_with_login(do)
        msg = parse_system_summary_important_message(html)
        if msg:
            raise SPCCommandError(msg)
        return parse_system_summary_arm_state(html)

    async def get_zones(self):
        """Fetch a list of zones. Each zone is a dictionary with
        following keys: zone_id, zone_name, area_id, area_name,
        zone_type, input, status."""

        async def do():
            return await self._request(
                "GET", "/secure.htm",
                params={
                    "session": self.sid,
                    "page": "status_zones",
                },
            )

        html = await self._do_with_login(do)
        return list(parse_status_zones(html))

    async def set_zone_inhibit(self, zone_id, inhibit):
        """Inhibit or deinhibit a zone. Returns the new state
        of the zone."""

        if inhibit:
            data = {f"inhibit{zone_id}": "Inhibit"}
        else:
            data = {f"uninhibit{zone_id}": "Deinhibit"}

        async def do():
            return await self._request(
                "POST", "/secure.htm",
                params={
                    "session": self.sid,
                    "page": "status_zones",
                    "action": "update",
                    # XXX website always sends this for some reason
                    "zone": "1",
                },
                data=data,
            )

        html = await self._do_with_login(do)
        return next((zone for zone in parse_status_zones(html)
                     if zone["zone_id"] == zone_id), None)
