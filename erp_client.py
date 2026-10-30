"""IITKGP ERP session handling: OTP login, session reuse, notice-board fetch."""
import logging
import re
import time
import xml.etree.ElementTree as ET
from http.cookiejar import LWPCookieJar

import requests

import config
import otp_mail

log = logging.getLogger(__name__)

ORIGIN = "https://erp.iitkgp.ac.in"
HOME = f"{ORIGIN}/IIT_ERP3/"
SSO = f"{ORIGIN}/SSOAdministration"
TP = f"{ORIGIN}/TrainingPlacementSSO"
TP_REFERER = f"{TP}/TPStudent.jsp"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
AJAX = {"X-Requested-With": "XMLHttpRequest", "Origin": ORIGIN}


class SessionExpired(Exception):
    """The saved cookies no longer authenticate us."""


class TruncatedResponse(Exception):
    """The ERP returned a partial XML document."""


class LoginFailed(Exception):
    """Credentials, security answer or OTP were rejected."""


class ErpClient:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})
        self.last_attachments = {}
        self.s.cookies = LWPCookieJar(str(config.COOKIE_FILE))
        if config.COOKIE_FILE.exists():
            try:
                self.s.cookies.load(ignore_discard=True, ignore_expires=True)
                log.debug("Loaded saved ERP session")
            except Exception as exc:
                log.warning("Could not load saved cookies: %s", exc)

    # -- session persistence ------------------------------------------------
    def _save(self):
        try:
            self.s.cookies.save(ignore_discard=True, ignore_expires=True)
            config.COOKIE_FILE.chmod(0o600)
        except Exception as exc:
            log.warning("Could not save cookies: %s", exc)

    def _cookie(self, name):
        for c in self.s.cookies:
            if c.name == name:
                return c.value
        return None

    # -- login --------------------------------------------------------------
    def _answer_for(self, question: str) -> str:
        if config.ERP_SECURITY_ANSWERS_PROBLEM:
            log.warning(
                "ERP_SECURITY_ANSWERS is partly unreadable: %s",
                config.ERP_SECURITY_ANSWERS_PROBLEM,
            )
        q = (question or "").lower()
        for needle, answer in config.ERP_SECURITY_ANSWERS.items():
            if needle.lower() in q:
                log.info("Security question matched rule %r", needle)
                return answer
        log.info("Security question %r -> using default answer", question.strip()[:80])
        return config.ERP_SECURITY_ANSWER_DEFAULT

    def login(self):
        log.info("Starting fresh ERP login (this will trigger an OTP mail)")
        self.s.cookies.clear()

        r = self.s.get(HOME, timeout=30)
        r.raise_for_status()
        token_m = re.search(r'name="sessionToken"[^>]*value="([^"]*)"', r.text)
        url_m = re.search(r'name="requestedUrl"[^>]*value="([^"]*)"', r.text)
        if not token_m:
            raise LoginFailed("Could not find sessionToken on the login page")
        session_token = token_m.group(1)
        requested_url = url_m.group(1) if url_m else HOME

        headers = dict(AJAX, Referer=r.url)

        q = self.s.post(
            f"{SSO}/getSecurityQues.htm",
            data={"user_id": config.ERP_USER_ID},
            headers=headers,
            timeout=30,
        )
        question = (q.text or "").strip()
        answer = self._answer_for(question)

        payload = {
            "user_id": config.ERP_USER_ID,
            "password": config.ERP_PASSWORD,
            "answer": answer,
            "typeee": "SI",
            "email_otp": "",
            "sessionToken": session_token,
            "requestedUrl": requested_url,
        }

        since = otp_mail.mark()
        o = self.s.post(f"{SSO}/getEmilOTP.htm", data=payload, headers=headers, timeout=60)
        msg = ""
        try:
            msg = (o.json() or {}).get("msg", "")
        except ValueError:
            msg = (o.text or "").strip()[:200]
        log.info("OTP request response: %s", msg)
        if msg and re.search(r"invalid|incorrect|wrong|not match|failed", msg, re.I):
            raise LoginFailed(f"ERP rejected the OTP request: {msg}")

        payload["email_otp"] = otp_mail.wait_for_otp(since)
        log.info("Submitting login with OTP")

        auth = self.s.post(
            f"{SSO}/auth.htm",
            data=payload,
            headers={"Origin": ORIGIN, "Referer": r.url},
            allow_redirects=True,
            timeout=60,
        )

        # Keep the untouched auth.htm response: later hops overwrite `auth`, and
        # this is the one response that actually says why a login failed.
        first_auth = auth
        self._dump(auth, "last_auth_response.html")

        sso_token, hop = self._extract_token(auth)

        # auth.htm hands the token back through a JS/meta redirect rather than a
        # 302, so requests' redirect following never sees it. Walk it by hand.
        hops = 0
        while not sso_token and hop and hops < 3:
            hops += 1
            hop = requests.compat.urljoin(auth.url, hop)
            log.info("Following in-page redirect to %s", hop.split("?")[0])
            auth = self.s.get(hop, headers={"Referer": auth.url}, timeout=30)
            sso_token, hop = self._extract_token(auth)

        if sso_token and hop:
            # Land on the page the token was meant for, so the app-side session
            # is actually created and not just handed to us as a string.
            try:
                self.s.get(
                    requests.compat.urljoin(auth.url, hop),
                    headers={"Referer": auth.url},
                    timeout=30,
                )
            except Exception as exc:
                log.warning("Could not follow post-login hop: %s", exc)

        if not sso_token or "login.htm" in (auth.url or ""):
            raise LoginFailed(self._explain_auth_failure(first_auth))

        log.info("Login succeeded")
        self._enter_placement_module(sso_token)
        self._save()

    # -- company list (read-only) --------------------------------------------
    def fetch_companies(self) -> list[dict]:
        """The company list (grid 37). Its owning page must be rendered first."""
        last = ""
        for attempt in range(1, 4):
            self.s.get(f"{TP}/TPStudent.jsp", headers={"Referer": f"{HOME}home.htm"}, timeout=60)
            r = self.s.get(
                f"{TP}/ERPMonitoring.htm",
                params={"action": "fetchData", "jqqueryid": "37", "_search": "false",
                        "nd": str(int(time.time() * 1000)), "rows": "500", "page": "1",
                        "sidx": "", "sord": "asc", "totalrows": "500"},
                headers={"Accept": "application/xml, text/xml, */*; q=0.01",
                         "X-Requested-With": "XMLHttpRequest", "Referer": TP_REFERER},
                allow_redirects=False, timeout=90)
            if r.status_code in (301, 302, 303, 307, 308):
                raise SessionExpired(f"Redirected to {r.headers.get('Location', '?')}")
            body = r.text or ""
            if r.status_code == 200 and self._complete(body):
                return parse_companies(body)
            last = body
            log.warning("Partial company list (%d bytes, attempt %d/3)", len(body), attempt)
            time.sleep(attempt)
        raise TruncatedResponse(f"company list incomplete after 3 attempts ({len(last)} bytes)")

    def jnf_state(self, year: str, com_id: str, jnf_id: str) -> dict:
        """What the portal's own page for this profile shows you (a plain read)."""
        r = self.s.get(
            f"{TP}/TPJNFView.jsp",
            params={"jnf_id": jnf_id, "com_id": com_id, "yop": year,
                    "user_type": "SU", "rollno": config.ERP_USER_ID},
            headers={"Referer": TP_REFERER}, allow_redirects=False, timeout=60)
        if r.status_code in (301, 302, 303, 307, 308):
            raise SessionExpired(f"Redirected to {r.headers.get('Location', '?')}")
        st = parse_jnf_page(r.text)
        st["http"] = r.status_code
        return st

    def apply_with_cv(self, st: dict, cv: int):
        """Submit the application the portal is offering on a profile's page.

        THIS SUBMITS AN APPLICATION. `st` must be a fresh jnf_state() whose state is
        "eligible"; every parameter comes from that page's own Apply button, so
        nothing is guessed. Refuses (ValueError) if the chosen CV is not offered, the
        button is for someone else, or the page's script no longer builds the request
        we expect. Returns (http_status, short_text_of_portal_reply).
        """
        offer = (st.get("apply") or {}).get(cv)
        if st.get("state") != "eligible" or not offer:
            raise ValueError(f"CV{cv} is not offered on this profile right now")
        if offer["rollno"] != config.ERP_USER_ID:
            raise ValueError("the Apply button is for a different roll number; not applying")
        if not offer["js_ok"]:
            raise ValueError("the portal's apply script changed; refusing to guess the request")

        r = self.s.get(
            f"{TP}/TPJNFViewAction.jsp",
            params={"mode": f"ApplyCV{cv}", "rollno": offer["rollno"], "year": offer["year"],
                    "com_id": offer["com_id"], "jnf_id": offer["jnf_id"]},
            headers={"Referer": (f"{TP}/TPJNFView.jsp?jnf_id={offer['jnf_id']}&com_id={offer['com_id']}"
                                 f"&yop={offer['year']}&user_type=SU&rollno={offer['rollno']}"),
                     "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
            allow_redirects=False, timeout=90)

        # The portal normally answers an apply with a redirect back to the profile
        # page (TPJNFView.jsp). That is the expected reply, NOT a failure: success is
        # decided by re-reading the profile page afterwards, never by this response.
        # Only a redirect to the login/SSO pages means the session died.
        location = r.headers.get("Location", "") if r.status_code in (301, 302, 303, 307, 308) else ""
        if location and re.search(r"login|SSOAdministration", location, re.I):
            raise SessionExpired(f"Redirected to {location}")
        if location:
            return r.status_code, f"redirected to {location}"[:300]
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", r.text or "")
        text = " ".join(re.sub(r"<[^>]+>", " ", text).replace("&nbsp;", " ").split())
        return r.status_code, text[:300]

    def download_attachment(self, year: str, notice_id: str, max_bytes: int = 15_000_000):
        """Fetch a notice's PDF. Returns (bytes, filename, content_type) or None."""
        try:
            r = self.s.get(
                ATTACH_URL,
                params={"type": "NOTICE", "year": year, "id": notice_id},
                headers={"Referer": f"{TP}/{config.NOTICE_PAGE}"},
                timeout=120,
            )
        except Exception as exc:
            log.warning("Attachment %s/%s failed: %s", year, notice_id, exc)
            return None

        if r.status_code != 200 or not r.content:
            log.warning("Attachment %s/%s: HTTP %s", year, notice_id, r.status_code)
            return None
        if len(r.content) > max_bytes:
            log.warning("Attachment %s/%s too large (%d bytes); skipping",
                        year, notice_id, len(r.content))
            return None

        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
        if r.content[:4] == b"%PDF":
            ctype, ext = "application/pdf", "pdf"
        else:
            ext = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png",
                   "application/msword": "doc"}.get(ctype, "bin")
            if ext == "bin" and b"<html" in r.content[:400].lower():
                log.warning("Attachment %s/%s returned a web page, not a file",
                            year, notice_id)
                return None
        return r.content, f"notice-{notice_id}.{ext}", ctype

    def _extract_token(self, resp) -> tuple[str | None, str | None]:
        """Find the ssoToken, and any in-page redirect target, in a response.

        The ERP returns it in whichever of three places it feels like: a cookie,
        the final URL, or embedded in a JS/meta redirect in the body.
        """
        body = resp.text or ""

        target = None
        m = re.search(
            r"""location\s*(?:\.\s*(?:href|replace)\s*(?:=|\(\s*))\s*["']([^"']+)["']"""
            r"""|<meta[^>]+http-equiv=["']?refresh["']?[^>]*url=([^"'>\s]+)"""
            # Only treat a form action as a hop when it actually carries the
            # token. The login page's own <form action="auth.htm"> is not a
            # redirect, and GETting it just returns 405 Method Not Allowed.
            r"""|<form[^>]+action=["']([^"']*ssoToken=[^"']+)["']""",
            body,
            re.I,
        )
        if m:
            target = next((g for g in m.groups() if g), None)

        # Never chase a hop back to the login or auth endpoints; that is the ERP
        # telling us we failed, not a redirect to follow.
        if target and re.search(r"(auth|login)\.htm", target, re.I) and "ssoToken=" not in target:
            target = None

        token = self._cookie("ssoToken")
        if not token:
            for haystack in (resp.url or "", target or "", body):
                hit = re.search(r"ssoToken=([A-Za-z0-9._-]{16,})", haystack)
                if hit:
                    token = hit.group(1)
                    break
        return token, target

    def _dump(self, resp, name: str):
        """Save a response for diagnosis. Written 0600: it can contain a token."""
        path = config.STATE_DIR / name
        try:
            path.write_text(resp.text or "")
            path.chmod(0o600)
            return path
        except Exception as exc:
            log.warning("Could not save %s: %s", name, exc)
            return None

    def _explain_auth_failure(self, resp) -> str:
        """Turn the auth.htm response into something actionable, and keep a copy."""
        body = resp.text or ""

        dump = self._dump(resp, "last_auth_failure.html")

        # Pull out whatever the page says went wrong.
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", body)
        text = " ".join(re.sub(r"<[^>]+>", " ", text).split())
        reason = ""
        for pattern in (
            r"((?:invalid|incorrect|wrong|expired|failed|not match|already)[^.!]{0,90})",
            r"([^.!]{0,60}(?:otp|password|answer|captcha|session|log(?:ged|in))[^.!]{0,60})",
        ):
            hit = re.search(pattern, text, re.I)
            if hit:
                reason = hit.group(1).strip()
                break

        parts = ["Login did not produce an ssoToken."]
        if reason:
            parts.append(f'The ERP said: "{reason}"')
        else:
            parts.append(f"The page gave no error text (landed on {resp.url}).")
        if dump:
            parts.append(f"Full response saved to {dump} for inspection.")
        return " ".join(parts)

    def _enter_placement_module(self, sso_token: str):
        """Exchange the SSO token for a TrainingPlacementSSO session."""
        candidates = []
        try:
            mods = self.s.post(
                f"{HOME}getModules.htm",
                headers=dict(AJAX, Referer=f"{HOME}home.htm?ssoToken={sso_token}"),
                timeout=30,
            )
            for url in re.findall(r"https?://[^\"'\s<>]*TrainingPlacementSSO[^\"'\s<>]*", mods.text):
                candidates.append(url)
            if candidates:
                log.info("Discovered placement module URL from getModules.htm")
        except Exception as exc:
            log.warning("getModules.htm lookup failed: %s", exc)

        candidates += [
            f"{TP}/TPStudent.jsp",
            f"{TP}/",
            f"{TP}/index.jsp",
        ]

        for base in candidates:
            url = base if "ssoToken=" in base else (
                f"{base}{'&' if '?' in base else '?'}ssoToken={sso_token}"
            )
            try:
                self.s.get(url, headers={"Referer": f"{HOME}home.htm"}, timeout=30)
            except Exception as exc:
                log.warning("Module bootstrap via %s failed: %s", base, exc)
                continue
            if self._cookie("JSID_TrainingPlacementSSO") or self._cookie("JSESSIONID"):
                try:
                    self._warm()
                    self._raw_fetch()
                    log.info("Placement module session established via %s", base)
                    return
                except SessionExpired:
                    continue
        raise LoginFailed("Logged in, but could not open the Training & Placement module")

    # -- notice board -------------------------------------------------------
    def _raw_fetch(self, attempts: int = 3) -> str:
        """Fetch the grid, retrying a truncated document.

        The ERP intermittently cuts the response short (a bare
        "<rows><page>1</page>" with no closing tag) on the ~1 MB notice board.
        Left unchecked that parses to zero rows and looks exactly like an empty
        board, so it is detected and retried rather than silently believed.
        """
        last = None
        for attempt in range(1, attempts + 1):
            body = self._fetch_once()
            if self._complete(body):
                return body
            last = body
            # Almost always this means the owning page has not been rendered in
            # this session, so the server has no query registered for the id.
            log.warning(
                "Partial grid response (%d bytes, attempt %d/%d); loading %s first",
                len(body), attempt, attempts, config.NOTICE_PAGE,
            )
            self._warm()
            time.sleep(attempt)
        raise TruncatedResponse(
            f"ERP kept returning a partial document after {attempts} attempts "
            f"(last was {len(last or '')} bytes)"
        )

    def _warm(self):
        """Render the page that owns the grid so the query is registered."""
        try:
            r = self.s.get(
                f"{TP}/{config.NOTICE_PAGE}",
                headers={"Referer": f"{TP}/TPStudent.jsp"},
                timeout=60,
            )
            log.debug("Warmed %s (%d bytes)", config.NOTICE_PAGE, len(r.text or ""))
        except Exception as exc:
            log.warning("Could not load %s: %s", config.NOTICE_PAGE, exc)

    @staticmethod
    def _complete(body: str) -> bool:
        stripped = (body or "").strip()
        if not stripped.startswith("<?xml") and not stripped.startswith("<rows"):
            return True  # not the XML grid; other checks handle it
        return stripped.endswith("</rows>")

    def _fetch_once(self) -> str:
        params = {
            "action": "fetchData",
            "jqqueryid": config.JQ_QUERY_ID,
            "_search": "false",
            "nd": str(int(time.time() * 1000)),
            "rows": str(config.NOTICE_ROWS),
            "page": "1",
            "sidx": "",
            "sord": "asc",
            "totalrows": str(config.NOTICE_ROWS),
        }
        r = self.s.get(
            f"{TP}/ERPMonitoring.htm",
            params=params,
            headers={
                "Accept": "application/xml, text/xml, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{TP}/{config.NOTICE_PAGE}",
            },
            allow_redirects=False,
            timeout=90,   # the notice board is ~1 MB
        )
        if r.status_code in (301, 302, 303, 307, 308):
            raise SessionExpired(f"Redirected to {r.headers.get('Location', '?')}")
        if r.status_code != 200:
            raise SessionExpired(f"HTTP {r.status_code}")
        body = r.text or ""
        if "login.htm" in body or "<html" in body[:400].lower():
            raise SessionExpired("Got a login page instead of grid data")
        return body

    def fetch_notices(self) -> list[list[str]]:
        """Return notice rows, logging in again only if the session is dead."""
        try:
            body = self._raw_fetch()
        except SessionExpired as exc:
            log.info("Saved session unusable (%s); logging in again", exc)
            self.login()
            body = self._raw_fetch()
        except TruncatedResponse:
            raise
        self._save()
        self.last_attachments = parse_attachments(body)
        return parse_rows(body)


