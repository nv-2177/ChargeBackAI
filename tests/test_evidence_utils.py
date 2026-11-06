import unittest

from agents.evidence_utils import extract_timeline_dates, parse_order_text


class EvidenceUtilsTests(unittest.TestCase):
    def test_extracts_replacement_date_from_status_timeline(self):
        text = "Replacement ordered\n16 February 2026\nReplacement complete"

        dates = extract_timeline_dates(text)

        self.assertEqual(dates["replacement_initiated_date"], "16 February 2026")

    def test_extracts_replacement_date_after_status_phrase(self):
        text = "Replacement requested on February 16, 2026"

        dates = extract_timeline_dates(text)

        self.assertEqual(dates["replacement_initiated_date"], "February 16, 2026")

    def test_extracts_return_completed_date(self):
        text = "Return completed on 16/02/2026"

        dates = extract_timeline_dates(text)

        self.assertEqual(dates["return_date"], "16/02/2026")

    def test_order_summary_total_is_labeled_and_not_subtotal(self):
        text = "Item(s) Subtotal:\n₹349.00\nShipping:\n₹0.00\nTotal:\n₹354.00\nGrand Total:\n₹354.00"

        summary = parse_order_text(text)

        self.assertEqual(summary["total"], "₹354.00")


if __name__ == "__main__":
    unittest.main()
