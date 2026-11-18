"""Offline checks (no browser, no network). Run:  python -m pytest tests -q   or   python tests/test_offline.py"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.platforms import get_platform_config, resolve_platform_url, PLATFORM_CONFIGS, GENERIC_TICKET_HINTS
from agents.evidence_utils import extract_ticket_details, extract_timeline_dates, normalize_iso
from agents.ticket_agent import TicketBrowserAgent, TicketAgentError
from agents.browser_agent import PlatformBrowserAgent, BrowserAgentError


def test_known_platform_types():
    assert get_platform_config("amazon")["type"] == "ecommerce"
    assert get_platform_config("zendesk")["type"] == "ticketing"
    assert get_platform_config("jira")["api_style"] == "jira"
    assert get_platform_config("freshdesk")["type"] == "ticketing"


def test_url_resolution_jira():
    cfg = get_platform_config("jira")
    u = resolve_platform_url(cfg, "api_ticket_url", "https://acme.atlassian.net", ticket_id="PROJ-12")
    assert u == "https://acme.atlassian.net/rest/api/2/issue/PROJ-12"


def test_unknown_ticketing_platform_now_accepted():
    with tempfile.TemporaryDirectory() as d:
        a = TicketBrowserAgent("helpscout-or-whatever", Path(d))
        assert a.generic and a.config["type"] == "ticketing"
        assert len(GENERIC_TICKET_HINTS) >= 5


def test_ecommerce_platform_rejected_in_ticket_mode():
    with tempfile.TemporaryDirectory() as d:
        try:
            TicketBrowserAgent("amazon", Path(d)); assert False
        except TicketAgentError as e:
            assert "ecommerce" in str(e)


def test_unknown_ecommerce_without_url_gives_clear_error():
    with tempfile.TemporaryDirectory() as d:
        a = PlatformBrowserAgent("nykaa", Path(d))
        a.page = object(); a.context = None
        a.login("", "u", "p")
        try:
            a.search_order("123"); assert False
        except BrowserAgentError as e:
            assert "Platform_URL" in str(e)


def test_jira_normalisation_and_dates():
    raw = {"key": "P-1", "fields": {"status": {"name": "In Progress"}, "priority": {"name": "High"},
           "created": "2026-11-10T11:15:48.000+0530", "summary": "Refund not received",
           "resolutiondate": None, "attachment": [{"content": "https://x/a.png", "filename": "a.png", "mimeType": "image/png", "size": 3, "id": "9"}]}}
    n = TicketBrowserAgent._normalize_jira(raw)
    assert n["ticket"]["status"] == "In Progress" and n["ticket"]["subject"] == "Refund not received"
    assert n["attachments"][0]["filename"] == "a.png"
    assert normalize_iso(n["ticket"]["created_at"]) == "2026-11-10T05:45:48+00:00"
    assert normalize_iso("2026-11-10T05:45:48.000Z") == "2026-11-10T05:45:48+00:00"


def test_dom_status_priority_variants():
    assert extract_ticket_details("Status\nIn Progress\nPriority\nHigh")["status"] == "In Progress"
    assert extract_ticket_details("Status: Open\nPriority: Normal")["priority"] == "Normal"
    assert extract_ticket_details("Statuses are great")["status"] == ""


def test_order_extraction_unchanged():
    txt = ("Order placed 5 November 2026\nOrder Total ₹1,299.00\nPayment Method\nUPI\nShipping Address\nChennai TN 600001")
    r = extract_timeline_dates(txt)
    assert r["price"] == "1299.00" and r["payment_method"] == "UPI" and r["pincode"] == "600001"


def test_return_page_html_to_dates():
    from agents.browser_agent import _html_to_text
    h = ("<div><span>Replacement requested</span><span>12 November 2026</span></div>"
         "<div><span>Replacement delivered</span></div><div>15 November 2026</div><script>var a='return'</script>")
    r = extract_timeline_dates(_html_to_text(h))
    assert r["replacement_initialization"] == "12 November 2026" and r["replacement_date"] == "15 November 2026"


def test_zendesk_api_first_fast_path():
    import json
    from agents.ticket_agent import TicketBrowserAgent
    T = {"ticket": {"id": 5, "subject": "Refund not received", "status": "solved", "priority": "high", "requester_id": 11,
         "assignee_id": 22, "created_at": "2026-11-10T05:45:48Z", "updated_at": "2026-11-12T09:00:00Z"},
         "users": [{"id": 11, "name": "Asha R"}, {"id": 22, "name": "Agent Bob"}], "metric_set": {"solved_at": "2026-11-12T08:59:00Z"}}
    C = {"comments": [{"id": 1, "author_id": 11, "created_at": "2026-11-10T05:45:48Z", "public": True,
         "plain_body": "Where is my <refund>?", "attachments": []}], "next_page": None}

    class R:
        ok, status = True, 200
        def __init__(s, j=None): s._j = j
        def json(s): return s._j
        def body(s): return b"x"
    class Req:
        def get(s, url, **k): return R(C if "comments.json" in url else T)
    class Ctx: request = Req()
    class Pg:
        def set_content(s, h, **k): pass
        def screenshot(s, path, **k): Path(path).write_bytes(b"j")
    with tempfile.TemporaryDirectory() as t:
        d = Path(t); (d / "documents").mkdir(); (d / "screenshots").mkdir()
        a = TicketBrowserAgent("zendesk", d)
        a.context, a.page, a._ensure_page, a.platform_url, a.evidence_dir = Ctx(), Pg(), (lambda: None), "https://acme.zendesk.com", d
        r = a.collect_ticket_evidence("TC", "5", "Refund not received")
        assert r["evidence_mode"] == "api_summary" and r["ticket_extra"]["requester"] == "Asha R"
        assert r["ticket_details"]["status"] == "solved" and r["subject_verification"]["matched"]
        assert "&lt;refund&gt;" in (d / "documents/ticket_summary.html").read_text()
        assert (d / "screenshots/ticket_page.jpg").exists() and (d / "documents/api_comments_5.json").exists()


def _amazon_stub(details_html, print_html="<html>nothing</html>", signed_in=True):
    class R:
        def __init__(s, body=b"", ct="text/html", url="", ok=True):
            s._b, s.ok, s.url, s.headers = body, ok, url, {"content-type": ct}
        def text(s): return s._b.decode()
        def body(s): return s._b
    class Req:
        def get(s, url, **k):
            if not signed_in: return R(b"login", url="https://www.amazon.in/ap/signin?x=1")
            if "order-details" in url: return R(details_html.encode(), url=url)
            if "print.html" in url: return R(print_html.encode(), url=url)
            if url.endswith("invoice.pdf"): return R(b"%PDF-1.4", ct="application/pdf", url=url)
            if "return-status" in url:
                return R(b"<div>Replacement requested</div><div>12 November 2026</div><div>Replacement delivered</div><div>15 November 2026</div>", url=url)
            return R(b"", ok=False, url=url)
    class Ctx: request = Req()
    class Pg:
        def set_content(s, h, **k): s.h = h
        def screenshot(s, path, **k): Path(path).write_bytes(b"j")
    return Ctx(), Pg()


_OID = "404-1234567-1234567"
_DETAILS = (f"<div>Order placed</div><div>5 November 2026</div><div>Order # {_OID}</div><div>boAt Airdopes 141 earbuds</div>"
            "<div>Order Total</div><div>₹1,299.00</div><a href='/return-status?o=1'><span>View Return/Replacement Status</span></a>"
            "<a href='/documents/download/abc/invoice.pdf'>Download Invoice</a>")
_PRINT = "<div>Payment Method</div><div>UPI</div><div>Shipping Address</div><div>Chennai TN 600001</div><p>" + _OID + "</p>"


def _run_amazon(details, print_html="<html>x</html>", signed_in=True):
    from agents.browser_agent import PlatformBrowserAgent
    t = tempfile.mkdtemp(); d = Path(t); (d / "documents").mkdir(); (d / "screenshots").mkdir()
    a = PlatformBrowserAgent("amazon", d)
    a.context, a.page = _amazon_stub(details, print_html, signed_in)
    a._ensure_page = lambda: None
    a.platform_url = "https://www.amazon.in"
    return a, d, a._collect_via_http


def test_amazon_http_first_success_with_print_summary_fill():
    from agents.fast_utils import Timer
    a, d, fn = _run_amazon(_DETAILS, _PRINT)
    det = {"order_id": _OID, "product_name_expected": "boAt Airdopes 141", "pages": {}, "invoices": [], "collected_at_utc": "now"}
    assert fn(_OID, "boAt Airdopes 141", Timer(), det) is True
    tl = det["timeline"]
    assert tl["price"] == "1299.00" and tl["payment_method"] == "UPI" and tl["pincode"] == "600001"   # payment/pincode via print page
    assert tl["replacement_initialization"] == "12 November 2026" and tl["replacement_date"] == "15 November 2026"
    assert det["product_verification"]["matched"] and det["invoices"][0]["file"].endswith(".pdf")
    assert (d / "screenshots/order_summary_page.jpg").exists() and (d / "documents/dom_order_details.html").exists()


def test_amazon_http_first_falls_back_when_signed_out_or_incomplete():
    from agents.fast_utils import Timer
    a, d, fn = _run_amazon(_DETAILS, _PRINT, signed_in=False)
    assert fn(_OID, "x", Timer(), {"pages": {}, "invoices": [], "collected_at_utc": "n"}) is False
    a, d, fn = _run_amazon("<div>some page without price or order id</div>")
    assert fn(_OID, "x", Timer(), {"pages": {}, "invoices": [], "collected_at_utc": "n"}) is False
    assert not (d / "documents/dom_order_details.html").exists()        # nothing half-written


def test_flipkart_not_http_eligible():
    from agents.browser_agent import PlatformBrowserAgent
    with tempfile.TemporaryDirectory() as t:
        a = PlatformBrowserAgent("flipkart", Path(t)); a.platform_url = "https://www.flipkart.com"
        assert not a._fast_http_eligible()


if __name__ == "__main__":
    fails = 0
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            try: f(); print("PASS", n)
            except Exception as e: fails += 1; print("FAIL", n, repr(e))
    sys.exit(1 if fails else 0)