ATTACH_URL = f"{TP}/AdmFilePDF.htm"
# The Download cell is <a onclick='TPNotice("2026-2027","923")'>Download</a>.
# An empty link text means the notice has no file attached.
_TPNOTICE_RE = re.compile(r"""TPNotice\(["']([^"']+)["']\s*,\s*["']([^"']+)["']\)""")
_CELL_RE = re.compile(r"<cell><!\[CDATA\[(.*?)\]\]></cell>", re.S)
_ROW_RE = re.compile(r"<row[^>]*>(.*?)</row>", re.S)


def parse_attachments(body: str) -> dict:
    """Map notice id -> {"year": ..., "id": ...} for notices that have a file.

    Kept separate from parse_rows so the parsed cells - and therefore the
    fingerprints every notice is deduplicated by - stay byte-for-byte identical.
    Folding this into the cells would make all stored notices look new.
    """
    out = {}
    for raw_row in _ROW_RE.findall(body or ""):
        cells = _CELL_RE.findall(raw_row)
        if len(cells) <= 7:
            continue
        download = cells[7]
        m = _TPNOTICE_RE.search(download)
        if not m:
            continue
        if not re.sub(r"<[^>]+>", "", download).strip():
            continue  # link present but no text => no file
        notice_id = _clean(cells[0])
        if notice_id:
            out[notice_id] = {"year": m.group(1), "id": m.group(2)}
    return out


