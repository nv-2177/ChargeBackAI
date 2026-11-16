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
    assert u == "https://acme.atlassian.net/rest/api/3/issue/PROJ-12"


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


if __name__ == "__main__":
    fails = 0
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            try: f(); print("PASS", n)
            except Exception as e: fails += 1; print("FAIL", n, repr(e))
    sys.exit(1 if fails else 0)