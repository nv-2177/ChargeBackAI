import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agents.browser_agent import PlatformBrowserAgent


class BrowserAgentTests(unittest.TestCase):
    def test_amazon_orders_page_is_not_navigated_to_twice(self):
        agent = PlatformBrowserAgent("amazon", Path("."))
        agent.platform_url = "https://www.amazon.in"
        agent.page = MagicMock()
        agent.page.url = "https://www.amazon.in/gp/css/order-history?ref_=nav_orders_first"

        with patch.object(agent, "_first_visible") as first_visible:
            agent.go_to_orders()

        agent.page.goto.assert_not_called()
        first_visible.assert_not_called()

    def test_session_check_does_not_reload_current_orders_page(self):
        agent = PlatformBrowserAgent("amazon", Path("."))
        agent.platform_url = "https://www.amazon.in"
        agent.page = MagicMock()
        agent.page.url = "https://www.amazon.in/gp/css/order-history"
        account_indicator = MagicMock()
        account_indicator.count.return_value = 1
        account_indicator.inner_text.return_value = "Hello, Customer"
        agent.page.locator.return_value.first = account_indicator

        with patch.object(agent, "_settle") as settle:
            self.assertTrue(agent._is_logged_in())

        agent.page.goto.assert_not_called()
        settle.assert_not_called()

    def test_find_order_card_uses_configured_order_details_link(self):
        agent = PlatformBrowserAgent("amazon", Path("."))
        marked_card = MagicMock()
        marked_card.first = MagicMock(name="marked_card")
        link_locator = MagicMock()
        agent.page = MagicMock()
        agent.page.locator.side_effect = lambda selector: (
            marked_card if selector == "[data-cb-card='1']" else link_locator
        )
        agent.page.evaluate.return_value = {
            "ok": True,
            "level": 4,
            "ids": ["407-4729841-9313128"],
        }

        result = agent.find_order_card("407-4729841-9313128")

        self.assertIs(result, marked_card.first)
        link_locator.first.wait_for.assert_called_once_with(state="attached", timeout=15_000)
        self.assertEqual(agent.page.evaluate.call_args.args[1][3], agent.config["card_link_selector"])


if __name__ == "__main__":
    unittest.main()