_JNF_ARGS_RE = re.compile(r"""TPJNFView\(\s*["'](\d+)["']\s*,\s*["'](\d+)["']\s*,\s*["']([^"']+)["']""")
# <a onclick="ApplyCV2('2026-2027', '<ROLL>', '260', '1')">Apply with CV2</a>
_APPLY_BTN_RE = re.compile(
    r"""onclick=["']Apply(CV\d+)\(\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)""")


def parse_companies(body: str) -> list[dict]:
    """Grid 37 -> one dict per (company, job profile).

    Columns: 0 company, 3 profile, 5 CTC, 6 currency, 8 applied flag ('Y' once
    you have applied), 9/10 the CV-upload window. The profile's ids come from
    the TPJNFView("jnf","com","year") call in its link.
    """
    out = []
    for raw in _ROW_RE.findall(body or ""):
        cells = _CELL_RE.findall(raw)
        if len(cells) < 11:
            continue
        m = _JNF_ARGS_RE.search(cells[3] + " " + cells[4])
        if not m:
            continue
        jnf_id, com_id, year = m.groups()
        out.append({
            "year": year, "com_id": com_id, "jnf_id": jnf_id,
            "company": _clean(cells[0]), "profile": _clean(cells[3]),
            "ctc": _clean(cells[5]), "currency": _clean(cells[6]),
            "applied": _clean(cells[8]).upper() == "Y",
            "window_start": _clean(cells[9]), "window_end": _clean(cells[10]),
        })
    return out


