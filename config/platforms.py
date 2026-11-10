"""
Platform registry with universal keyword patterns for Orders & Ticketing Systems.
"""
from urllib.parse import urljoin, urlsplit

UNIVERSAL_NAVIGATION_KEYWORDS = {
    "ecommerce_orders": [
        "my orders", "your orders", "orders", "order history",
        "my purchases", "purchase history", "my account/orders",
        "orders & returns", "track orders", "all orders", "view orders"
    ],
    "ticketing_systems": [
        "my tickets", "tickets", "support tickets", "ticket history",
        "my requests", "all requests", "view tickets", "help tickets",
        "issue history", "my cases", "cases", "support history", "my issues"
    ]
}

PLATFORM_CONFIGS = {
    "amazon": {
        "base_url": "https://www.amazon.in",
        "login_flow": "password",
        "type": "ecommerce",
        "signin_nav": ["#nav-link-accountList"],
        "email_input": ["#ap_email_login", "#ap_email", "input[name='email']"],
        "continue_btn": ["#continue", "input#continue"],
        "password_input": ["#ap_password", "input[name='password']"],
        "submit_btn": ["#signInSubmit", "input#signInSubmit"],
        "account_indicator": "#nav-link-accountList-nav-line-1",
        "orders_nav": ["#nav-orders"],
        "orders_url": "/gp/css/order-history",
        "reauth_markers": ["/ap/signin"],
        "search_input": ["#searchOrdersInput", "#yourOrdersInput", "input[name='search']"],
        "search_btn": ["input[name='submit.search']", "button:has-text('Search Orders')", "input[aria-label*='Search Orders' i]"],
        "card_from_link": True,
        "order_id_regex": r"\d{3}-\d{7}-\d{7}",
        "order_card": ["div.a-section.a-spacing-none.a-padding-small", "div.order-card", "div.js-order-card", "div.a-box-group.order"],
        "product_link_css": "a[href*='/dp/'], a[href*='/gp/product/']",
        "item_title": ["a[href*='/dp/']", "a[href*='/gp/product/']"],
        "details_link": ["a:has-text('View order details')", "a:has-text('Order details')", "a[href*='order-details']"],
        "direct_details_url": "/your-orders/order-details?orderID={order_id}",
        "return_status_link": [
            "a:has-text('View Return/Refund Status')",
            "a:has-text('View Return/Replacement Status')",
            "a:has-text('Return/Replacement status')", "a:has-text('replacement status')",
            "a:has-text('return status')", "a:has-text('refund status')",
            "a:has-text('Track Return')", "a:has-text('Track replacement')",
        ],
        "invoice_trigger": ["a:has-text('Download Invoice')", "a:has-text('Invoice')", "button:has-text('Invoice')"],
        "invoice_href_regex": r"/documents/download/|invoice|print\.html",
        "print_summary_url": "/gp/css/summary/print.html?ie=UTF8&orderID={order_id}",
        "security_urls": ["/ap/cvf", "/ap/mfa", "/ap/challenge"],
        "security_selectors": ["#auth-mfa-otpcode", "input[name='cvf_captcha_input']", "form[action*='validateCaptcha']"],
    },

    "flipkart": {
        "base_url": "https://www.flipkart.com",
        "login_flow": "otp",
        "type": "ecommerce",
        "login_url": "/account/login?ret=%2Faccount%2Forders",
        "signin_nav": ["a[href*='/account/login']", "a:has-text('Login')", "button:has-text('Login')"],
        "email_mode_link": ["text=/Use Email-ID/i", "text=/Existing User\\? Log in/i"],
        "email_input": ["input[placeholder*='Email' i]", "input[type='email']", "input[type='text']:not([name='q'])"],
        "continue_btn": ["button:has-text('Request OTP')", "button:has-text('CONTINUE')", "button:has-text('Continue')"],
        "otp_input": ["input[maxlength='6']", "input[maxlength='1']", "input[placeholder*='OTP' i]"],
        "login_check": "orders_redirect",
        "login_redirect_marker": "/login",
        "orders_nav": ["a[href*='/account/orders']"],
        "orders_url": "/account/orders",
        "direct_order_url": "/account/orders?order_id={order_id}",
        "reauth_markers": ["/login"],
        "search_input": ["input[placeholder*='Search your orders' i]", "input[placeholder*='Search' i]:not([name='q'])"],
        "search_btn": ["button:has-text('Search Orders')", "button:has-text('Search')", "button[type='submit']"],
        "order_id_regex": r"OD\d{10,}",
        "order_card": ["div[class*='OrderCard']", "div[class*='_2pt2v']", "div[class*='row']", "a[href*='order_id=']"],
        "details_link": ["a[href*='order_details']", "a[href*='/rv/orders']", "a[href*='order_id=']"],
        "return_status_link": [
            "a:has-text('View Return/Refund Status')",
            "a:has-text('Return Status')", "a:has-text('Refund Status')",
            "a:has-text('Replacement Status')",
        ],
        "product_link_css": None,
        "item_title": ["a[href*='/p/']", "div[class*='title' i]", "span[class*='title' i]"],
        "invoice_trigger": ["a:has-text('Download Invoice')", "button:has-text('Download Invoice')", "a:has-text('Invoice')", "button:has-text('Invoice')"],
        "invoice_href_regex": r"invoice|\.pdf",
        "print_summary_url": None,
        "security_urls": [],
        "security_selectors": [],
    },  # <-- this comma was missing (syntax error)

    # ------------------------------------------------------------------ Ticketing
    # Zendesk AGENT WORKSPACE. Platform_URL in tickets.csv must be the subdomain
    # URL, e.g. https://acme.zendesk.com  (only scheme + host are used).
    # NOTE: selectors below are best-effort and UNVERIFIED against your account's
    # sign-in page; send me the sign-in page HTML if any of them miss.
    "zendesk": {
        "base_url": "",                      # always taken from Platform_URL per row
        "login_flow": "password",
        "type": "ticketing",
        "tickets_url": "/agent/",            # hitting this while logged out redirects to sign-in
        # "customer sign in" link on the agent sign-in page (clicked only if visible)
        "customer_signin_link": [
            "text=/customer sign.?in/i",
            "text=/sign in as (a )?customer/i",
            "text=/end.?user/i",
        ],
        "email_input": ["#user_email", "input[name='user[email]']", "input[type='email']"],
        "continue_btn": ["button:has-text('Next')", "button:has-text('Continue')"],
        "password_input": ["#user_password", "input[name='user[password]']", "input[type='password']"],
        "submit_btn": ["#sign-in-submit-button", "button[type='submit']", "input[type='submit']"],
        "reauth_markers": ["/auth/", "/access/unauthenticated", "/login", "/signin"],

        "direct_ticket_url": "/agent/tickets/{ticket_id}",
        # Session-cookie JSON endpoints (same login as the browser):
        "api_me_url": "/api/v2/users/me.json",
        "api_ticket_url": "/api/v2/tickets/{ticket_id}.json?include=metric_sets",
        "api_comments_url": "/api/v2/tickets/{ticket_id}/comments.json",
        # Only used if the account that signs in turns out to be an end-user (not an agent):
        "direct_ticket_url_enduser": "/hc/en-us/requests/{ticket_id}",
        "api_ticket_url_enduser": "/api/v2/requests/{ticket_id}.json",
        "api_comments_url_enduser": "/api/v2/requests/{ticket_id}/comments.json",

        "attachment_href_regex": r"/attachments/|/attachments/token/",
    },

    # Placeholder only - untested. No API keys configured, so it runs DOM-only.
    "jira": {
        "base_url": "",
        "login_flow": "password",
        "type": "ticketing",
        "customer_signin_link": [],
        "email_input": ["#username"],
        "continue_btn": ["#login-submit"],
        "password_input": ["#password"],
        "submit_btn": ["#login-submit"],
        "tickets_url": "/jira/your-work",
        "reauth_markers": ["/auth/v2/login", "/login"],
        "direct_ticket_url": "/browse/{ticket_id}",
        "attachment_href_regex": r"/secure/attachment/",
    },
}


