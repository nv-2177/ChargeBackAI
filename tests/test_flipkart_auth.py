import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agents.browser_agent import PlatformBrowserAgent


class FlipkartAuthTests(unittest.TestCase):
    def make_agent(self, landing_url):
        agent = PlatformBrowserAgent("flipkart", Path("."))
        agent.page = MagicMock()

        def goto(_url, **_kwargs):
            agent.page.url = landing_url

        agent.page.goto.side_effect = goto
        return agent

    def test_login_redirect_is_not_mistaken_for_authenticated_session(self):
        agent = self.make_agent("https://www.flipkart.com/login?type=email")

        with patch.object(agent, "_configured_url", return_value="https://www.flipkart.com/account/orders"), \
                patch.object(agent, "_settle"):
            self.assertFalse(agent._already_logged_in())

    def test_orders_landing_page_is_accepted_as_authenticated(self):
        agent = self.make_agent("https://www.flipkart.com/account/orders")

        with patch.object(agent, "_configured_url", return_value="https://www.flipkart.com/account/orders"), \
                patch.object(agent, "_settle"):
            self.assertTrue(agent._already_logged_in())


if __name__ == "__main__":
    unittest.main()
