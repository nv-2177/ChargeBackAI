"""
agents/browser_pool.py

One Playwright driver and one browser context per profile, kept alive for the WHOLE run.
- Pending profiles are prewarmed before case processing, moving browser launch time out of case timings.
- Every later case reuses the running browser (a new tab opens in milliseconds).
- With a CDP URL (python main.py --cdp-url http://localhost:9222) the agent attaches to a Chrome that is
  already running, so even the first case has no launch cost.
"""
import logging
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, Error as PlaywrightError

log = logging.getLogger("chargebackai")

LAUNCH_ARGS = [
    "--start-maximized", "--disable-blink-features=AutomationControlled",
    "--no-first-run", "--no-default-browser-check", "--disable-extensions",
    "--disable-background-networking", "--disable-sync", "--disable-component-update",
]
CTX_KW = dict(no_viewport=True, locale="en-IN", timezone_id="Asia/Kolkata")
_BLANK = ("about:blank", "chrome://newtab/", "chrome://new-tab-page/")


class BrowserPool:
    def __init__(self, cdp_url: str = ""):
        self.cdp_url = (cdp_url or "").strip()
        self._pw = None
        self._browser = None
        self._ctx: dict = {}
        self._claimed: set = set()
        self._warm: dict = {}
        self._logs: dict = {}

    def _driver(self):
        if self._pw is None:
            self._pw = sync_playwright().start()
        return self._pw

    def context(self, profile_dir, headless: bool = False):
        key = "cdp" if self.cdp_url else str(profile_dir)
        ctx = self._ctx.get(key)
        if ctx is not None:
            return ctx
        pw = self._driver()
        if self.cdp_url:
            self._browser = self._browser or pw.chromium.connect_over_cdp(self.cdp_url)
            ctx = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context(**CTX_KW)
        else:
            ctx = None
            if profile_dir:
                Path(profile_dir).mkdir(parents=True, exist_ok=True)
                for channel in ("chrome", None):
                    try:
                        kw = dict(headless=headless, args=LAUNCH_ARGS,
                                  ignore_default_args=["--enable-automation"], **CTX_KW)
                        if channel:
                            kw["channel"] = channel
                        ctx = pw.chromium.launch_persistent_context(str(profile_dir), **kw)
                        log.info(f"Browser started ({channel or 'bundled chromium'}) for profile {Path(profile_dir).name}")
                        break
                    except Exception as e:
                        log.warning(f"Launch with channel={channel} failed: {e}")
            if ctx is None:
                try:
                    browser = pw.chromium.launch(channel="chrome", headless=headless, args=LAUNCH_ARGS)
                except PlaywrightError:
                    browser = pw.chromium.launch(headless=headless, args=LAUNCH_ARGS)
                ctx = browser.new_context(**CTX_KW)
        ctx.set_default_timeout(5_000)
        ctx.on("close", lambda *_: self._ctx.pop(key, None))
        self._ctx[key] = ctx
        return ctx

    def page(self, ctx):
        """A free blank tab if there is one, otherwise a new tab."""
        for pg in ctx.pages:
            if not pg.is_closed() and pg.url in _BLANK and id(pg) not in self._claimed:
                self._claimed.add(id(pg))
                return pg
        pg = ctx.new_page()
        self._claimed.add(id(pg))
        return pg

    def req_log(self, pg) -> list:
        """List of (host, resource_type, ms, url) for finished requests on this tab (for load diagnostics)."""
        entries = self._logs.get(id(pg))
        if entries is None:
            entries = []
            self._logs[id(pg)] = entries

            def _done(req, _l=entries):
                try:
                    end = (req.timing or {}).get("responseEnd", -1)
                    _l.append((urlsplit(req.url).hostname or "", req.resource_type,
                               int(end) if end and end > 0 else -1, req.url[:140]))
                    if len(_l) > 800:
                        del _l[:200]
                except Exception:
                    pass

            pg.on("requestfinished", _done)
        return entries

    def take_warm(self, key):
        """A tab that still has the app loaded from the previous case, or None."""
        pg = self._warm.pop(key, None)
        return pg if pg is not None and not pg.is_closed() else None

    def keep_warm(self, key, pg):
        """Keep this tab (and the app loaded in it) for the next case instead of blanking it."""
        old = self._warm.get(key)
        if old is not None and old is not pg:
            self._claimed.discard(id(old))
        self._warm[key] = pg

    def release(self, ctx, pg):
        """Hand a tab back. The last tab is never closed (that could close the browser)."""
        self._claimed.discard(id(pg))
        try:
            if len([p for p in ctx.pages if not p.is_closed()]) > 1:
                pg.close()                      # routes die with the tab: no unroute round trip
            else:
                try:
                    pg.unroute_all(behavior="ignoreErrors")
                except Exception:
                    pass
                pg.goto("about:blank")
        except Exception:
            pass

    def shutdown(self):
        if self.cdp_url:
            try:
                if self._browser:
                    self._browser.close()      # disconnects; does not quit your Chrome
            except Exception:
                pass
        else:
            for ctx in list(self._ctx.values()):
                try:
                    ctx.close()
                except Exception:
                    pass
        self._ctx.clear()
        self._warm.clear()
        self._logs.clear()
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._pw = None
        self._browser = None


_POOL = None


def configure(cdp_url: str = "") -> BrowserPool:
    global _POOL
    _POOL = BrowserPool(cdp_url)
    return _POOL


def get_pool() -> BrowserPool:
    global _POOL
    if _POOL is None:
        _POOL = BrowserPool()
    return _POOL


def shutdown():
    global _POOL
    if _POOL is not None:
        _POOL.shutdown()
        _POOL = None