def parse_jnf_page(html: str) -> dict:
    """Read a profile's own page and say what the portal shows THIS student.

    state:
      applied     "Cancel apply" is shown -- you already applied
      eligible    the Apply-with-CV buttons are shown (open, eligible, not applied)
      ineligible  no buttons, and the portal prints a reason instead
      unknown     none of the above
    The portal does the eligibility maths; this only reads its verdict.
    """
    html = html or ""
    out = {"state": "unknown", "reason": "", "form_type": "", "bond": "", "cgpa_cutoff": "",
           "apply": {}}

    b = html.find("<body")
    c = html.find("Company :", b if b >= 0 else 0)
    area = html[b:c] if b >= 0 and c > b else ""
    status_html = area[area.rfind("<table"):] if "<table" in area else area
    status_txt = " ".join(re.sub(r"<[^>]+>", " ", status_html).replace("&nbsp;", " ").split())

    row = re.search(r"<th>\s*Form Type\s*</th>.*?</tr>\s*<tr>(.*?)</tr>", html, re.S)
    if row:
        tds = [re.sub(r"<[^>]+>", "", x).strip() for x in re.findall(r"<td[^>]*>(.*?)</td>", row.group(1), re.S)]
        if tds:
            out["form_type"] = tds[0]
        if len(tds) > 3:
            out["bond"] = tds[3]
        if len(tds) > 5:
            out["cgpa_cutoff"] = tds[5]

    if re.search(r">\s*Cancel apply\s*<", area):
        out["state"] = "applied"
    elif re.search(r"Apply(CV\d+)\(", area):
        out["state"] = "eligible"
        # Which CVs the portal offers, with the exact arguments from its own buttons.
        # js_ok confirms the page's script still builds the request we expect, so we
        # never act on a changed page by guessing.
        for m in _APPLY_BTN_RE.finditer(area):
            cv = int(m.group(1)[2:])
            template = (r'TPJNFViewAction\.jsp\?mode=ApplyCV%d&rollno="\s*\+\s*rollno\s*\+\s*"&year="'
                        r'\s*\+\s*year\s*\+\s*"&com_id="\s*\+\s*com_id\s*\+\s*"&jnf_id="\s*\+\s*jnf_id' % cv)
            out["apply"][cv] = {"year": m.group(2), "rollno": m.group(3),
                                "com_id": m.group(4), "jnf_id": m.group(5),
                                "js_ok": bool(re.search(template, html))}
    elif status_txt:
        out["state"] = "ineligible"
        out["reason"] = status_txt[:300]
    return out


def parse_rows(body: str) -> list[list[str]]:
    """Parse a jqGrid XML (or JSON) payload into a list of cell lists."""
    body = (body or "").strip()
    if not body:
        return []

    if body.startswith("{") or body.startswith("["):
        import json

        data = json.loads(body)
        rows = data.get("rows", data) if isinstance(data, dict) else data
        out = []
        for row in rows or []:
            if isinstance(row, dict):
                cells = row.get("cell", list(row.values()))
            else:
                cells = row
            out.append([_clean(c) for c in cells])
        return out

    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        log.error("Could not parse notice payload: %s; first 300 chars: %r", exc, body[:300])
        return []

    out = []
    for row in root.iter("row"):
        cells = [_clean(c.text) for c in row.findall("cell")]
        if any(cells):
            out.append(cells)
    return out


def _clean(value) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return " ".join(text.split())
