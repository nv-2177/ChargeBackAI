import logging
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import (
    sync_playwright, Page, TimeoutError as PlaywrightTimeout, Error as PlaywrightError,
)

from config.platforms import get_platform_config, resolve_platform_url, UNIVERSAL_NAVIGATION_KEYWORDS
from agents.evidence_utils import match_product, extract_timeline_dates, utc_now

log = logging.getLogger("chargebackai")


class BrowserAgentError(Exception):
    pass


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

    def start(self):
        self._pw = sync_playwright().start()
        args = ["--start-maximized", "--disable-blink-features=AutomationControlled"]
        ctx_kw = dict(no_viewport=True, locale="en-IN", timezone_id="Asia/Kolkata")

        if self.profile_dir:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            try:
                self.context = self._pw.chromium.launch_persistent_context(str(self.profile_dir), headless=self.headless, args=args, **ctx_kw)
            except Exception:
                pass

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

        # SPEED OPTIMIZATION: Block heavy assets (Images, CSS, Fonts, Tracking)
        def _block_resources(route):
            if route.request.resource_type in ["image", "media", "font"]:
                route.abort()
            elif any(track in route.request.url for track in ["analytics", "telemetry", "doubleclick"]):
                route.abort()
            else:
                route.continue_()

        self.page.route("**/*", _block_resources)

    def _ensure_page(self):
        if self.page is not None and not self.page.is_closed():
            return
        open_pages = [p for p in self.context.pages if not p.is_closed()]
        if not open_pages:
            raise BrowserAgentError("Browser window closed.")
        self.page = open_pages[-1]

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

    def login(self, target_url: str, username: str, password: str):
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

    def go_to_orders(self):
        """Direct Jump to Orders page URL instantly."""
        orders_url = self._configured_url("orders_url")
        if orders_url and orders_url not in self.page.url:
            self.page.goto(orders_url, wait_until="commit")
            self._settle(200)

    def search_order(self, order_id: str):
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

    def _download_invoices(self, order_id: str) -> list[dict]:
        c = self.config
        docs = self.evidence_dir / "documents"
        found: list[dict] = []

        trigger = self._first_visible(c.get("invoice_trigger"))
        if trigger:
            try:
                with self.page.expect_download(timeout=2_000) as dl_info:
                    trigger.click()
                out = docs / f"invoice_{order_id}.pdf"
                dl_info.value.save_as(str(out))
                found.append({"file": out.name, "source_url": self.page.url, "type": "direct_download"})
                return found
            except Exception:
                pass

        out = docs / f"order_details_{order_id}.pdf"
        try:
            self.page.pdf(path=str(out), format="A4", print_background=True)
            found.append({"file": out.name, "source_url": self.page.url, "type": "page_pdf"})
        except Exception:
            pass
        return found

    def collect_evidence(self, case_id: str, order_id: str, product_name: str) -> dict:
        d = self.details
        d.update({
            "case_id": case_id,
            "order_id": order_id,
            "product_name_expected": product_name,
            "platform": self.platform,
            "pages": {},
            "invoices": [],
            "collected_at_utc": utc_now(),
        })

        self.search_order(order_id)
        self._open_flipkart_order_details(order_id)
        d["pages"]["order_details"] = self.page.url
        
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass
        self._settle(1000)

        # FIX: Expand Payment Details section if hidden inside dynamic dropdown/accordion
        payment_expanders = [
            "text=/Payment Details/i", "text=/Payment Mode/i",
            "text=/View Payment Details/i", "div:has-text('Payment Information')"
        ]
        expander = self._first_visible(payment_expanders)
        if expander:
            try:
                expander.click()
                self.page.wait_for_timeout(300)
            except Exception:
                pass

        body_text = self.page.locator("body").inner_text()
        d["order_details_text"] = body_text
        
        d["timeline"] = extract_timeline_dates(body_text)
        d["product_verification"] = match_product(product_name, [body_text])

        self.page.screenshot(
            path=str(self.evidence_dir / "screenshots" / "order_details_page.png"),
            type="jpeg",
            quality=50
        )
        (self.evidence_dir / "documents" / "dom_order_details.html").write_text(self.page.content(), encoding="utf-8")

        d["invoices"] = self._download_invoices(order_id)

        # Return / Replacement Status Navigation
        return_selectors = [
            "a:has-text('View Return/Refund Status')", "a:has-text('Return Status')",
            "a:has-text('Refund Status')", "a:has-text('Replacement Status')",
            "div:has-text('Return Status')", "span:has-text('Return Status')"
        ]
        ret_btn = self._first_visible(return_selectors)
        if ret_btn:
            try:
                ret_btn.click()
                self._settle(1000)
                d["pages"]["return_status"] = self.page.url
                ret_text = self.page.locator("body").inner_text()
                ret_timeline = extract_timeline_dates(ret_text)
                for k, v in ret_timeline.items():
                    if v:
                        d["timeline"][k] = v
                self.page.screenshot(
                    path=str(self.evidence_dir / "screenshots" / "return_status_page.png"),
                    type="jpeg",
                    quality=50
                )
            except Exception as e:
                log.warning(f"Return status navigation failed: {e}")

        return d