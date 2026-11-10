# agents/ticket_agent.py
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import (
    sync_playwright, Page, TimeoutError as PlaywrightTimeout, Error as PlaywrightError,
)

from config.platforms import get_platform_config, resolve_platform_url
from agents.evidence_utils import (
    extract_ticket_details, match_product, normalize_iso, utc_now, write_json,
)

log = logging.getLogger("chargebackai")


class TicketAgentError(Exception):
    pass


def _origin(url: str) -> str:
    p = urlsplit(str(url or "").strip())
    return f"{p.scheme}://{p.netloc}" if p.scheme in ("http", "https") and p.netloc else ""


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w.\- ]+", "_", str(name or "file")).strip() or "file"


class TicketBrowserAgent:
    def __init__(self, platform_name: str, evidence_dir: Path, headless: bool = True, profile_dir: Path | None = None):
        self.platform = str(platform_name).strip().lower()
        self.config = get_platform_config(platform_name)
        if self.config.get("type") != "ticketing":
            raise TicketAgentError(
                f"Platform '{platform_name}' is not configured as a ticketing platform in config/platforms.py"
            )
        self.evidence_dir = Path(evidence_dir)
        (self.evidence_dir / "screenshots").mkdir(parents=True, exist_ok=True)
        (self.evidence_dir / "documents").mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.profile_dir = Path(profile_dir) if profile_dir else None
        self._pw = None
        self.browser = None
        self.context = None
        self.page: Page | None = None
        self.details: dict = {}
        self.platform_url = ""
        self.user_role = ""

    # ------------------------------------------------------------ lifecycle
    def start(self):
        self._pw = sync_playwright().start()
        args = ["--start-maximized", "--disable-blink-features=AutomationControlled"]
        ctx_kw = dict(no_viewport=True, locale="en-IN", timezone_id="Asia/Kolkata")

        if self.profile_dir:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            for channel in ("chrome", None):  # installed Chrome first, bundled Chromium as fallback
                try:
                    kw = dict(headless=self.headless, args=args,
                              ignore_default_args=["--enable-automation"], **ctx_kw)
                    if channel:
                        kw["channel"] = channel
                    self.context = self._pw.chromium.launch_persistent_context(str(self.profile_dir), **kw)
                    log.info(f"[{self.platform}] Browser started ({channel or 'bundled chromium'}).")
                    break
                except Exception as e:
                    log.warning(f"[{self.platform}] Launch with channel={channel} failed: {e}")

        if self.context is None:
            try:
                self.browser = self._pw.chromium.launch(channel="chrome", headless=self.headless, args=args)
            except PlaywrightError:
                self.browser = self._pw.chromium.launch(headless=self.headless, args=args)
            self.context = self.browser.new_context(**ctx_kw)
        else:
            self.browser = self.context.browser

        self.context.set_default_timeout(5_000)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

    def _install_filters(self):
        """Block media/fonts, but only AFTER sign-in so 2FA/verification pages are never touched.
        Images and analytics URLs are NOT blocked (screenshots show inline images)."""
        def _block(route):
            if route.request.resource_type in ("media", "font"):
                route.abort()
            else:
                route.continue_()

        self.page.route("**/*", _block)

    def close(self):
        for obj in (self.context, self.browser):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass

    # ------------------------------------------------------------ helpers
    def _ensure_page(self):
        if self.page is not None and not self.page.is_closed():
            return
        open_pages = [p for p in self.context.pages if not p.is_closed()]
        if not open_pages:
            raise TicketAgentError("Browser window closed.")
        self.page = open_pages[-1]

    def _as_list(self, v) -> list[str]:
        return [v] if isinstance(v, str) else list(v) if v else []

    def _first_visible(self, selectors):
        self._ensure_page()
        for sel in self._as_list(selectors):
            try:
                loc = self.page.locator(sel)
                for i in range(min(loc.count(), 5)):
                    if loc.nth(i).is_visible():
                        return loc.nth(i)
            except Exception:
                continue
        return None

    def _wait_visible(self, selectors, timeout_ms: int = 4_000):
        end = time.time() + timeout_ms / 1000
        while time.time() < end:
            el = self._first_visible(selectors)
            if el:
                return el
            self.page.wait_for_timeout(100)
        return None

    def _settle(self, extra_ms: int = 300):
        self._ensure_page()
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=2_000)
        except PlaywrightTimeout:
            pass
        self.page.wait_for_timeout(extra_ms)

    def _url(self, setting: str, **values) -> str:
        """Resolved absolute URL for a config key, or '' if the key isn't configured."""
        if not self.config.get(setting):
            return ""
        return resolve_platform_url(self.config, setting, self.platform_url, **values)

    def _role_setting(self, name: str) -> str:
        if self.user_role == "end-user" and self.config.get(f"{name}_enduser"):
            return f"{name}_enduser"
        return name

    def _api_json(self, url: str, quiet: bool = False):
        if not url:
            return None
        try:
            resp = self.context.request.get(url, headers={"Accept": "application/json"}, timeout=15_000)
        except Exception as e:
            (log.debug if quiet else log.warning)(f"API GET failed {url}: {e}")
            return None
        if not resp.ok:
            (log.debug if quiet else log.warning)(f"API GET {url} -> HTTP {resp.status}")
            return None
        try:
            return resp.json()
        except Exception:
            return None

    def _api_paginated(self, url: str, key: str, max_pages: int = 50) -> list | None:
        items, pages = [], 0
        while url and pages < max_pages:
            data = self._api_json(url)
            if data is None:
                return items if pages else None
            items.extend(data.get(key, []))
            url = data.get("next_page") or ""
            pages += 1
        return items

    # ------------------------------------------------------------ session / login
    def _session_ok(self) -> bool:
        """True if the saved profile already holds a valid session."""
        me_url = self._url("api_me_url")
        if me_url:
            user = (self._api_json(me_url, quiet=True) or {}).get("user") or {}
            if user.get("id"):
                self.user_role = str(user.get("role") or "")
                return True
            return False
        # No API configured: navigate and look for sign-in redirects.
        try:
            self.page.goto(self._url("tickets_url") or self.platform_url, wait_until="commit")
            self._settle(1500)
            url = self.page.url.lower()
            return not any(m in url for m in self.config.get("reauth_markers", []))
        except Exception:
            return False

    def login(self, target_url: str, username: str, password: str):
        self._login(target_url, username, password)
        self._install_filters()

    def _login(self, target_url: str, username: str, password: str):
        self.platform_url = _origin(target_url)
        if not self.platform_url:
            raise TicketAgentError(
                "Platform_URL must be the full URL with subdomain, e.g. https://acme.zendesk.com")

        if self._session_ok():
            log.info(f"[{self.platform}] Active session found, skipping sign-in (role={self.user_role or 'unknown'}).")
            return

        log.info(f"[{self.platform}] No valid session. Signing in...")
        c = self.config
        self.page.goto(self._url("tickets_url") or self.platform_url, wait_until="commit")
        self._settle(1000)

        # "Customer sign in" link on the agent sign-in page, only if present
        cust = self._wait_visible(c.get("customer_signin_link"), 2_000)
        if cust:
            cust.click()
            self._settle(500)

        email = self._wait_visible(c.get("email_input"), 6_000)
        if not email:
            raise TicketAgentError("Sign-in email field not found; check email_input selectors in config/platforms.py")
        email.fill(username)

        pw = self._wait_visible(c.get("password_input"), 1_500)
        if not pw:  # two-step form: email first, then password
            nxt = self._first_visible(c.get("continue_btn"))
            if nxt:
                nxt.click()
            else:
                email.press("Enter")
            self._settle(400)
            pw = self._wait_visible(c.get("password_input"), 5_000)
        if not pw:
            raise TicketAgentError("Sign-in password field not found; check password_input selectors.")
        pw.fill(password)

        sub = self._first_visible(c.get("submit_btn"))
        if sub:
            sub.click()
        else:
            pw.press("Enter")
        self._settle(1500)

        deadline = time.time() + 8
        while time.time() < deadline:
            if self._session_ok():
                return
            self.page.wait_for_timeout(500)

        # Captcha / 2FA / SSO: hand over to the human
        print(f"\n[VERIFICATION REQUIRED] Finish signing in for {username} in the browser window.")
        input(">>> Press ENTER after you are signed in... ")
        if not self._session_ok():
            raise TicketAgentError("Login failed: no valid session after sign-in.")

    # ------------------------------------------------------------ navigation
    def go_to_ticket(self, ticket_id: str):
        url = self._url(self._role_setting("direct_ticket_url"), ticket_id=ticket_id)
        if not url:
            raise TicketAgentError(f"No direct_ticket_url configured for platform '{self.platform}'.")
        self.page.goto(url, wait_until="commit")
        self._settle(500)
        try:  # the agent workspace is a SPA; let its XHRs finish
            self.page.wait_for_load_state("networkidle", timeout=8_000)
        except PlaywrightTimeout:
            pass
        cur = self.page.url.lower()
        if any(m in cur for m in self.config.get("reauth_markers", [])):
            raise TicketAgentError(f"Session lost while opening ticket {ticket_id} (redirected to {self.page.url}).")

    # ------------------------------------------------------------ evidence
    def _download(self, url: str, dest: Path) -> tuple[bool, str]:
        try:
            resp = self.context.request.get(url, timeout=60_000)
            if not resp.ok:
                return False, f"HTTP {resp.status}"
            dest.write_bytes(resp.body())
            return True, ""
        except Exception as e:
            return False, str(e)

    def _download_attachments(self, comments: list | None) -> list[dict]:
        out_dir = self.evidence_dir / "documents" / "attachments"
        out_dir.mkdir(parents=True, exist_ok=True)
        records: list[dict] = []
        seen: set[str] = set()

        def grab(url, name, comment_id="", content_type="", size=None):
            if not url or url in seen:
                return
            seen.add(url)
            fname = f"{comment_id or 'dom'}_{len(records) + 1}_{_safe_name(name)}"
            ok, err = self._download(url, out_dir / fname)
            rec = {"file": f"documents/attachments/{fname}" if ok else None, "original_name": name,
                   "content_type": content_type, "size": size, "comment_id": comment_id, "source_url": url}
            if not ok:
                rec["error"] = err
                log.warning(f"Attachment download failed: {name}: {err}")
            records.append(rec)

        # 1) From the API comments (complete thread, not limited to what's rendered)
        for cmt in comments or []:
            for a in cmt.get("attachments", []) or []:
                grab(a.get("content_url"), a.get("file_name") or f"attachment_{a.get('id', '')}",
                     str(cmt.get("id", "")), a.get("content_type", ""), a.get("size"))

        # 2) Fallback / supplement: attachment links visible in the DOM
        pattern = self.config.get("attachment_href_regex")
        if pattern:
            try:
                hrefs = self.page.eval_on_selector_all(
                    "a[href]", "els => els.map(e => ({href: e.href, text: (e.innerText||'').trim()}))")
            except Exception:
                hrefs = []
            for h in hrefs:
                if re.search(pattern, h["href"]):
                    grab(h["href"], h["text"] or h["href"].rsplit("/", 1)[-1].split("?")[0] or "attachment")
        return records

    def collect_ticket_evidence(self, case_id: str, ticket_id: str, subject_query: str) -> dict:
        ticket_id = str(ticket_id).strip().lstrip("#")
        d = self.details
        d.update({
            "case_id": case_id,
            "ticket_id": ticket_id,
            "expected_subject": subject_query,
            "platform": self.platform,
            "login_role": self.user_role,
            "pages": {},
            "attachments": [],
            "collected_at_utc": utc_now(),
        })
        docs = self.evidence_dir / "documents"

        self.go_to_ticket(ticket_id)
        d["pages"]["ticket_details"] = self.page.url

        # --- structured data via the signed-in session
        api_ticket = self._api_json(self._url(self._role_setting("api_ticket_url"), ticket_id=ticket_id))
        comments = self._api_paginated(
            self._url(self._role_setting("api_comments_url"), ticket_id=ticket_id), "comments")
        if api_ticket is not None:
            write_json(docs / f"api_ticket_{ticket_id}.json", api_ticket)
        if comments is not None:
            write_json(docs / f"api_comments_{ticket_id}.json", {"comments": comments})

        # --- DOM
        body_text = self.page.locator("body").inner_text()
        d["raw_text"] = body_text
        try:
            timestamps = self.page.eval_on_selector_all(
                "time[datetime]",
                "els => els.map(e => ({text: (e.innerText||'').trim(), datetime: e.getAttribute('datetime')}))")
        except Exception:
            timestamps = []
        d["dom_timestamps"] = timestamps

        t = (api_ticket or {}).get("ticket") or (api_ticket or {}).get("request") or {}
        if not t and re.search(r"ticket not found|does not exist|don.t have (access|permission)", body_text, re.I):
            raise TicketAgentError(f"Ticket {ticket_id} not found or not accessible with this account.")
        if not t and not timestamps and len(body_text.strip()) < 50:
            raise TicketAgentError(f"Ticket page for {ticket_id} did not load any content.")

        # --- merge: API values win, DOM fills gaps; record where each came from
        metric = (api_ticket or {}).get("metric_set") or ((api_ticket or {}).get("metric_sets") or [{}])[0] or {}
        api_vals = {
            "status": t.get("status") or "",
            "priority": t.get("priority") or "",
            "created_date": normalize_iso(t.get("created_at")),
            "resolved_date": normalize_iso(metric.get("solved_at")),
        }
        dom_vals = extract_ticket_details(body_text, [x.get("datetime") for x in timestamps])
        merged, sources = {}, {}
        for k in ("status", "priority", "created_date", "resolved_date"):
            if api_vals[k]:
                merged[k], sources[k] = api_vals[k], "api"
            elif dom_vals.get(k):
                merged[k], sources[k] = dom_vals[k], "dom"
            else:
                merged[k], sources[k] = "", "not_found"
        if str(merged["status"]).lower() in ("solved", "closed") and not merged["resolved_date"]:
            log.warning(f"[{ticket_id}] status is {merged['status']} but no solved_at available; resolved_date left blank.")
        d["ticket_details"] = merged
        d["field_sources"] = sources
        d["subject_actual"] = t.get("subject") or ""

        # --- subject check (recorded; main.py decides whether it fails the case)
        if subject_query:
            candidates = [d["subject_actual"]] if d["subject_actual"] else [body_text]
            d["subject_verification"] = match_product(subject_query, candidates)

        # --- evidence artifacts
        self.page.screenshot(path=str(self.evidence_dir / "screenshots" / "ticket_details_page.png"),
                             type="jpeg", quality=60)
        (docs / "dom_ticket_details.html").write_text(self.page.content(), encoding="utf-8")
        for n, fr in enumerate(self.page.frames[1:], start=1):  # iframes (composer, apps, etc.)
            try:
                (docs / f"dom_frame_{n}.html").write_text(fr.content(), encoding="utf-8")
            except Exception:
                pass

        d["attachments"] = self._download_attachments(comments)
        return d