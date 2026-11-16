"""agents/fast_utils.py - timing and page-readiness helpers shared by both agents."""
import time
from contextlib import contextmanager


class Timer:
    """Per-phase wall-clock timings in milliseconds."""

    def __init__(self):
        self.t0 = time.perf_counter()
        self.marks: dict = {}

    @contextmanager
    def phase(self, name: str):
        s = time.perf_counter()
        try:
            yield
        finally:
            self.marks[name] = self.marks.get(name, 0) + int((time.perf_counter() - s) * 1000)

    def total_ms(self) -> int:
        return int((time.perf_counter() - self.t0) * 1000)


_READY_JS = r"""
(args) => {
  const texts = args[0], sels = args[1];
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  let busy = false;
  for (const e of document.querySelectorAll(
      '[aria-busy="true"], [role="progressbar"], [class*="skeleton" i], [class*="spinner" i], [class*="loader" i]')) {
    if (vis(e)) { busy = true; break; }
  }
  const text = document.body ? document.body.innerText.toLowerCase() : '';
  let any = (texts.length === 0 && sels.length === 0);
  for (const t of texts) if (t && text.includes(t.toLowerCase())) any = true;
  for (const s of sels) if (document.querySelector(s)) any = true;
  return {rs: document.readyState, busy: busy, len: text.length, any: any};
}
"""


def wait_page_ready(page, any_of_texts=None, any_of_selectors=None, timeout_ms: int = 3_000,
                    stable_polls: int = 2, poll_ms: int = 80) -> bool:
    """
    True as soon as the page content is really there and has stopped changing:
      - at least one expected text / selector is present (if any were given), and
      - the visible text length is unchanged for `stable_polls` polls (~160 ms).
    A visible spinner/skeleton only adds ~240 ms to that (many apps keep a permanent loader element,
    so it must not block readiness). Returns False on timeout; the last page state is kept in
    wait_page_ready.last so the caller can store it as a diagnostic.
    """
    texts = [t for t in (any_of_texts or []) if t]
    sels = list(any_of_selectors or [])
    deadline = time.perf_counter() + timeout_ms / 1000
    last_len, stable = -1, 0
    while True:
        try:
            st = page.evaluate(_READY_JS, [texts, sels])
        except Exception:          # navigation in progress
            st = None
        if st:
            wait_page_ready.last = st
            has = bool(st["any"]) and st["len"] > 0
            stable = (stable + 1 if st["len"] == last_len else 1) if has else 0
            last_len = st["len"]
            if has and stable >= stable_polls + (3 if st["busy"] else 0):
                return True
        if time.perf_counter() >= deadline:
            return False
        page.wait_for_timeout(poll_ms)


wait_page_ready.last = {}