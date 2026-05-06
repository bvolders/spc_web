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
    r">All Areas</td>\s*<td[^>]*>([^<]+)</td>",
    re.IGNORECASE,
)
RE_IMPORTANT = re.compile(
    r"<font[^>]*color=red[^>]*><b>(.*?)</b></font>",
    re.IGNORECASE | re.DOTALL
)
# Submit buttons in the system_summary form. The `name=` attribute is
# firmware-canonical (locale- and installer-invariant); the `value=` is
# locale-dependent and additionally customisable by the engineer at
# commissioning time. We use `name=` for state inference and cache `value=`
# for reverse-lookup of the displayed status text.
RE_SUBMIT_BUTTON = re.compile(
    r'<input\b[^>]*type="submit"[^>]*>',
    re.IGNORECASE,
)
RE_BTN_NAME = re.compile(r'\bname="([^"]+)"', re.IGNORECASE)
RE_BTN_VALUE = re.compile(r'\bvalue="([^"]*)"', re.IGNORECASE)

# Page: controller_status
# Status indicators rendered as `<FONT COLOR="green|red">OK|Fault</FONT>`,
# preceded by a label like "Cabinet Tamper:" — we extract these as
# (label, color, text) triples.
RE_STATUS_INDICATOR = re.compile(
    r"([A-Za-z][A-Za-z0-9.\s]{1,40}?):\s*"
    r'<FONT\s+COLOR="(\w+)"[^>]*>([^<]+)</FONT>',
    re.IGNORECASE,
)
# Numeric value rows: "Battery Voltage: 13.5V" / "Battery Current: 60mA"
RE_NUMERIC_ROW = re.compile(
    r"<td[^>]*>\s*([A-Za-z][A-Za-z0-9.\s]{1,40}?)\s*</td>"
    r"\s*<td[^>]*>\s*(\d+\.?\d*)\s*(V|mA|Hz)?\s*</td>",
    re.IGNORECASE,
)
# Plain key:value diagnostics rows
RE_DIAG_ROW = re.compile(
    r"<td[^>]*>\s*([A-Za-z][^<:]{1,40}?)\s*:?\s*</td>"
    r"\s*<td[^>]*>\s*([^<\s][^<]{0,80}?)\s*</td>",
    re.IGNORECASE,
)

