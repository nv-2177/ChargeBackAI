import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
from agents.browser_agent import PlatformBrowserAgent
from agents.evidence_utils import extract_timeline_dates
from main import read_table, update_status, write_table


class ReplacementStatusTests(unittest.TestCase):
    def test_status_link_opens_directly(self):
        agent = PlatformBrowserAgent("amazon", Path("."))
        agent.page = MagicMock()
        agent.page.url = "https://www.amazon.in/your-orders"
        control = MagicMock()
        control.get_attribute.return_value = "/spr/returns/status?orderId=123"
        control.is_visible.return_value = True

        with patch.object(agent, "_find_status_control", return_value=control), \
                patch.object(agent, "_settle") as settle:
            self.assertTrue(agent._open_status("CASE001", None))

        agent.page.goto.assert_called_once_with(
            "https://www.amazon.in/spr/returns/status?orderId=123",
            wait_until="domcontentloaded",
        )
        settle.assert_called_once_with(2000)

    def test_status_page_replacement_details_are_written_to_xlsx(self):
        timeline = extract_timeline_dates(
            "Replacement requested on 16 February 2026\n"
            "Replacement complete on 20 February 2026\n"
            "Replacement window closes on 25 February 2026"
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            table = Path(temp_dir) / "orders.xlsx"
            write_table(pd.DataFrame([{"case id": "CASE001"}]), table)
            update_status(
                table,
                "CASE001",
                replacement_initialization=timeline["replacement_initialization"],
                replacement_date=timeline["replacement_date"],
                replacement_window=timeline["replacement_window"],
            )

            row = read_table(table).iloc[0]

        self.assertEqual(row["replacement_initialization"], "16 February 2026")
        self.assertEqual(row["replacement_date"], "20 February 2026")
        self.assertEqual(row["replacement_window"], "25 February 2026")


if __name__ == "__main__":
    unittest.main()
