"""
agents/path_learner.py

Learn-once / replay-forever navigation, shared by the e-commerce and ticketing agents.

FIRST visit to a website (no stored path):
    discover each button/field with GENERIC keywords (no platform-specific selectors),
    perform it, and record the concrete step (selector, matched keyword, expected URL).
    If keyword discovery cannot reach the order/ticket, fall back to the URL hints in
    config/platforms.py and record that instead.
NEXT visits (stored path exists):
    NO keyword searching. The stored steps are replayed exactly. If a step fails
    (selector gone, wrong page), that stored path is discarded and re-learned once.

Everything is written to config/learned_paths.json, keyed by  platform|host|flow .
Passwords/usernames/order IDs are never stored: steps hold placeholders
(value_from = "username" | "password"; selectors/URLs use {order_id} / {ticket_id}).
"""
import copy
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from config.platforms import UNIVERSAL_NAVIGATION_KEYWORDS

log = logging.getLogger("chargebackai")

STORE_PATH = Path(__file__).resolve().parent.parent / "config" / "learned_paths.json"
SCHEMA_VERSION = 1
FORCE_RELEARN = False      # set by main.py --relearn
_RELEARNED: set = set()    # keys already relearned in this process (relearn once, not per case)

# ----------------------------------------------------------------------------
# Generic keyword lists - NOT platform specific. Earlier entry = higher priority.
# ----------------------------------------------------------------------------
KEYWORDS = {
    "signin": ["sign in", "signin", "sign-in", "log in", "login", "my account", "account"],
    "customer_signin": ["customer sign in", "customer sign-in", "customer login", "sign in as customer",
                        "sign in as a customer", "end user", "end-user"],
    "continue": ["continue", "next", "request otp", "get otp", "send otp", "proceed"],
    "submit": ["sign in", "sign-in", "log in", "login", "submit", "verify", "continue"],
    "orders_nav": list(UNIVERSAL_NAVIGATION_KEYWORDS["ecommerce_orders"]),
    "tickets_nav": list(UNIVERSAL_NAVIGATION_KEYWORDS["ticketing_systems"]),
    "search_btn": ["search orders", "search tickets", "search requests", "search", "go"],
    "details_link": ["view order details", "order details", "view details", "details",
                     "view ticket", "open ticket", "view request", "view case"],
}
FIELD_KEYWORDS = {"email": ["email", "e-mail", "username", "user name", "user id", "login", "mobile", "phone"]}
LOGIN_INTENTS = {"signin", "customer_signin", "continue", "submit"}
NEGATIVE_LOGIN = ("google", "facebook", "apple", "microsoft", "sso", "forgot", "create account",
                  "register", "sign up", "signup")

_LOGIN_PATH = re.compile(r"(sign-?in|log-?in|/auth/|/ap/|unauthenticated|/sso)", re.I)

_CLICKABLE = ('a, button, [role="button"], [role="link"], [role="menuitem"], [role="tab"], '
              'input[type="submit"], input[type="button"], summary')
_INPUTS = ('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="checkbox"])'
           ':not([type="radio"]):not([type="image"]), textarea')

_COLLECT_JS = r"""
(sel) => {
  document.querySelectorAll('[data-cbai-i]').forEach(e => e.removeAttribute('data-cbai-i'));
  const vis = e => {
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const s = getComputedStyle(e);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  const out = []; let i = 0;
  for (const e of document.querySelectorAll(sel)) {
    if (!vis(e)) continue;
    e.setAttribute('data-cbai-i', String(i));
    out.push({
      i, tag: e.tagName.toLowerCase(),
      text: ((e.innerText || e.value || '') + '').trim().replace(/\s+/g, ' ').slice(0, 100),
      aria: e.getAttribute('aria-label') || '', title: e.getAttribute('title') || '',
      href: e.getAttribute('href') || '', id: e.id || '',
      testid: e.getAttribute('data-testid') || e.getAttribute('data-test-id') || '',
      name: e.getAttribute('name') || '', type: e.getAttribute('type') || '',
      placeholder: e.getAttribute('placeholder') || '',
      autocomplete: e.getAttribute('autocomplete') || ''
    });
    if (++i >= 500) break;
  }
  return out;
}
"""


