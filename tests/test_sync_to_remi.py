import unittest

from scripts.sync_to_remi import row_to_property, source_from_url


class SyncToRemiPayloadTests(unittest.TestCase):
    def test_full_row_maps_to_remi_contract(self):
        row = {
            "ID_обява": " 12345 ",
            "Линк": "https://www.olx.bg/ad/abc",
            "Описание": "Хубав апартамент.",
            "Цена": "100000",
            "Площ_квм": "60",
            "Тип_имот": "Апартамент",
            "Тип_сделка": "Продажба",
            "Локация-район": "Левски",
            "Снимка_URL": "https://example.com/a.jpg",
            "Дата_обява": "днес",
            "Спешност": "YES",
            "Ново_строителство": "yes",
            "От_инвеститор": "no",
        }
        prop = row_to_property(row)
        self.assertEqual(prop["source_listing_id"], "12345")
        self.assertEqual(prop["source_url"], "https://www.olx.bg/ad/abc")
        self.assertEqual(prop["price_eur"], 100000)
        self.assertEqual(prop["area_sqm"], 60.0)
        self.assertEqual(prop["property_type"], "Апартамент")
        self.assertEqual(prop["transaction_type"], "sale")
        self.assertEqual(prop["urgency"], "YES")
        self.assertTrue(prop["is_new_construction"])
        self.assertFalse(prop["is_from_investor"])
        self.assertEqual(prop["photo_url"], "https://example.com/a.jpg")
        self.assertIsNotNone(prop["listing_date"])
        for legacy in (
            "external_id", "deal_type", "urgent", "new_build",
            "from_investor", "photos", "published_at",
            "seller_type", "raw_location", "phones",
            "price_currency_confidence",
        ):
            self.assertNotIn(legacy, prop)

    def test_missing_optionals_become_none(self):
        prop = row_to_property({"ID_обява": "1", "Линк": "https://www.imot.bg/x", "Цена": "", "Площ_квм": ""})
        self.assertIsNone(prop["price_eur"])
        self.assertIsNone(prop["photo_url"])
        self.assertIsNone(prop["urgency"])
        self.assertFalse(prop["is_new_construction"])
        self.assertIsNone(prop["listing_date"])

    def test_source_from_url(self):
        self.assertEqual(source_from_url("https://www.alo.bg/x"), "alo_bg")
        self.assertEqual(source_from_url("https://olx.bg/x"), "olx_bg")
        self.assertEqual(source_from_url("https://example.com/x"), "imot_bg")


if __name__ == "__main__":
    unittest.main()