# Page: log
RE_LOG_ENTRY = re.compile(
    r"<TR[^>]*>\s*<TD[^>]*>\s*"
    r"(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2}:\d{2})\s+([^<]+?)\s*"
    r"</TD>",
    re.IGNORECASE,
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


def parse_buttons(html):
    """Yield (name, value) tuples for every <input type=submit> in the HTML.

    The button's `name=` attribute is firmware-canonical
    (e.g. ``partset_a_area1``), while `value=` is the displayed label and
    can be locale-translated AND installer-customised.
    """
    for tag_match in RE_SUBMIT_BUTTON.finditer(html):
        tag = tag_match.group(0)
        name_match = RE_BTN_NAME.search(tag)
        if not name_match:
            continue
        name = name_match.group(1)
        value_match = RE_BTN_VALUE.search(tag)
        value = value_match.group(1) if value_match else ""
        yield name, value


def parse_system_summary_state(html, button_value_cache=None):
    """Extract canonical arm state + arming-button value cache.

    Strategy:
    - Buttons present in the system_summary form indicate available
      transitions: `unset_all_areas` is shown when the panel is currently
      armed, while `partset_*_area1` / `fullset_area1` are shown when it is
      currently disarmed. So armed-vs-disarmed is reliably detectable by
      button presence regardless of locale or installer label customisation.
    - When disarmed, we *also* cache the button `value=` attributes — these
      are the human-readable labels (locale + installer) that the panel will
      display in the "All Areas" status cell when it later transitions to
      that armed mode. Caching lets us reverse-lookup the status text on the
      next armed poll without needing a global label dictionary.

    Returns:
        (arm_state, updated_button_cache)
        arm_state ∈ {"unset", "fullset", "partset_a", "partset_b",
                     "armed_unknown", or whatever the panel reports}
    """
    buttons = dict(parse_buttons(html))
    cache = dict(button_value_cache) if button_value_cache else {}

    has_unset = "unset_all_areas" in buttons
    has_arming = any(
        n in buttons
        for n in ("fullset_area1", "partset_a_area1", "partset_b_area1")
    )

    if has_arming and not has_unset:
        # Currently disarmed. Refresh the cache from the visible arming
        # buttons — these are the labels the panel will display when armed.
        for name, value in buttons.items():
            if name.endswith("_area1") and (
                name.startswith("partset_") or name.startswith("fullset_")
            ):
                cache[name] = value
        return "unset", cache

    if has_unset:
        # Currently armed in some mode. The "All Areas" status cell shows
        # the value attribute of the arming button that produced this state.
        m = RE_ARM_STATE.search(html)
        status_text = m.group(1).strip() if m else ""
        status_lower = status_text.lower()

        # First try the cache — works regardless of locale / installer label.
        for name, value in cache.items():
            if value.strip().lower() == status_lower and value.strip():
                if name == "fullset_area1":
                    return "fullset", cache
                if name == "partset_a_area1":
                    return "partset_a", cache
                if name == "partset_b_area1":
                    return "partset_b", cache

        # Fallback: firmware-canonical English strings (when language=0).
        if status_lower in ("fullset", "full set"):
            return "fullset", cache
        if status_lower in ("partset", "part set", "partset a", "part set a"):
            return "partset_a", cache
        if status_lower in ("partset b", "part set b"):
            return "partset_b", cache

        # Unknown armed state — return the raw label so callers can log it.
        return f"armed_unknown:{status_text}", cache

    # Couldn't determine — neither arming nor unset buttons present.
    raise SPCParseError(
        "Could not determine arm state: no recognised form buttons in "
        "system_summary HTML"
    )


def parse_system_summary_important_message(html):
    re_match = RE_IMPORTANT.search(html)
    if re_match:
        return re_match.group(1).strip()


def parse_controller_status(html):
    """Parse the controller_status page into a dict of diagnostics.

    Returns a dict with two top-level keys:
        "indicators": dict of {label: {"color": str, "text": str}}
            Status indicators (tamper, fuse, X-BUS health, etc.)
        "metrics": dict of {label: (value: float, unit: str|None)}
            Numeric metrics (Battery Voltage, Battery Current, ...)
        "diagnostics": dict of {label: str}
            Plain key:value rows (MAC, IP, system time, ...)
    """
    indicators = {}
    for m in RE_STATUS_INDICATOR.finditer(html):
        label = re.sub(r"\s+", " ", m.group(1)).strip()
        color = m.group(2).strip().lower()
        text = m.group(3).strip()
        indicators[label] = {"color": color, "text": text}

    metrics = {}
    for m in RE_NUMERIC_ROW.finditer(html):
        label = re.sub(r"\s+", " ", m.group(1)).strip()
        try:
            value = float(m.group(2))
        except ValueError:
            continue
        unit = m.group(3) if m.group(3) else None
        metrics[label] = (value, unit)

    diagnostics = {}
    for m in RE_DIAG_ROW.finditer(html):
        label = re.sub(r"\s+", " ", m.group(1)).strip()
        value = re.sub(r"\s+", " ", m.group(2)).strip()
        # Skip rows we already captured as indicators or metrics
        if label in indicators or label in metrics:
            continue
        if not label or not value or value == "-":
            continue
        diagnostics[label] = value

    return {
        "indicators": indicators,
        "metrics": metrics,
        "diagnostics": diagnostics,
    }


def parse_event_log(html, limit=20):
    """Yield up to `limit` recent events from the log page.

    Each event is a dict with:
        "timestamp": "DD/MM/YYYY HH:MM:SS" string
        "message": event description
    """
    count = 0
    for m in RE_LOG_ENTRY.finditer(html):
        if count >= limit:
            break
        date, time, message = m.groups()
        yield {
            "timestamp": f"{date} {time}",
            "message": message.strip(),
        }
        count += 1


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

    async def get_system_summary(self, button_value_cache=None):
        """Fetch the system_summary page and return arm state + button cache.

        Returns:
            (arm_state, updated_button_cache) — see parse_system_summary_state.
        """
        async def do():
            return await self._request(
                "GET", "/secure.htm",
                params={
                    "session": self.sid,
                    "page": "system_summary",
                },
            )

        html = await self._do_with_login(do)
        return parse_system_summary_state(html, button_value_cache)

    async def get_arm_state(self):
        """Backward-compatible: fetch and return only the arm state."""
        arm_state, _ = await self.get_system_summary()
        return arm_state

    async def get_controller_status(self):
        """Fetch the controller_status page and return parsed diagnostics.

        See parse_controller_status() for the return-value shape.
        """
        async def do():
            return await self._request(
                "GET", "/secure.htm",
                params={
                    "session": self.sid,
                    "page": "controller_status",
                },
            )
        html = await self._do_with_login(do)
        return parse_controller_status(html)

    async def get_event_log(self, limit=20):
        """Fetch the log page and return a list of recent events."""
        async def do():
            return await self._request(
                "GET", "/secure.htm",
                params={
                    "session": self.sid,
                    "page": "log",
                },
            )
        html = await self._do_with_login(do)
        return list(parse_event_log(html, limit=limit))

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