_SOFT_NAV_JS = """(u) => { const x = new URL(u, location.href);
  history.pushState({}, '', x.pathname + x.search + x.hash);
  window.dispatchEvent(new PopStateEvent('popstate', {state: {}})); return location.href; }"""


class StepFailure(Exception):
    """A step could not be performed / did not reach the expected page."""


class LoginRequired(Exception):
    """The browser is on a sign-in page."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ----------------------------------------------------------------------------
class PathStore:
    def __init__(self, path: Path = STORE_PATH):
        self.path = Path(path)

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("paths"), dict):
                return data
        except Exception:
            pass
        return {"schema": SCHEMA_VERSION, "paths": {}}

    def get(self, key: str):
        return copy.deepcopy(self._load()["paths"].get(key))

    def put(self, key: str, entry: dict):
        data = self._load()
        data["paths"][key] = entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)


# ----------------------------------------------------------------------------
class PathNavigator:
    def __init__(self, page_fn, platform: str, origin: str, flow: str, target_key: str, target_value: str,
                 start_url: str, username: str, password: str, hint_urls=None, store: PathStore | None = None,
                 on_login_start=None, on_login_end=None, soft_nav: bool = False):
        self._page_fn = page_fn
        self.platform = str(platform).strip().lower()
        self.origin = origin.rstrip("/")
        self.host = urlsplit(self.origin).netloc.lower()
        self.flow = flow                      # "orders" | "tickets"
        self.target_key = target_key          # "order_id" | "ticket_id"
        self.target = str(target_value).strip()
        self.username = username or ""
        self.password = password or ""
        self.start_url = start_url or self.origin
        self.hint_urls = [h for h in (hint_urls or []) if h]
        self.store = store or PathStore()
        self.on_login_start = on_login_start
        self.on_login_end = on_login_end
        self.key = f"{self.platform}|{self.host}|{self.flow}"
        self._last_login_url = ""
        self.soft_navigated = False

        stored = None
        if FORCE_RELEARN and self.key not in _RELEARNED:
            _RELEARNED.add(self.key)
        else:
            stored = self.store.get(self.key)
        self._soft_requested = bool(soft_nav)
        self.entry = stored or {
            "schema": SCHEMA_VERSION, "platform": self.platform, "host": self.host, "flow": self.flow,
            "learned_at_utc": None, "last_used_utc": None, "runs_ok": 0, "discovered_via": None,
            "start_url": None, "login_steps": None, "fetch_steps": None, "fast_path": None,
        }

    # ------------------------------------------------------------ public API
    def open_target(self) -> dict:
        """Navigate (signing in if needed) to the order/ticket page. Returns {'mode', 'url'}."""
        e = self.entry
        if e.get("fetch_steps") or e.get("fast_path"):
            try:
                mode = self._replay_fetch()
                self._touch()
                return {"mode": f"replay:{mode}", "url": self._p().url}
            except StepFailure as ex:
                log.warning(f"[{self.key}] stored path failed ({ex}); re-learning it.")
                e["fetch_steps"], e["fast_path"] = None, None
        mode = self._learn_fetch()
        return {"mode": f"learned:{mode}", "url": self._p().url}

    def recheck(self) -> bool:
        """Call once the page has rendered. The quick jump to a stored URL no longer waits for the page to load,
        so if the site bounced us to its sign-in page, sign in and reopen the target. True if that happened."""
        if self._looks_like_login():
            self._last_login_url = self._p().url
            self._login()
            self.open_target()
            return True
        return False

    # ------------------------------------------------------------ helpers
    def _p(self):
        return self._page_fn()

    def _settle(self, ms: int = 250):
        p = self._p()
        try:
            p.wait_for_load_state("domcontentloaded", timeout=2_000)
        except Exception:
            pass
        p.wait_for_timeout(ms)

    def _fmt(self, s: str) -> str:
        return str(s).replace("{origin}", self.origin).replace("{" + self.target_key + "}", self.target)

    def _templ(self, s: str) -> str:
        s = str(s)
        if self.target:
            s = s.replace(self.target, "{" + self.target_key + "}")
        return s.replace(self.origin, "{origin}")

    def _abs(self, url: str) -> str:
        return self.origin + url if url.startswith("/") else url

    def _value(self, name: str) -> str:
        if name == "username":
            return self.username
        if name == "password":
            return self.password
        if name == self.target_key:
            return self.target
        raise StepFailure(f"unknown value_from '{name}'")

    def _first_visible(self, selector: str):
        p = self._p()
        try:
            loc = p.locator(selector)
            for i in range(min(loc.count(), 5)):
                if loc.nth(i).is_visible():
                    return loc.nth(i)
        except Exception:
            pass
        return None

    def _wait_visible(self, selector: str, timeout_ms: int = 4_000):
        end = time.time() + timeout_ms / 1000
        while time.time() < end:
            el = self._first_visible(selector)
            if el:
                return el
            self._p().wait_for_timeout(100)
        return None

    def _untag(self):
        try:
            self._p().evaluate("document.querySelectorAll('[data-cbai-i]').forEach(e=>e.removeAttribute('data-cbai-i'))")
        except Exception:
            pass

    def _looks_like_login(self) -> bool:
        p = self._p()
        try:
            if _LOGIN_PATH.search(urlsplit(p.url).path):
                return True
            return p.locator("input[type='password']").first.is_visible()
        except Exception:
            return False

    def _raise_if_login(self):
        if self._looks_like_login():
            self._last_login_url = self._p().url
            raise LoginRequired()

    def _target_reached(self) -> bool:
        tv = self.target.lower()
        in_url_only = self.flow == "tickets"   # ticket numbers are short; require them in the URL
        deadline = time.time() + 5
        while True:
            p = self._p()
            self._raise_if_login()
            if tv in p.url.lower():
                return True
            if not in_url_only:
                try:
                    if tv in p.locator("body").inner_text(timeout=1_500).lower():
                        return True
                except Exception:
                    pass
            if time.time() >= deadline:
                return False
            p.wait_for_timeout(120)

    def _is_final_page(self) -> bool:
        url = self._p().url.lower()
        if self.flow == "orders":
            return "detail" in urlsplit(url).path
        return self.target.lower() in url

    # ------------------------------------------------------------ step execution (REPLAY)
    def _exec(self, s: dict):
        p = self._p()
        a = s["action"]
        if a == "goto":
            url = self._abs(self._fmt(s["url"]))
            soft = (self._soft_requested and self.entry.get("soft_nav") != "fails"
                    and s.get("name") == "fast_path" and p.url.startswith(self.origin + "/"))
            if soft:   # in-app navigation: the already-loaded app is reused (the caller must verify the new content)
                p.evaluate(_SOFT_NAV_JS, url)
                self.soft_navigated = True
            else:
                p.goto(url, wait_until="commit")   # returns at first byte; no load wait
                self.soft_navigated = False
                if s.get("settle_ms"):
                    p.wait_for_timeout(s["settle_ms"])
        elif a == "click":
            if s.get("skip_if_visible") and self._first_visible(s["skip_if_visible"]):
                return
            sel = self._fmt(s["selector"])
            el = self._wait_visible(sel, s.get("timeout_ms", 4_000))
            if not el:
                if s.get("optional"):
                    return
                raise StepFailure(f"step '{s['name']}': selector not found: {sel}")
            el.click()
            p.wait_for_timeout(s.get("settle_ms", 100))
        elif a == "fill":
            sel = self._fmt(s["selector"])
            el = self._wait_visible(sel, s.get("timeout_ms", 4_000))
            if not el:
                if s.get("optional"):
                    return
                raise StepFailure(f"step '{s['name']}': field not found: {sel}")
            el.click()
            el.fill(self._value(s["value_from"]))
        elif a == "press":
            p.keyboard.press(s["key"])
            p.wait_for_timeout(s.get("settle_ms", 300))
        elif a == "manual_pause":
            print(f"\n[ACTION REQUIRED] {s['message']}")
            input(">>> Press ENTER when done... ")
            self._settle(500)
        else:
            raise StepFailure(f"unknown action '{a}'")
        exp = s.get("expect_url_contains")
        if exp:
            exp = self._fmt(exp).lower()
            end = time.time() + 3
            while exp not in self._p().url.lower():
                if self._looks_like_login():
                    return  # login handling is done by the caller
                if time.time() > end:
                    raise StepFailure(f"step '{s['name']}': expected URL to contain '{exp}', got {self._p().url}")
                self._p().wait_for_timeout(150)

    def _resume(self, steps):
        for s in steps:
            self._exec(s)

    # ------------------------------------------------------------ replay
    def _fetch_once(self) -> str:
        e = self.entry
        errors = []
        if e.get("fast_path"):
            try:
                self._exec({"name": "fast_path", "action": "goto", "url": e["fast_path"]})
                if self._target_reached():
                    return "fast_path"
                errors.append("fast_path did not reach the target")
            except StepFailure as ex:
                errors.append(str(ex))
        if e.get("fetch_steps"):
            for s in e["fetch_steps"]:
                self._exec(s)
                self._raise_if_login()
            if self._target_reached():
                return "steps"
            errors.append("stored steps did not reach the target")
        raise StepFailure("; ".join(errors) or "no stored steps")

    def _replay_fetch(self) -> str:
        for _ in range(3):
            try:
                return self._fetch_once()
            except LoginRequired:
                self._login()
        raise StepFailure("still being sent to the sign-in page after signing in")

    def _touch(self):
        self.entry["runs_ok"] = int(self.entry.get("runs_ok") or 0) + 1
        self._save()

    def _save(self):
        self.entry["last_used_utc"] = _now()
        self.entry["path_summary"] = self._summary()
        self.store.put(self.key, self.entry)

    def _summary(self) -> list:
        out = []
        for label, steps in (("LOGIN (only when signed out)", self.entry.get("login_steps")),
                             ("FETCH", self.entry.get("fetch_steps"))):
            if steps:
                out.append(f"== {label} ==")
                out.extend(f"{n}. {s.get('instruction', s['action'])}" for n, s in enumerate(steps, 1))
        if self.entry.get("fast_path"):
            out.append(f"FAST PATH (tried first): go to {self.entry['fast_path']}")
        return out

    # ------------------------------------------------------------ login (replay or discover)
    def _login(self):
        if self.on_login_start:
            self.on_login_start()
        try:
            steps = self.entry.get("login_steps")
            if steps:
                try:
                    self._resume(steps)
                    self._wait_login_done()
                    return
                except StepFailure as ex:
                    log.warning(f"[{self.key}] stored login steps failed ({ex}); re-learning login.")
                    if self._last_login_url:
                        self._p().goto(self._last_login_url, wait_until="commit")
                        self._settle(500)
            self.entry["login_steps"] = self._discover_login()
            self._save()
        finally:
            if self.on_login_end:
                self.on_login_end()

    def _wait_login_done(self, timeout_s: int = 10):
        end = time.time() + timeout_s
        while time.time() < end:
            if not self._looks_like_login():
                return
            self._p().wait_for_timeout(300)
        raise StepFailure("still on the sign-in page after the login steps")

    # ------------------------------------------------------------ DISCOVERY: scoring / selectors
    def _score(self, m: dict, keywords, intent: str, require_href: str | None):
        hay = " ".join([m["text"], m["aria"], m["title"]]).lower().strip()
        if intent in LOGIN_INTENTS and any(n in hay for n in NEGATIVE_LOGIN):
            return -1, ""
        if require_href and require_href not in m["href"]:
            return -1, ""
        text, aria = m["text"].lower(), m["aria"].lower()
        best, hit = -1, ""
        for k, kw in enumerate(keywords):
            kw = kw.lower()
            if text == kw or aria == kw:
                s = 1000 - k * 10
            elif kw in hay and len(hay) <= 60:
                s = 500 - k * 10 - len(hay)
            else:
                continue
            if s > best:
                best, hit = s, kw
        if require_href:      # an element linking to THIS order/ticket is accepted even without a keyword
            best = max(best, 1500) + 2000
            hit = hit or f"href contains {self.target_key}"
        return best, hit

    def _field_score(self, m: dict, intent: str):
        t = (m["type"] or "text").lower()
        hay = " ".join([m["name"], m["id"], m["placeholder"], m["aria"], m["autocomplete"]]).lower()
        if intent == "password":
            return (3000, "type=password") if t == "password" else (-1, "")
        if t == "password":
            return -1, ""
        if intent == "email":
            if t == "email":
                return 2000, "type=email"
            if t in ("text", "tel", ""):
                for k, kw in enumerate(FIELD_KEYWORDS["email"]):
                    if kw in hay:
                        return 1000 - k * 10, kw
            return -1, ""
        if intent == "search":
            if m["name"].lower() == "q":
                return -1, ""
            words = ("order",) if self.flow == "orders" else ("ticket", "request", "case")
            for w in words:
                if w in hay and "search" in hay:
                    return 1500, f"placeholder/name mentions '{w}' + search"
            return -1, ""
        return -1, ""

    def _clean_text(self, text: str) -> str:
        t = (text or "").strip()
        local = self.username.split("@")[0].lower() if self.username else ""
        if not t or re.search(r"\d{4,}", t):
            return ""
        if len(local) >= 3 and local in t.lower():
            return ""
        return t

    def _href_key(self, href: str) -> str:
        if not href or href.startswith(("javascript:", "#", "mailto:")):
            return ""
        sp = urlsplit(href)
        if self.target and self.target in href:
            m = re.search(r"([\w.\-]+=[^&#]*" + re.escape(self.target) + r"[^&#]*)", sp.query)
            if m:
                return m.group(1)
            if self.target in sp.path:
                return sp.path
        return sp.path if len(sp.path) >= 3 else ""

    def _build_selector(self, meta: dict):
        tag = meta["tag"]
        cands = []

        def q(v):
            return str(v).replace("\\", "\\\\").replace('"', '\\"')

        if meta["id"] and not re.search(r"\d{4,}", meta["id"]) and self.target not in meta["id"]:
            cands.append(f'[id="{q(meta["id"])}"]')
        if meta["testid"]:
            cands.append(f'[data-testid="{q(meta["testid"])}"], [data-test-id="{q(meta["testid"])}"]')
        if meta["name"] and tag in ("input", "button", "textarea"):
            t = f'[type="{q(meta["type"])}"]' if meta["type"] else ""
            cands.append(f'{tag}[name="{q(meta["name"])}"]{t}')
        key = self._href_key(meta["href"])
        if key:
            cands.append(f'{tag}[href*="{q(key)}"]')
        if meta["aria"]:
            cands.append(f'{tag}[aria-label="{q(meta["aria"])}"]')
        if meta["placeholder"]:
            cands.append(f'{tag}[placeholder="{q(meta["placeholder"])}"]')
        txt = self._clean_text(meta["text"])
        if txt and len(txt) <= 40 and tag != "input":
            cands.append(f'{tag}:has-text("{q(txt)}")')
        for c in cands:
            el = self._first_visible(c)
            try:
                if el is not None and el.get_attribute("data-cbai-i") == str(meta["i"]):
                    return c, True
            except Exception:
                pass
        if cands:
            return cands[-1], False
        raise StepFailure("cannot build a stable selector for the matched element")

    # ------------------------------------------------------------ DISCOVERY: actions
    def _discover_click(self, name, intent, keywords, require_href=None, optional=False):
        p = self._p()
        metas = p.evaluate(_COLLECT_JS, _CLICKABLE)
        scored = []
        for m in metas:
            sc, kw = self._score(m, keywords, intent, require_href)
            if sc >= 0:
                scored.append((sc, kw, m))
        if not scored:
            self._untag()
            if optional:
                return None
            raise StepFailure(f"step '{name}': no visible element matched keywords {keywords[:5]}")
        scored.sort(key=lambda x: -x[0])
        _, kw, meta = scored[0]
        selector, verified = self._build_selector(meta)
        url_before = p.url
        try:
            p.locator(f'[data-cbai-i="{meta["i"]}"]').first.click()
        finally:
            self._untag()
        self._settle(400)

        step = {"name": name, "action": "click", "selector": self._templ(selector), "intent": intent,
                "matched_keyword": kw, "element_text": self._clean_text(meta["text"]),
                "selector_verified": verified}
        after = urlsplit(p.url)
        if (p.url != url_before and after.path != urlsplit(url_before).path and len(after.path) > 1
                and not self._looks_like_login()):
            step["expect_url_contains"] = self._templ(after.path)
        if optional:
            step["optional"] = True
        txt = f", text '{step['element_text']}'" if step["element_text"] else ""
        step["instruction"] = (f"Click the element matching `{step['selector']}` "
                               f"(found via keyword '{kw}'{txt})"
                               + (f"; expect URL to contain '{step['expect_url_contains']}'"
                                  if "expect_url_contains" in step else ""))
        log.info(f"[{self.key}] learned: {step['instruction']}")
        return step

    def _discover_field(self, name, intent, value_from, optional=False):
        p = self._p()
        metas = p.evaluate(_COLLECT_JS, _INPUTS)
        pick, best, why = None, -1, ""
        for m in metas:
            sc, w = self._field_score(m, intent)
            if sc > best:
                best, pick, why = sc, m, w
        if pick is None:
            self._untag()
            if optional:
                return None
            raise StepFailure(f"step '{name}': no visible {intent} input found")
        selector, verified = self._build_selector(pick)
        try:
            loc = p.locator(f'[data-cbai-i="{pick["i"]}"]').first
            loc.click()
            loc.fill(self._value(value_from))
        finally:
            self._untag()
        step = {"name": name, "action": "fill", "selector": self._templ(selector), "intent": intent,
                "value_from": value_from, "matched_by": why, "selector_verified": verified}
        step["instruction"] = f"Fill `{step['selector']}` with the {value_from} (matched by {why})"
        log.info(f"[{self.key}] learned: {step['instruction']}")
        return step

    def _press_step(self, name, key="Enter"):
        self._p().keyboard.press(key)
        self._settle(500)
        return {"name": name, "action": "press", "key": key, "instruction": f"Press {key}"}

    def _pause_step(self, name, message):
        print(f"\n[ACTION REQUIRED] {message}")
        input(">>> Press ENTER when done... ")
        self._settle(500)
        return {"name": name, "action": "manual_pause", "message": message,
                "instruction": f"Pause for the human: {message}"}

    def _goto_step(self, name, url):
        u = self._templ(url)
        return {"name": name, "action": "goto", "url": u, "instruction": f"Go to {u}"}

    # ------------------------------------------------------------ DISCOVERY: login
    def _discover_login(self) -> list:
        steps = []
        p = self._p()
        pw_sel = "input[type='password']"

        have_fields = self._first_visible(pw_sel) is not None or self._field_probe("email")
        if not have_fields:
            steps.append(self._discover_click("open_signin", "signin", KEYWORDS["signin"]))
            self._settle(600)
        cust = self._discover_click("customer_signin", "customer_signin", KEYWORDS["customer_signin"], optional=True)
        if cust:
            steps.append(cust)
            self._settle(600)

        if self._first_visible(pw_sel) is None or self._field_probe("email"):
            email_step = self._discover_field("enter_email", "email", "username")
            steps.append(email_step)
        else:
            email_step = None

        if self._wait_visible(pw_sel, 800) is None:      # two-step form: email first
            nxt = self._discover_click("continue_after_email", "continue", KEYWORDS["continue"], optional=True)
            steps.append(nxt if nxt else self._press_step("submit_email"))
            self._settle(500)

        if self._wait_visible(pw_sel, 4_000) is not None:
            steps.append(self._discover_field("enter_password", "password", "password"))
            sub = self._discover_click("submit_login", "submit", KEYWORDS["submit"], optional=True)
            steps.append(sub if sub else self._press_step("submit_password"))
        else:                                            # OTP / emailed code instead of a password
            steps.append(self._pause_step(
                "otp_or_verification",
                f"Enter the OTP / verification code for {self.username} in the browser window, then continue."))

        end = time.time() + 8
        while time.time() < end and self._looks_like_login():
            p.wait_for_timeout(300)
        if self._looks_like_login():
            steps.append(self._pause_step(
                "verification_2fa",
                f"Finish verification (2FA / captcha) for {self.username} in the browser window."))
            if self._looks_like_login():
                raise StepFailure("login did not complete")

        # Early steps are skipped on replay when the email field is already on screen
        if email_step:
            for s in steps:
                if s is email_step:
                    break
                if s["action"] == "click":
                    s["skip_if_visible"] = email_step["selector"]
        return steps

    def _field_probe(self, intent) -> bool:
        metas = self._p().evaluate(_COLLECT_JS, _INPUTS)
        self._untag()
        return any(self._field_score(m, intent)[0] >= 0 for m in metas)

    # ------------------------------------------------------------ DISCOVERY: fetch
    def _ensure_session(self, steps):
        if self._looks_like_login():
            self._last_login_url = self._p().url
            self._login()
            self._resume(steps)

    def _reached(self, steps) -> bool:
        try:
            return self._target_reached()
        except LoginRequired:
            self._login()
            self._resume(steps)
            try:
                return self._target_reached()
            except LoginRequired:
                raise StepFailure("signed in but still redirected to the sign-in page")

    def _discover_search(self, steps) -> bool:
        field = self._discover_field("search_for_target", "search", self.target_key, optional=True)
        if not field:
            return False
        steps.append(field)
        btn = self._discover_click("submit_search", "search_btn", KEYWORDS["search_btn"], optional=True)
        steps.append(btn if btn else self._press_step("submit_search"))
        self._settle(800)
        return True

    def _hops(self, steps, max_hops: int = 3):
        searched = False
        hop = 0
        while hop < max_hops:
            if self._reached(steps) and self._is_final_page():
                return
            hop += 1
            s = self._discover_click(f"open_target_{hop}", "details_link", KEYWORDS["details_link"],
                                     require_href=self.target, optional=True)
            if s is None:
                if not searched and self._discover_search(steps):
                    searched = True
                    hop -= 1
                    continue
                break
            steps.append(s)
        if not self._reached(steps):
            raise StepFailure(f"could not reach the page for {self.target_key} {self.target}")

    def _learn_fetch(self) -> str:
        log.info(f"[{self.key}] no stored path - discovering with generic keywords (first visit).")
        steps = [self._goto_step("open_start_page", self.start_url)]
        self._exec(steps[0])
        try:
            self._ensure_session(steps)
            nav_intent = "orders_nav" if self.flow == "orders" else "tickets_nav"
            lst = self._discover_click("open_list", nav_intent, KEYWORDS[nav_intent], optional=True)
            if lst:
                steps.append(lst)
                self._ensure_session(steps)
            self._hops(steps)
            via = "keywords"
        except (StepFailure, LoginRequired) as ex:
            log.warning(f"[{self.key}] keyword discovery failed ({ex}); trying configured URL hints.")
            steps = self._learn_from_hints()
            via = "config_hint"

        final = self._p().url
        e = self.entry
        e["fetch_steps"] = steps
        e["fast_path"] = self._stable_fast_path(final)
        e["discovered_via"] = via
        e["start_url"] = self._templ(self.start_url)
        e["learned_at_utc"] = _now()
        e["runs_ok"] = 1
        self._save()
        return via

    def _stable_fast_path(self, url: str):
        """A one-link shortcut is stored only if the URL differs per order by the order/ticket ID alone.
        URLs carrying other per-order values (item_id, unit_id, share_token, long opaque ids) are not
        shortcuts: they would send the next order to the wrong item."""
        if not self.target or self.target not in url:
            return None
        for k, vals in parse_qs(urlsplit(url).query).items():
            for v in vals:
                if self.target in v:
                    continue
                if "token" in k.lower() or re.fullmatch(r"[A-Za-z0-9_\-=.%]{12,}", v):
                    return None
        return self._templ(url)

    def _learn_from_hints(self) -> list:
        last = None
        for h in self.hint_urls:
            steps = [self._goto_step("open_hint_url", h)]
            try:
                self._exec(steps[0])
                self._ensure_session(steps)
                self._hops(steps)
                return steps
            except (StepFailure, LoginRequired) as ex:
                last = ex
        raise StepFailure(f"discovery failed and no configured URL reached the target ({last})")