import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agents.browser_agent import PlatformBrowserAgent
from agents.evidence_utils import extract_timeline_dates


class OrderTimelineTests(unittest.TestCase):
    def test_flipkart_order_confirmed_date_and_paid_price(self):
        timeline = extract_timeline_dates(
            "Order Confirmed, Sep 22, 2021\nPaid ₹248 by Net Banking"
        )

        self.assertEqual(timeline["order_date"], "Sep 22, 2021")
        self.assertEqual(timeline["price"], "248")
        self.assertEqual(timeline["payment_method"], "Net Banking")

    def test_payment_methods_are_extracted_only_from_payment_context(self):
        samples = (
            ("Payment method\nBHIM UPI", "UPI"),
            ("Payment Details\nCredit Card ending 1234", "Card"),
            ("Payment Mode: Cash on Delivery", "Cash on Delivery"),
            ("Mode of Payment\nFlipkart Pay Later", "Wallet / Pay Later"),
            ("Payments\nShipping\nCard offers and UPI offers", ""),
        )
        for text, expected in samples:
            with self.subTest(text=text):
                self.assertEqual(
                    extract_timeline_dates(text)["payment_method"],
                    expected,
                )

    def test_flipkart_opens_matching_order_details_link_for_payment_data(self):
        agent = PlatformBrowserAgent("flipkart", Path("."))
        agent.page = MagicMock()
        agent.page.url = (
            "https://www.flipkart.com/account/orders?"
            "order_id=OD222915018370162000"
        )
        order_link = MagicMock()
        order_link.get_attribute.return_value = (
            "/order_details?order_id=OD222915018370162000"
            "&item_id=22291501837016200"
        )
        order_link.is_visible.return_value = True
        order_link.click.side_effect = lambda: setattr(
            agent.page,
            "url",
            "https://www.flipkart.com/order_details?order_id=OD222915018370162000",
        )
        links = MagicMock()
        links.count.return_value = 1
        links.nth.return_value = order_link
        agent.page.locator.return_value = links

        with patch.object(agent, "_settle") as settle:
            agent._open_flipkart_order_details("OD222915018370162000")

        order_link.click.assert_called_once_with()
        settle.assert_called_once_with(300)

    def test_labeled_prices_work_across_platform_formats(self):
        samples = (
            ("Grand Total:\n₹354.00", "354.00"),
            ("Order Total: Rs. 1,299", "1299"),
            ("Price details\nPaid ₹248 by UPI", "248"),
        )
        for text, expected in samples:
            with self.subTest(text=text):
                self.assertEqual(extract_timeline_dates(text)["price"], expected)

    def test_details_and_return_status_pages_both_contribute_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            agent = PlatformBrowserAgent("amazon", Path(temp_dir))
            agent.platform_url = "https://www.amazon.in"
            agent.page = MagicMock()
            agent.page.url = "https://www.amazon.in/your-orders/search"
            agent.page.goto.side_effect = lambda url, **_: setattr(agent.page, "url", url)
            agent.page.content.return_value = "<html></html>"

            details_link = MagicMock()
            details_link.is_visible.return_value = True
            details_link.get_attribute.return_value = (
                "/your-orders/order-details?orderID=111-2222222-3333333"
            )
            details_locator = MagicMock()
            details_locator.count.return_value = 1
            details_locator.nth.return_value = details_link
            status_link = MagicMock()
            status_link.get_attribute.return_value = "/returns/status?orderId=111-2222222-3333333"
            status_locator = MagicMock()
            status_locator.count.return_value = 1
            status_locator.nth.return_value = status_link
            status_link.is_visible.return_value = True
            body_locator = MagicMock()
            body_locator.inner_text.side_effect = lambda: (
                "Order placed 16 December 2025\nGrand Total: ₹354.00"
                if "order-details" in agent.page.url
                else "Replacement requested on 17 December 2025\n"
                     "Replacement complete on 22 December 2025"
            )

            def page_locator(selector):
                if selector == "body":
                    return body_locator
                if selector in agent.config["details_link"]:
                    return details_locator
                if selector in agent.config["return_status_link"]:
                    return status_locator
                return MagicMock()

            agent.page.locator.side_effect = page_locator

            def first_visible(selectors):
                if selectors == agent.config["details_link"]:
                    return details_link
                if selectors == agent.config["return_status_link"]:
                    return status_link
                return None

            with patch.object(agent, "search_order"), \
                    patch.object(agent, "_first_visible", side_effect=first_visible), \
                    patch.object(agent, "_settle"), \
                    patch.object(agent, "_download_invoices", return_value=[]):
                details = agent.collect_evidence(
                    "CASE001", "111-2222222-3333333", "Example product"
                )

        self.assertIn("order-details", details["pages"]["order_details"])
        self.assertIn("/returns/status", details["pages"]["return_status"])
        self.assertEqual(details["timeline"]["order_date"], "16 December 2025")
        self.assertEqual(details["timeline"]["price"], "354.00")
        self.assertEqual(details["timeline"]["replacement_initialization"], "17 December 2025")
        self.assertEqual(details["timeline"]["replacement_date"], "22 December 2025")

    def test_flipkart_login_redirect_means_session_is_not_logged_in(self):
        agent = PlatformBrowserAgent("flipkart", Path("."))
        agent.page = MagicMock()
        agent.page.goto.side_effect = lambda _url, **_: setattr(
            agent.page, "url", "https://www.flipkart.com/login?type=email"
        )

        with patch.object(agent, "_settle"):
            self.assertFalse(agent._is_logged_in())


if __name__ == "__main__":
    unittest.main()
