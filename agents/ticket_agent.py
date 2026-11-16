# agents/ticket_agent.py
import logging
import re
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import (
    sync_playwright, Page, TimeoutError as PlaywrightTimeout, Error as PlaywrightError,
)

from config.platforms import get_platform_config, resolve_platform_url
from agents.path_learner import PathNavigator, StepFailure
from agents.browser_pool import get_pool
from agents.fast_utils import Timer, wait_page_ready
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
        self._creds = ("", "")
        self._start_url = ""
        self._filters_on = False
        self._warm_key = None
        self._is_warm = False
        self._soft_used = False
        self._req_log: list = []

    # ------------------------------------------------------------ lifecycle
    def start(self):
        t0 = time.perf_counter()
        self.pool = get_pool()
        self.context = self.pool.context(self.profile_dir, self.headless)   # already running after the first case
        self.browser = None
        self.page = self.pool.page(self.context)                           # new tab: milliseconds
        self._req_log = self.pool.req_log(self.page)
        self._install_filters()
        self.details.setdefault("timings_ms", {})["browser_start"] = int((time.perf_counter() - t0) * 1000)

    def _allowed_hosts(self) -> list:
        hosts = list(self.config.get("allow_hosts") or [])
        own = urlsplit(self.platform_url).hostname if self.platform_url else ""
        if own:
            hosts.append(own)
        return hosts

    def _install_filters(self):
        """Speed filters: always block media/fonts; with block_third_party also block every request to a host
        that is not the account's own or in allow_hosts (analytics, tag managers, chat widgets...).
        Removed while signing in (2FA/captcha pages stay untouched) and re-installed afterwards by the
        PathNavigator login hooks. Images are NOT blocked: screenshots show inline images."""
        block_3p = bool(self.config.get("block_third_party"))

        def _block(route):
            req = route.request
            if req.resource_type in ("media", "font"):
                route.abort()
                return
            if block_3p and req.resource_type != "document" and not req.url.startswith(("data:", "blob:", "about:")):
                host = urlsplit(req.url).hostname or ""
                allowed = self._allowed_hosts()
                if allowed and not any(host == h or host.endswith("." + h) for h in allowed):
                    route.abort()
                    return
            route.continue_()

        self.page.route("**/*", _block)
        self._filters_on = True

    def _remove_filters(self):
        if self._filters_on:
            try:
                self.page.unroute("**/*")
            except Exception:
                pass
            self._filters_on = False

    def close(self):
        """Hand the tab back; the browser stays running. After a good run on a warm_tab platform the tab is kept
        (app still loaded) so the next ticket does not pay the app's start-up cost again."""
        pool = getattr(self, "pool", None)
        if pool is None or self.page is None:
            return
        keep = False
        try:
            keep = bool(self.config.get("warm_tab") and self._warm_key and self.details.get("page_ready")
                        and self.details.get("ticket_details") and not self.page.is_closed())
        except Exception:
            pass
        if keep:
            pool.keep_warm(self._warm_key, self.page)
        else:
            pool.release(self.context, self.page)

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
        if not url:
            return None
        items, pages = [], 0
        while url and pages < max_pages:
            data = self._api_json(url)
            if data is None:
                return items if pages else None
            items.extend(data.get(key, []))
            url = data.get("next_page") or ""
            pages += 1
        return items

    # ------------------------------------------------------------ session / navigation (learned paths)
    def _session_ok(self) -> bool:
        """API check, used only to learn the account role (agent vs end-user)."""
        me_url = self._url("api_me_url")
        if not me_url:
            return False
        user = (self._api_json(me_url, quiet=True) or {}).get("user") or {}
        if user.get("id"):
            self.user_role = str(user.get("role") or "")
            return True
        return False

    def login(self, target_url: str, username: str, password: str):
        """Sign-in is deferred: the learned path signs in only if the saved session is not valid."""
        self.platform_url = _origin(target_url)
        if not self.platform_url:
            raise TicketAgentError(
                "Platform_URL must be the full URL with subdomain, e.g. https://acme.zendesk.com")
        self._start_url = str(target_url).strip()
        self._creds = (username, password)
        if self.config.get("warm_tab") and getattr(self, "pool", None) is not None:
            self._warm_key = f"{id(self.context)}|{self.platform_url}"
            warm = self.pool.take_warm(self._warm_key)
            if warm is not None:
                if self.page is not None and self.page is not warm:
                    self.pool.release(self.context, self.page)   # the blank tab start() opened
                self.page = warm
                self._req_log = self.pool.req_log(warm)
                self._filters_on = True                          # the filters are still registered on this tab
                self._is_warm = True

    def go_to_ticket(self, ticket_id: str, soft: bool = False):
        ticket_id = str(ticket_id).strip().lstrip("#")
        hints = [self.config.get("direct_ticket_url"), self.config.get("direct_ticket_url_enduser")]
        nav = PathNavigator(
            page_fn=lambda: (self._ensure_page() or self.page),
            platform=self.platform, origin=self.platform_url, flow="tickets",
            target_key="ticket_id", target_value=ticket_id,
            start_url=self._start_url, username=self._creds[0], password=self._creds[1],
            hint_urls=hints,
            on_login_start=self._remove_filters, on_login_end=self._install_filters,
            soft_nav=bool(soft and self._is_warm),
        )
        self._nav = nav
        try:
            info = nav.open_target()
        except StepFailure as e:
            raise TicketAgentError(f"Could not reach ticket {ticket_id}: {e}") from e
        self._soft_used = bool(nav.soft_navigated)
        self.details["path_mode"] = info["mode"]      # learned:keywords | learned:config_hint | replay:fast_path | replay:steps
        self.details["path_key"] = nav.key

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

    @staticmethod
    def _anchor_text(comments) -> str:
        """A distinctive line from this ticket's own first comment (not a greeting): lets us recognise the
        right ticket on screen. Empty if the ticket has no usable text (then no in-app jump is attempted)."""
        for c in comments or []:
            lines = []
            for line in re.split(r"[\r\n]+", c.get("plain_body") or ""):
                line = re.sub(r"\s+", " ", line).strip()
                if line:
                    lines.append(line)
            best = max(lines[:8], key=len, default="")
            if len(best) >= 15:
                return best[:60]
        return ""

    def _api_ticket_bundle(self, ticket_id: str):
        """Ticket JSON + comments. Agent API first (no extra 'who am I' call); end-user API as fallback."""
        api_ticket = self._api_json(self._url("api_ticket_url", ticket_id=ticket_id), quiet=True)
        if api_ticket is not None:
            self.user_role = self.user_role or "agent"
        elif self.config.get("api_ticket_url_enduser"):
            api_ticket = self._api_json(self._url("api_ticket_url_enduser", ticket_id=ticket_id), quiet=True)
            if api_ticket is not None:
                self.user_role = "end-user"
        c_setting = "api_comments_url_enduser" if self.user_role == "end-user" else "api_comments_url"
        comments = self._api_paginated(self._url(c_setting, ticket_id=ticket_id), "comments")
        return api_ticket, comments

    def collect_ticket_evidence(self, case_id: str, ticket_id: str, subject_query: str) -> dict:
        ticket_id = str(ticket_id).strip().lstrip("#")
        tm = Timer()
        d = self.details
        d.update({
            "case_id": case_id,
            "ticket_id": ticket_id,
            "expected_subject": subject_query,
            "platform": self.platform,
            "pages": {},
            "attachments": [],
            "collected_at_utc": utc_now(),
        })
        docs = self.evidence_dir / "documents"

        del self._req_log[:]

        def load_api():
            with tm.phase("api"):
                at, cm = self._api_ticket_bundle(ticket_id)
                if at is not None:
                    write_json(docs / f"api_ticket_{ticket_id}.json", at)
                if cm is not None:
                    write_json(docs / f"api_comments_{ticket_id}.json", {"comments": cm})
            return at, cm

        # Warm tab: fetch the data FIRST, so the new ticket can be recognised on screen by its own text.
        # Cold tab: open the page first; it renders while the data requests run.
        api_ticket, comments, anchor = None, None, ""
        if self._is_warm:
            api_ticket, comments = load_api()
            anchor = self._anchor_text(comments)
        with tm.phase("open_ticket"):
            self.go_to_ticket(ticket_id, soft=bool(anchor))
        d["pages"]["ticket_details"] = self.page.url
        if not self._is_warm:
            api_ticket, comments = load_api()
            anchor = self._anchor_text(comments)
        d["login_role"] = self.user_role

        # Screenshot / DOM are taken ONLY after the ticket page has really rendered: its own content is on screen
        # (text from this ticket, a timestamp or its id) and the text has stopped changing.
        with tm.phase("page_ready"):
            cold_kw = dict(any_of_selectors=["time[datetime]"],
                           any_of_texts=[x for x in (anchor, ticket_id) if x], timeout_ms=3_500)
            nav = getattr(self, "_nav", None)
            if self._soft_used:
                # strict: the previous ticket's DOM is still on screen, so only this ticket's own text counts
                ready = wait_page_ready(self.page, any_of_texts=[anchor], timeout_ms=2_500)
                if nav is not None:
                    nav.entry["soft_nav"] = "works" if ready else "fails"   # remembered: never retried if it fails
                    nav._save()
                d["soft_nav"] = "works" if ready else "fell_back_to_full_load"
                if not ready:
                    self.page.goto(self.page.url, wait_until="commit")
                    self._soft_used = False
                    ready = wait_page_ready(self.page, **cold_kw)
            else:
                ready = wait_page_ready(self.page, **cold_kw)
            if nav is not None:
                try:
                    if nav.recheck():        # the quick jump was bounced to sign-in: signed in, reopened, wait again
                        d["pages"]["ticket_details"] = self.page.url
                        ready = wait_page_ready(self.page, **cold_kw)
                except Exception as e:
                    log.warning(f"[{ticket_id}] session re-check failed: {e}")
            d["page_ready"] = ready
            d["page_ready_diag"] = dict(getattr(wait_page_ready, "last", {}) or {})
            if not ready:
                log.warning(f"[{ticket_id}] ticket page not ready ({d['page_ready_diag']}); capturing anyway (flagged).")

        with tm.phase("read_dom"):
            body_text = self.page.locator("body").inner_text()
            d["raw_text"] = body_text
            try:
                timestamps = self.page.eval_on_selector_all(
                    "time[datetime]",
                    "els => els.map(e => ({text: (e.innerText||'').trim(), datetime: e.getAttribute('datetime')}))")
            except Exception:
                timestamps = []
            d["dom_timestamps"] = timestamps

        # Evidence artifacts, captured right after the readiness check, before anything else touches the page
        with tm.phase("screenshot"):
            self.page.screenshot(path=str(self.evidence_dir / "screenshots" / "ticket_details_page.png"),
                                 type="jpeg", quality=60, scale="css")
            d["screenshot_taken_after_ready"] = bool(d["page_ready"])
        with tm.phase("dom_dump"):
            (docs / "dom_ticket_details.html").write_text(self.page.content(), encoding="utf-8")
            for n, fr in enumerate(self.page.frames[1:7], start=1):  # iframes (composer, apps, etc.)
                try:
                    (docs / f"dom_frame_{n}.html").write_text(fr.content(), encoding="utf-8")
                except Exception:
                    pass

        t = (api_ticket or {}).get("ticket") or (api_ticket or {}).get("request") or {}
        if not t and re.search(r"ticket not found|does not exist|don.t have (access|permission)", body_text, re.I):
            raise TicketAgentError(f"Ticket {ticket_id} not found or not accessible with this account.")
        if not t and not timestamps and len(body_text.strip()) < 50:
            raise TicketAgentError(f"Ticket page for {ticket_id} did not load any content.")

        # Merge: API values win, DOM fills gaps; record where each came from
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

        # Subject check (recorded; main.py decides whether it fails the case)
        if subject_query:
            candidates = [d["subject_actual"]] if d["subject_actual"] else [body_text]
            d["subject_verification"] = match_product(subject_query, candidates)

        with tm.phase("attachments"):
            d["attachments"] = self._download_attachments(comments)

        # Load diagnostics: where the page's load time went (hosts, slowest requests, browser navigation timing)
        try:
            reqs = list(self._req_log)
            d["load_diag"] = {
                "requests": len(reqs),
                "top_hosts": Counter(r[0] for r in reqs).most_common(8),
                "slowest_ms": [{"ms": r[2], "type": r[1], "url": r[3]}
                               for r in sorted(reqs, key=lambda r: -r[2])[:5]],
            }
            d["nav_timing_ms"] = self.page.evaluate(
                "() => { const n = performance.getEntriesByType('navigation')[0];"
                " return n ? {response_start: Math.round(n.responseStart),"
                " dom_content_loaded: Math.round(n.domContentLoadedEventEnd), load: Math.round(n.loadEventEnd)} : null; }")
        except Exception:
            pass

        d.setdefault("timings_ms", {}).update(tm.marks)
        d["timings_ms"]["collect_total"] = tm.total_ms()
        return d