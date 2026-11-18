import base64
import html as _html
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

from playwright.sync_api import (
    sync_playwright, Page, TimeoutError as PlaywrightTimeout, Error as PlaywrightError,
)

from config.platforms import get_platform_config, resolve_platform_url, UNIVERSAL_NAVIGATION_KEYWORDS
from agents.evidence_utils import match_product, extract_timeline_dates, utc_now
from agents.path_learner import PathNavigator, StepFailure
from agents.browser_pool import get_pool
from agents.fast_utils import Timer, wait_page_ready

log = logging.getLogger("chargebackai")


class BrowserAgentError(Exception):
    pass


_BLOCK_RE = re.compile(
    r"(\.(?:png|jpe?g|gif|webp|avif|svg|ico|bmp|woff2?|ttf|otf|eot|mp4|webm|m4s|mp3)(?:[?#].*)?$)"
    r"|(?:analytics|telemetry|doubleclick|googletagmanager|google-analytics|googlesyndication|amazon-adsystem|"
    r"adsystem|clevertap|moengage|hotjar|gravatar|xp\.atlassian|facebook\.net|connect\.facebook|segment\.io|mixpanel|sentry\.io|"
    r"newrelic|nr-data|/beacon|/pixel|/metrics|fls-eu|fls-na|unagi)", re.I)

_RETURN_LINK_JS = r"""
() => {
  const re = /(return|refund|replacement)[\w\s\/]{0,20}status|track\s+(return|replacement|refund)/i;
  for (const e of document.querySelectorAll('a, button, [role="link"], [role="button"]')) {
    const t = (e.innerText || e.getAttribute('aria-label') || '').trim();
    if (!t || t.length > 80 || !re.test(t)) continue;
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    e.setAttribute('data-cb-return', '1');
    return {href: e.href || '', text: t};
  }
  return null;
}
"""

def _html_to_text(h: str) -> str:
    """HTML -> line-per-block plain text (good enough for the date regexes)."""
    h = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</(?:div|p|li|tr|h\d|td|section)>", "\n", h)
    h = _html.unescape(re.sub(r"<[^>]+>", " ", h))
    lines = (re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in h.splitlines())
    return "\n".join(ln for ln in lines if ln)


_FETCH_MANY_JS = r"""
async (urls) => Promise.all(urls.map(async u => {
  try {
    const r = await fetch(u, {credentials: 'include'});
    if (!r.ok) return {u, ok: false};
    const ct = r.headers.get('content-type') || '';
    const buf = new Uint8Array(await r.arrayBuffer());
    let s = ''; for (let i = 0; i < buf.length; i += 32768) s += String.fromCharCode.apply(null, buf.subarray(i, i + 32768));
    return {u, ok: true, ct, b64: btoa(s)};
  } catch (e) { return {u, ok: false}; }
}))
"""


_RETURN_TEXT_RE = re.compile(r"(return|refund|replacement)[\w\s/]{0,20}status|track\s+(return|replacement|refund)", re.I)
_ANCHOR_RE = re.compile(r"(?is)<a\b[^>]*?href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>")


