import unittest
from datetime import date

from worldmodel_data.rtms import (
    PATHS,
    SOURCE_IDS,
    SOURCE_LANDING_URLS,
    normalize_item,
    parse_xml_items,
    rolling_months,
)


SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<response><header><resultCode>000</resultCode><resultMsg>OK</resultMsg></header>
<body><items><item><umdNm>신림동</umdNm><dealYear>2026</dealYear><dealMonth>8</dealMonth>
<dealDay>14</dealDay><deposit>1,000</deposit><monthlyRent>55</monthlyRent>
<excluUseAr>19.8</excluUseAr><floor>3</floor></item></items><totalCount>1</totalCount></body></response>"""


class RtmsTests(unittest.TestCase):
    def test_parses_and_normalizes_xml(self):
        items, total, _ = parse_xml_items(SAMPLE)
        self.assertEqual(total, 1)
        row = normalize_item("single_multi", items[0], "11620")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["deal_date"], "2026-08-14")
        self.assertEqual(row["deposit_manwon"], 1000)
        self.assertEqual(row["monthly_rent_manwon"], 55)
        self.assertEqual(row["lease_type"], "monthly")

    def test_rolling_months_crosses_year(self):
        self.assertEqual(rolling_months(3, date(2026, 1, 20)), ["202601", "202512", "202511"])

    def test_rejects_unbounded_month_count(self):
        with self.assertRaises(ValueError):
            rolling_months(0)
        with self.assertRaises(ValueError):
            rolling_months(61)

    def test_identity_preserves_source_and_row_multiplicity(self):
        items, _, _ = parse_xml_items(SAMPLE)
        apartment = normalize_item("apartment", items[0], "11620", 0)
        same_after_repagination = normalize_item("apartment", dict(items[0]), "11620", 0)
        officetel = normalize_item("officetel", items[0], "11620", 0)
        second_contract = normalize_item("apartment", items[0], "11620", 1)
        assert apartment and same_after_repagination and officetel and second_contract
        self.assertEqual(apartment["id"], same_after_repagination["id"])
        self.assertNotEqual(apartment["id"], officetel["id"])
        self.assertNotEqual(apartment["id"], second_contract["id"])

    def test_each_property_endpoint_has_independent_provenance(self):
        self.assertEqual(set(PATHS), set(SOURCE_IDS))
        self.assertEqual(set(PATHS), set(SOURCE_LANDING_URLS))
        self.assertEqual(len(set(SOURCE_IDS.values())), len(PATHS))

    def test_missing_amount_is_not_silently_treated_as_zero(self):
        items, _, _ = parse_xml_items(SAMPLE)
        items[0].pop("monthlyRent")
        self.assertIsNone(normalize_item("apartment", items[0], "11620"))


if __name__ == "__main__":
    unittest.main()
