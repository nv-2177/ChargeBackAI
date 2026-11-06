import unittest
from unittest.mock import patch

from config.platforms import PLATFORM_CONFIGS, get_platform_config, resolve_platform_url


class PlatformConfigTests(unittest.TestCase):
    def test_configured_relative_urls_use_case_platform_url(self):
        config = {
            "base_url": "https://configured.example",
            "login_url": "/signin",
            "orders_url": "/account/orders",
            "direct_order_url": "/account/orders/{order_id}",
        }

        self.assertEqual(
            resolve_platform_url(config, "login_url", "https://shop.example"),
            "https://shop.example/signin",
        )
        self.assertEqual(
            resolve_platform_url(
                config, "direct_order_url", "https://shop.example", order_id="A-123"
            ),
            "https://shop.example/account/orders/A-123",
        )

    def test_configured_base_url_is_used_when_case_url_is_empty(self):
        config = {"base_url": "https://shop.example", "orders_url": "/orders"}

        self.assertEqual(
            resolve_platform_url(config, "orders_url"),
            "https://shop.example/orders",
        )

    def test_existing_platform_routes_follow_case_url(self):
        config = get_platform_config("flipkart")

        self.assertEqual(
            resolve_platform_url(config, "login_url", "https://alternate.example"),
            "https://alternate.example/account/login?ret=%2Faccount%2Forders",
        )
        self.assertEqual(
            resolve_platform_url(config, "orders_url", "https://alternate.example"),
            "https://alternate.example/account/orders",
        )

    def test_custom_platform_entry_is_available_by_configured_name(self):
        custom = {"base_url": "https://shop.example", "login_flow": "password"}
        with patch.dict(PLATFORM_CONFIGS, {"custom_store": custom}):
            self.assertIs(get_platform_config("Custom_Store"), custom)

    def test_invalid_or_missing_base_url_fails_clearly(self):
        with self.assertRaisesRegex(ValueError, "valid HTTP"):
            resolve_platform_url({"orders_url": "/orders"}, "orders_url")

    def test_missing_url_setting_fails_clearly(self):
        with self.assertRaisesRegex(ValueError, "missing 'orders_url'"):
            resolve_platform_url({"base_url": "https://shop.example"}, "orders_url")


if __name__ == "__main__":
    unittest.main()