def get_platform_config(platform_name: str) -> dict:
    key = str(platform_name).strip().lower()
    if key not in PLATFORM_CONFIGS:
        return {
            "base_url": "",
            "login_flow": "password",
            "type": "generic",
            "signin_nav": ["a:has-text('Login')", "a:has-text('Sign in')", "button:has-text('Login')"],
            "email_input": ["input[type='email']", "input[type='text']"],
            "password_input": ["input[type='password']"],
            "submit_btn": ["button[type='submit']"],
            "orders_nav": [],
            "orders_url": "",
            "tickets_url": "",
            "direct_ticket_url": "",
            "search_input": ["input[type='search']", "input[placeholder*='Search' i]"],
            "search_btn": ["button[type='submit']"],
            "security_urls": [],
            "security_selectors": [],
        }
    return PLATFORM_CONFIGS[key]


def resolve_platform_url(config: dict, url_setting: str, platform_url: str = "", **values) -> str:
    configured = config.get(url_setting)
    if not configured:
        return platform_url
    try:
        configured = str(configured).format(**values)
    except KeyError:
        return platform_url

    base = str(platform_url or config.get("base_url", "")).strip()
    parsed = urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return configured

    pc = urlsplit(configured)
    if pc.scheme and pc.netloc:
        return configured
    return urljoin(base.rstrip("/") + "/", configured.lstrip("/") if not configured.startswith("/") else configured)