class PlatformBrowserAgent:
    def __init__(self, platform_name: str, evidence_dir: Path, headless: bool = True, profile_dir: Path | None = None):
        self.platform = str(platform_name).strip().lower()
        try:
            self.config = get_platform_config(platform_name)
        except ValueError as exc:
            raise BrowserAgentError(str(exc)) from exc
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
        self._login_args = ("", "", "")

    def start(self):
        t0 = time.perf_counter()
        self.pool = get_pool()
        self.context = self.pool.context(self.profile_dir, self.headless)   # already running after the first case
        self.browser = None
        self.page = self.pool.page(self.context)                           # new tab: milliseconds

        # SPEED OPTIMIZATION: block heavy assets (images, media, fonts, tracking)
        # Only requests that MATCH this pattern reach Python (and get aborted); everything else goes straight to the
        # network. The old catch-all "**/*" handler made a Python round trip for every one of hundreds of requests.
        self.page.route(_BLOCK_RE, lambda route: route.abort())
        self.details.setdefault("timings_ms", {})["browser_start"] = int((time.perf_counter() - t0) * 1000)

    def _ensure_page(self):
        if self.page is not None and not self.page.is_closed():
            return
        open_pages = [p for p in self.context.pages if not p.is_closed()]
        if not open_pages:
            raise BrowserAgentError("Browser window closed.")
        self.page = open_pages[-1]

    def close(self):
        """Hand the tab back; the browser itself stays running for the next case (see browser_pool)."""
        pool = getattr(self, "pool", None)
        if pool is not None and self.page is not None:
            pool.release(self.context, self.page)

    def _as_list(self, v) -> list[str]:
        return [v] if isinstance(v, str) else list(v) if v else []

    def _first_visible(self, selectors):
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
            self._ensure_page()
            el = self._first_visible(selectors)
            if el:
                return el
            self.page.wait_for_timeout(100)
        return None

    def _settle(self, extra_ms: int = 200):
        self._ensure_page()
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=2_000)
        except PlaywrightTimeout:
            pass
        self.page.wait_for_timeout(extra_ms)

    def _configured_url(self, setting: str, **values) -> str:
        return resolve_platform_url(self.config, setting, self.platform_url, **values)

    # ------------------------------------------------------------ Direct Routing Logic
    def _is_logged_in(self) -> bool:
        """Directly hits the Orders page to verify active session without landing on Home/Account page."""
        try:
            target_orders_url = self._configured_url("orders_url")
            self.page.goto(target_orders_url, wait_until="commit")
            self._settle(300)
            
            # Check if redirected to sign-in page
            for marker in self.config.get("reauth_markers", ["/signin", "/login"]):
                if marker in self.page.url.lower():
                    return False
            return True
        except Exception:
            return False

    def _legacy_login(self, target_url: str, username: str, password: str):
        base_url = str(target_url or self.config.get("base_url", "")).strip()
        self.platform_url = base_url

        # Check session directly on Orders page
        if self._is_logged_in():
            log.info(f"[{self.platform}] Active session found! Skipped Home & Account pages.")
            return

        log.info(f"[{self.platform}] Session expired. Performing authentication...")
        if self.config.get("login_flow") == "otp":
            self._login_otp(base_url, username)
        else:
            self._login_password(base_url, username, password)

    def _login_password(self, base_url: str, username: str, password: str):
        c = self.config
        self.page.goto(base_url, wait_until="commit")
        self._settle(300)

        nav = self._first_visible(c.get("signin_nav"))
        if nav:
            nav.click()

        email = self._wait_visible(c.get("email_input"))
        if email:
            email.fill(username)
            btn = self._first_visible(c.get("continue_btn"))
            if btn:
                btn.click()
            else:
                email.press("Enter")
            self._settle(400)

        pw = self._wait_visible(c.get("password_input"))
        if pw:
            pw.fill(password)
            sub = self._first_visible(c.get("submit_btn"))
            if sub:
                sub.click()
            else:
                pw.press("Enter")
            self._settle(800)

    def _login_otp(self, base_url: str, username: str):
        c = self.config
        login_url = self._configured_url("login_url") if c.get("login_url") else base_url
        self.page.goto(login_url, wait_until="commit")
        self._settle(500)

        email_mode = self._wait_visible(c.get("email_mode_link"), timeout_ms=2_000)
        if email_mode:
            email_mode.click()
            self.page.wait_for_timeout(200)

        field = self._wait_visible(c.get("email_input"), timeout_ms=3_000)
        if field:
            field.click()
            field.fill(username)
            btn = self._first_visible(c.get("continue_btn"))
            if btn:
                btn.click()
            
            print(f"\n[OTP REQUIRED] Enter OTP for {username} in browser window and press ENTER.")
            input(">>> Press ENTER after logging in... ")

    def _legacy_go_to_orders(self):
        """Direct Jump to Orders page URL instantly."""
        orders_url = self._configured_url("orders_url")
        if orders_url and orders_url not in self.page.url:
            self.page.goto(orders_url, wait_until="commit")
            self._settle(200)

    def _legacy_search_order(self, order_id: str):
        c = self.config
        # Direct Jump to specific order ID page
        if c.get("direct_details_url"):
            direct = self._configured_url("direct_details_url", order_id=order_id)
            self.page.goto(direct, wait_until="commit")
            self._settle(300)
            return

        if c.get("direct_order_url"):
            direct = self._configured_url("direct_order_url", order_id=order_id)
            self.page.goto(direct, wait_until="commit")
            self._settle(300)
            return

        box = self.page.locator(", ".join(c.get("search_input", []))).first
        if box.count() and box.is_visible():
            box.fill(order_id)
            btn = self._first_visible(c.get("search_btn"))
            if btn:
                btn.click()
            else:
                box.press("Enter")
            self._settle(400)

    # ------------------------------------------------------------ learned path (primary flow)
    def login(self, target_url: str, username: str, password: str):
        """Sign-in is deferred: the learned path signs in only if the saved session is not valid."""
        base_url = str(target_url or self.config.get("base_url", "")).strip()
        self.platform_url = base_url
        self._login_args = (target_url, username, password)

    def go_to_orders(self):
        """No-op: navigating to the order is part of the learned path (see search_order)."""
        return

    def search_order(self, order_id: str):
        _, username, password = self._login_args
        c = self.config
        origin = urlsplit(self.platform_url)
        if origin.scheme not in ("http", "https") or not origin.netloc:
            raise BrowserAgentError(
                f"No website URL for platform '{self.platform}': it is not in config/platforms.py, so put its "
                "site address (e.g. https://www.example.com) in the Platform_URL column.")
        origin = f"{origin.scheme}://{origin.netloc}"
        hints = [c.get("direct_details_url"), c.get("direct_order_url"), c.get("orders_url")]
        nav = PathNavigator(
            page_fn=lambda: (self._ensure_page() or self.page),
            platform=self.platform, origin=origin, flow="orders",
            target_key="order_id", target_value=order_id,
            start_url=self.platform_url, username=username, password=password, hint_urls=hints,
        )
        self._nav = nav
        try:
            info = nav.open_target()
            self.details["path_mode"] = info["mode"]   # learned:keywords | learned:config_hint | replay:fast_path | replay:steps
            self.details["path_key"] = nav.key
            return
        except StepFailure as e:
            log.warning(f"[{self.platform}] learned-path navigation failed ({e}); using the legacy flow.")
            self.details["path_mode"] = "legacy_fallback"
        # Legacy flow (unchanged from before)
        self._legacy_login(*self._login_args)
        self._legacy_go_to_orders()
        self._legacy_search_order(order_id)

    def _open_flipkart_order_details(self, order_id: str):
        if self.platform != "flipkart" or "/order_details" in self.page.url.lower():
            return

        order_id_pattern = re.compile(
            rf"(?:[?&])order_id={re.escape(order_id)}(?:&|$)",
            re.I,
        )
        for selector in self._as_list(self.config.get("details_link")):
            links = self.page.locator(selector)
            for index in range(min(links.count(), 20)):
                link = links.nth(index)
                href = link.get_attribute("href") or ""
                if order_id_pattern.search(href) and link.is_visible():
                    link.click()
                    self._settle(300)
                    if "/order_details" not in self.page.url.lower():
                        raise BrowserAgentError(
                            f"Flipkart did not open order details for {order_id}."
                        )
                    return

        raise BrowserAgentError(
            f"Could not find Flipkart order details for {order_id}; "
            "payment information is not available on the order summary."
        )

    # ------------------------------------------------------------ opening the order: history first, then shared links
    SHARED_PARAMS = ("order_id", "item_id", "unit_id", "share_token")
    _SHARED_ERROR = re.compile(
        r"(link (has )?expired|invalid link|something went wrong|page not found|no longer available|"
        r"not authori[sz]ed|access denied|unable to load)", re.I)

    def _shared_url_check(self, order_id: str, url: str):
        """(ok, reason). The URL itself is never modified."""
        sp = urlsplit(str(url).strip())
        if sp.scheme not in ("http", "https") or not sp.netloc:
            return False, "not a complete http(s) URL"
        base_host = urlsplit(str(self.config.get("base_url", ""))).netloc.lower()
        root = ".".join(base_host.split(".")[-2:]) if base_host else ""
        if root and not sp.netloc.lower().split(":")[0].endswith(root):
            return False, f"host {sp.netloc} is not a {root} address"
        ids = parse_qs(sp.query).get("order_id") or []
        if not ids or ids[0].strip().lower() != order_id.lower():
            return False, f"order_id in the URL ({ids[0] if ids else 'missing'}) does not match {order_id}"
        return True, ""

    def _on_login_page(self) -> bool:
        url = self.page.url.lower()
        return any(m in url for m in self.config.get("reauth_markers", ["/login"]))

    def _open_shared_order(self, order_id: str, url: str) -> bool:
        ok, why = self._shared_url_check(order_id, url)
        if not ok:
            log.warning(f"[{self.platform}] shared URL rejected: {why}")
            self.details.setdefault("shared_url_errors", []).append(why)
            return False
        url = str(url).strip()

        for attempt in range(2):
            self._ensure_page()
            self.page.goto(url, wait_until="commit")        # the complete URL, untouched
            self._settle(800)
            if not self._on_login_page():
                break
            if attempt == 1:
                self.details.setdefault("shared_url_errors", []).append("sign-in required but did not complete")
                return False
            self._legacy_login(*self._login_args)             # sign in, then reopen the same full URL

        oid = order_id.lower()
        seen, error = False, ""
        end = time.time() + 6
        while time.time() < end:
            try:
                text = self.page.locator("body").inner_text(timeout=1_500)
            except Exception:
                text = ""
            if oid in text.lower():
                seen = True
                break
            m = self._SHARED_ERROR.search(text)
            if m:
                error = m.group(0)
                break
            self.page.wait_for_timeout(300)
        if error:
            self.details.setdefault("shared_url_errors", []).append(f"page shows: {error}")
            return False
        if not seen:
            # Accept only a details-looking page with real content; flag it.
            plausible = "detail" in urlsplit(self.page.url).path.lower() and len(text.strip()) > 300
            if not plausible:
                self.details.setdefault("shared_url_errors", []).append("order id not visible on the opened page")
                return False
            self.details["shared_url_order_id_not_seen"] = True

        q = parse_qs(urlsplit(url).query)
        self.details["shared_url"] = {
            "requested": url,
            "final": self.page.url,
            "params_present": [k for k in self.SHARED_PARAMS if k in q],
        }
        return True

    def _discover_shared_links(self, order_id: str) -> list[str]:
        """Accessible shared-order URLs (order_id + item_id/unit_id/share_token) visible in any open tab:
        the tab URL, its links, and URLs embedded in the page source. Best match first."""
        found: dict[str, int] = {}

        def consider(u: str):
            u = (u or "").replace("&amp;", "&").replace("\\u0026", "&")
            try:
                q = parse_qs(urlsplit(u).query)
            except Exception:
                return
            if (q.get("order_id") or [""])[0].strip().lower() != order_id.lower():
                return
            present = [k for k in ("item_id", "unit_id", "share_token") if k in q]
            if not present or not self._shared_url_check(order_id, u)[0]:
                return
            found[u] = len(present) + (10 if "share_token" in q else 0)

        pattern = re.compile(r"https?://[^\s\"'<>\\]+?order_id=" + re.escape(order_id) + r"[^\s\"'<>\\]*", re.I)
        for pg in [x for x in self.context.pages if not x.is_closed()]:
            consider(pg.url)
            try:
                for h in pg.eval_on_selector_all("a[href]", "els => els.map(e => e.href)"):
                    consider(h)
            except Exception:
                pass
            try:
                html = pg.content().replace("\\u0026", "&").replace("&amp;", "&")
                for m in pattern.finditer(html):
                    consider(m.group(0))
            except Exception:
                pass
        return [u for u, _ in sorted(found.items(), key=lambda kv: -kv[1])][:3]

    def _open_order(self, order_id: str, order_url: str = "") -> str:
        """Returns how the order page was reached: shared_url | order_history | shared_url_discovered."""
        shared_ok = self.platform == "flipkart"
        if order_url and shared_ok:
            if self._open_shared_order(order_id, order_url):
                return "shared_url"
            log.warning(f"[{self.platform}] the supplied Order_URL did not open; trying order history.")
        elif order_url:
            log.info(f"[{self.platform}] Order_URL is only used for Flipkart; ignoring it.")

        history_err = None
        try:
            self.search_order(order_id)
            self._open_flipkart_order_details(order_id)
            return "order_history"
        except Exception as e:
            history_err = e
            log.warning(f"[{self.platform}] order history flow failed: {e}")

        if shared_ok:
            for u in self._discover_shared_links(order_id):
                log.info(f"[{self.platform}] trying discovered shared link: {u}")
                if self._open_shared_order(order_id, u):
                    return "shared_url_discovered"
            msg = (f"{history_err} | No accessible shared order link was found for {order_id}"
                   + (f" ({'; '.join(self.details.get('shared_url_errors', []))})" if self.details.get("shared_url_errors") else "")
                   + ". Add the complete shared link (order_id, item_id, unit_id, share_token) in an 'Order_URL' column.")
            raise BrowserAgentError(msg) from history_err
        raise history_err

    def _download_invoices(self, order_id: str) -> list[dict]:
        c = self.config
        docs = self.evidence_dir / "documents"
        found: list[dict] = []
        pattern = c.get("invoice_href_regex") or r"invoice"
        oid = order_id.lower()

        # 1) Invoice links that are plain URLs: fetch with the signed-in session (no click, no waiting)
        try:
            links = self.page.eval_on_selector_all(
                "a[href]", "els => els.map(e => ({href: e.href, text: (e.innerText || '').trim()}))")
        except Exception:
            links = []
        cands: list = []
        for link in links:
            href = link["href"]
            if href.startswith("http") and href not in cands and re.search(pattern, href, re.I):
                cands.append(href)
        cands = cands[:3]

        def _accept(href, ctype, body):
            ctype = (ctype or "").lower()
            if "pdf" in ctype:
                ext = ".pdf"
            elif "html" in ctype and oid in body.decode("utf-8", "ignore").lower():
                ext = ".html"          # e.g. a printable order summary that carries this order id
            else:
                return None
            name = f"invoice_{order_id}{'' if not found else '_' + str(len(found) + 1)}{ext}"
            (docs / name).write_bytes(body)
            found.append({"file": name, "source_url": href, "type": "direct_fetch", "content_type": ctype})
            return ext

        if cands:
            results, failed = [], list(cands)
            try:       # all candidates in parallel inside the page (same session cookies), one round trip
                results = self.page.evaluate(_FETCH_MANY_JS, cands)
                failed = [r["u"] for r in results if not r.get("ok")]
            except Exception:
                results = []
            ok = [r for r in results if r.get("ok")]
            ok.sort(key=lambda r: "pdf" not in (r.get("ct") or "").lower())      # PDFs first
            for r in ok:
                try:
                    if _accept(r["u"], r.get("ct"), base64.b64decode(r["b64"])) == ".pdf":
                        return found
                except Exception:
                    continue
            for href in failed:      # e.g. cross-origin (CORS) links: use the browser context's own request client
                try:
                    resp = self.context.request.get(href, timeout=4_000)
                    if resp.ok and _accept(href, resp.headers.get("content-type"), resp.body()) == ".pdf":
                        return found
                except Exception:
                    continue
        if found:
            return found

        # 2) Click-to-download. Short wait: most "Invoice" buttons only open a menu, and waiting 2 s each time is slow.
        trigger = self._first_visible(c.get("invoice_trigger"))
        if trigger:
            try:
                with self.page.expect_download(timeout=700) as dl_info:
                    trigger.click()
                out = docs / f"invoice_{order_id}.pdf"
                dl_info.value.save_as(str(out))
                found.append({"file": out.name, "source_url": self.page.url, "type": "direct_download"})
                return found
            except Exception:
                pass

        # 3) Page-as-PDF only works in headless Chromium
        if self.headless and os.environ.get("CB_PAGE_PDF", "1") != "0":     # CB_PAGE_PDF=0 skips this slow fallback
            out = docs / f"order_details_{order_id}.pdf"
            try:
                self.page.pdf(path=str(out), format="A4", print_background=True)
                found.append({"file": out.name, "source_url": self.page.url, "type": "page_pdf"})
            except Exception:
                pass
        return found

    # ------------------------------------------------------------ HTTP-first evidence (no order-page load)
    def _fast_http_eligible(self) -> bool:
        c = self.config
        return bool(c.get("fast_http_order") and c.get("direct_details_url")
                    and str(self.platform_url).startswith("http") and os.environ.get("CB_ORDER_LIVE", "0") != "1")

    def _http_page(self, url: str, timeout: int = 6_000) -> str:
        """Body of an authenticated GET, or '' when it is not usable (error, sign-in redirect, captcha)."""
        try:
            resp = self.context.request.get(url, timeout=timeout)
        except Exception as e:
            log.debug(f"HTTP GET failed {url}: {e}")
            return ""
        if not resp.ok or any(m in (resp.url or url) for m in (self.config.get("reauth_markers") or [])):
            return ""
        body = resp.text()
        if re.search(r"validateCaptcha|Enter the characters you see", body, re.I):
            return ""
        return body

    def _render_order_summary(self, d: dict, text: str) -> str:
        e = _html.escape
        tl = d.get("timeline") or {}
        pv = d.get("product_verification") or {}
        rows = [("Order ID", d.get("order_id")), ("Expected product", d.get("product_name_expected")),
                ("Matched on page", f"{pv.get('matched')} (score {pv.get('score')})"),
                ("Order date", tl.get("order_date")), ("Price paid", tl.get("price")),
                ("Payment method", tl.get("payment_method")), ("Delivery pincode", tl.get("pincode")),
                ("Replacement requested", tl.get("replacement_initialization")),
                ("Replacement delivered", tl.get("replacement_date")), ("Return window", tl.get("replacement_window"))]
        trs = "".join(f"<tr><th>{e(k)}</th><td>{e(str(v or '-'))}</td></tr>" for k, v in rows)
        inv = "".join(f"<div>invoice: {e(i['file'])}</div>" for i in d.get("invoices") or []) or "<div>invoice: none found</div>"
        return f"""<!doctype html><html><head><meta charset="utf-8"><title>Order {e(str(d.get('order_id')))}</title>
<style>body{{font:14px/1.45 system-ui,Segoe UI,Arial,sans-serif;margin:18px;color:#1f2933}}
.banner{{background:#fff8e1;border:1px solid #f0c36d;padding:8px 10px;border-radius:6px;font-size:12px;margin-bottom:12px}}
h1{{font-size:18px;margin:0 0 8px}}th{{text-align:left;padding:2px 14px 2px 0;color:#52606d;font-weight:600}}td{{padding:2px 0}}
pre{{white-space:pre-wrap;font:12px/1.4 ui-monospace,Consolas,monospace;background:#f5f7fa;padding:8px;border-radius:6px;max-height:260px;overflow:hidden}}</style></head><body>
<div class="banner"><b>Evidence summary built from the order page as served by {e(self.platform.title())}</b> (authenticated HTTP request at
{e(d['collected_at_utc'])}) &mdash; not a screenshot of the live page. The page source exactly as received is saved in
documents/dom_order_details.html (SHA-256 in metadata.json).</div>
<h1>Order {e(str(d.get('order_id')))}</h1><table>{trs}</table>{inv}
<div style="margin-top:10px"><b>Page text (excerpt)</b></div><pre>{e(text[:2500])}</pre></body></html>"""

    def _collect_via_http(self, order_id: str, product_name: str, tm, d: dict) -> bool:
        """True when the evidence was built entirely over HTTP. False (nothing half-done) -> use the live page."""
        docs = self.evidence_dir / "documents"
        url = resolve_platform_url(self.config, "direct_details_url", self.platform_url, order_id=order_id)
        with tm.phase("http_order"):
            body = self._http_page(url)
            if not body:
                return False
            text = _html_to_text(body)
            if order_id not in text and order_id not in body:
                return False
            timeline = extract_timeline_dates(text)
            printed = ""
            need = [k for k in ("order_date", "price", "payment_method", "pincode") if not timeline.get(k)]
            if need and self.config.get("print_summary_url"):
                # the printable order summary carries payment / address / totals as plain server-rendered HTML
                purl = resolve_platform_url(self.config, "print_summary_url", self.platform_url, order_id=order_id)
                pbody = self._http_page(purl)
                if pbody and order_id in pbody:
                    printed = pbody
                    for k, v in extract_timeline_dates(_html_to_text(pbody)).items():
                        if k in need and v:
                            timeline[k] = v
            if not (timeline.get("price") and timeline.get("order_date")):
                return False                       # incomplete: never ship half a record, use the live page instead
            (docs / "dom_order_details.html").write_text(body, encoding="utf-8")
            if printed:
                (docs / "order_summary_print.html").write_text(printed, encoding="utf-8")
            d["order_details_text"] = text
            d["timeline"] = timeline
            d["product_verification"] = match_product(product_name, [text])
            d["pages"]["order_details"] = d["order_details_url_full"] = url

        with tm.phase("return_status"):
            raw = _html.unescape(body)
            rhref = ""
            for href, inner in _ANCHOR_RE.findall(raw):
                if _RETURN_TEXT_RE.search(re.sub(r"<[^>]+>", " ", inner)) and len(inner) < 400:
                    rhref = href
                    break
            if rhref and not (timeline.get("replacement_initialization") and timeline.get("replacement_date")):
                rurl = urljoin(url, rhref)
                rbody = self._http_page(rurl, 3_000)
                if rbody:
                    (docs / "dom_return_details.html").write_text(rbody, encoding="utf-8")
                    rtext = _html_to_text(rbody)
                    if re.search(r"return|replacement|refund", rtext, re.I):
                        for k, v in extract_timeline_dates(rtext).items():
                            if k in ("replacement_initialization", "replacement_date", "replacement_window") and v:
                                timeline[k] = v
                        d["pages"]["return_status"] = rurl

        with tm.phase("invoices"):
            pattern = self.config.get("invoice_href_regex") or r"invoice"
            cands = []
            for href, _inner in _ANCHOR_RE.findall(raw):
                if re.search(pattern, href, re.I):
                    cands.append(urljoin(url, href))
            cands += [urljoin(url, m) for m in re.findall(r"/documents/download/[^\"'\s<>\\]+", raw)]
            seen, found = set(), []
            for href in [c for c in cands if not (c in seen or seen.add(c))][:3]:
                try:
                    r = self.context.request.get(href, timeout=4_000)
                    ct = (r.headers.get("content-type") or "").lower()
                    if not r.ok:
                        continue
                    data = r.body()
                except Exception:
                    continue
                if "pdf" in ct:
                    ext = ".pdf"
                elif "html" in ct and order_id.lower() in data.decode("utf-8", "ignore").lower():
                    ext = ".html"
                else:
                    continue
                name = f"invoice_{order_id}{'' if not found else '_' + str(len(found) + 1)}{ext}"
                (docs / name).write_bytes(data)
                found.append({"file": name, "source_url": href, "type": "direct_fetch", "content_type": ct})
                if ext == ".pdf":
                    break
            d["invoices"] = found

        d["order_source"] = "http_fast"
        d["evidence_mode"] = "http_summary"
        with tm.phase("evidence_files"):
            summary = self._render_order_summary(d, text)
            (docs / "order_summary.html").write_text(summary, encoding="utf-8")
            self._ensure_page()
            self.page.set_content(summary, wait_until="domcontentloaded")
            self.page.screenshot(path=str(self.evidence_dir / "screenshots" / "order_summary_page.jpg"),
                                 type="jpeg", quality=65, full_page=False)
        d["page_ready"] = True
        return True

    def collect_evidence(self, case_id: str, order_id: str, product_name: str, order_url: str = "") -> dict:
        d = self.details
        tm = Timer()
        d.update({
            "case_id": case_id,
            "order_id": order_id,
            "product_name_expected": product_name,
            "platform": self.platform,
            "pages": {},
            "invoices": [],
            "collected_at_utc": utc_now(),
        })

        # FAST PATH: the saved session already authenticates plain HTTP requests; no page needs to be opened.
        if self._fast_http_eligible():
            try:
                if self._collect_via_http(order_id, product_name, tm, d):
                    d.setdefault("timings_ms", {}).update(tm.marks)
                    d["timings_ms"]["collect_total"] = tm.total_ms()
                    return d
            except Exception as e:
                log.warning(f"[{self.platform}] HTTP-first evidence failed ({e}); using the live page.")
            else:
                log.info(f"[{self.platform}] HTTP-first not usable (not signed in / page incomplete); using the live page.")
            d.update({"pages": {}, "invoices": []})
        d["evidence_mode"] = "live_page"

        with tm.phase("open_order"):
            d["order_source"] = self._open_order(order_id, order_url)
        d["pages"]["order_details"] = self.page.url
        d["order_details_url_full"] = self.page.url   # complete URL, incl. item_id / unit_id / share_token

        # Wait for the page to be rendered (replaces fixed sleeps); evidence is only captured after this.
        with tm.phase("page_ready"):
            texts = [order_id, "order details", "order summary"]
            d["page_ready"] = wait_page_ready(self.page, any_of_texts=texts, timeout_ms=2_000)
            nav = getattr(self, "_nav", None)
            if nav is not None and d["order_source"] == "order_history":
                try:
                    if nav.recheck():        # the quick jump was bounced to sign-in: signed in, reopened, wait again
                        d["pages"]["order_details"] = d["order_details_url_full"] = self.page.url
                        d["page_ready"] = wait_page_ready(self.page, any_of_texts=texts, timeout_ms=2_000)
                except Exception as e:
                    log.warning(f"[{self.platform}] session re-check failed: {e}")
            d["page_ready_diag"] = dict(getattr(wait_page_ready, "last", {}) or {})
            if not d["page_ready"]:
                log.warning(f"[{self.platform}] order page not ready after 2 s ({d['page_ready_diag']}); capturing anyway.")

        with tm.phase("read_page"):
            # Expand Payment Details if it is a collapsed section (text selectors only: fast)
            expander = self._first_visible([
                "text=/Payment Details/i", "text=/Payment Mode/i",
                "text=/View Payment Details/i", "text=/Payment Information/i",
            ])
            if expander:
                try:
                    expander.click()
                    self.page.wait_for_timeout(150)
                except Exception:
                    pass
            body_text = self.page.locator("body").inner_text()
            d["order_details_text"] = body_text
            d["timeline"] = extract_timeline_dates(body_text)
            d["product_verification"] = match_product(product_name, [body_text])

        with tm.phase("evidence_files"):
            self.page.screenshot(
                path=str(self.evidence_dir / "screenshots" / "order_details_page.png"),
                type="jpeg", quality=60, scale="css")
            (self.evidence_dir / "documents" / "dom_order_details.html").write_text(
                self.page.content(), encoding="utf-8")

        with tm.phase("invoices"):
            d["invoices"] = self._download_invoices(order_id)

        # Return / Replacement Status Navigation
        with tm.phase("return_status"):
            try:
                ret = self.page.evaluate(_RETURN_LINK_JS)           # one call; None when the order has no return link
            except Exception:
                ret = None
            tl = d.get("timeline") or {}
            if ret and tl.get("replacement_initialization") and tl.get("replacement_date"):
                d["return_status_skipped"] = "dates already on the order page"
                ret = None
            if ret and os.environ.get("CB_FAST_RETURN", "1") != "0" and (ret.get("href") or "").startswith("http"):
                # No navigation: authenticated GET with the browser's own cookies (~150-300 ms instead of ~2-3 s).
                # The raw HTML is kept as the evidence copy; dates come from its text. CB_FAST_RETURN=0 = old page visit.
                try:
                    rurl = urljoin(self.page.url, ret["href"])
                    resp = self.context.request.get(rurl, timeout=3_000)
                    if resp.ok:
                        body = resp.text()
                        (self.evidence_dir / "documents" / "dom_return_details.html").write_text(body, encoding="utf-8")
                        rtext = _html_to_text(body)
                        if re.search(r"return|replacement|refund", rtext, re.I):     # a real status page, not a login/error page
                            for k, v in extract_timeline_dates(rtext).items():
                                if k in ("replacement_initialization", "replacement_date", "replacement_window") and v:
                                    d["timeline"][k] = v
                            d["pages"]["return_status"] = rurl
                            d["return_status_mode"] = "http_get (HTML saved, no screenshot)"
                            ret = None
                except Exception as e:
                    log.warning(f"Return-status GET failed, opening the page instead: {e}")
            if ret:
                try:
                    href = ret.get("href") or ""
                    if href.startswith("http"):
                        self.page.goto(href, wait_until="domcontentloaded")
                    else:
                        self.page.click('[data-cb-return="1"]')
                    wait_page_ready(self.page, any_of_texts=["return", "refund", "replacement"], timeout_ms=1_500)
                    d["pages"]["return_status"] = self.page.url
                    ret_text = self.page.locator("body").inner_text()
                    ret_timeline = extract_timeline_dates(ret_text)
                    for k in ("replacement_initialization", "replacement_date", "replacement_window"):
                        if ret_timeline.get(k):      # never let this page overwrite price / order_date / payment / pincode
                            d["timeline"][k] = ret_timeline[k]
                    self.page.screenshot(
                        path=str(self.evidence_dir / "screenshots" / "return_status_page.png"),
                        type="jpeg", quality=60, scale="css")
                except Exception as e:
                    log.warning(f"Return status navigation failed: {e}")

        d.setdefault("timings_ms", {}).update(tm.marks)
        d["timings_ms"]["collect_total"] = tm.total_ms()
        return d