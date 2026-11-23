import csv
import os
import tempfile
import unittest
from unittest.mock import patch

from scripts.sync_to_remi import (
    is_existing_url_duplicate,
    main,
    row_to_property,
    source_from_url,
)


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
            "Тип_продавач": "частно лице",
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
        self.assertEqual(prop["seller_type"], "private")
        self.assertEqual(prop["urgency"], "YES")
        self.assertTrue(prop["is_new_construction"])
        self.assertFalse(prop["is_from_investor"])
        self.assertEqual(prop["photo_url"], "https://example.com/a.jpg")
        self.assertIsNotNone(prop["listing_date"])
        for legacy in (
            "external_id", "deal_type", "urgent", "new_build",
            "from_investor", "photos", "published_at",
            "raw_location", "phones",
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

    def test_known_existing_url_conflict_is_idempotent(self):
        row = {
            "source_listing_id": "IDa5b6w",
            "reason": (
                'duplicate key value violates unique constraint '
                '"market_listings_raw_url_key" — 23505'
            ),
        }
        self.assertTrue(is_existing_url_duplicate(row))

    def test_other_ingestion_rejections_remain_errors(self):
        row = {
            "source_listing_id": "IDa5b6w",
            "reason": 'duplicate key value violates unique constraint "other_key" — 23505',
        }
        self.assertFalse(is_existing_url_duplicate(row))
        self.assertFalse(is_existing_url_duplicate({"reason": "invalid payload"}))

    def test_known_existing_url_conflict_allows_sync_to_finish(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "listings.csv")
            with open(path, "w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["Източник", "Описание"])
                writer.writeheader()
                writer.writerow({"Източник": "https://www.olx.bg/ad/IDa5b6w.html",
                                 "Описание": "Апартамент"})

            class Response:
                status_code = 200
                text = ""

                def __init__(self, data):
                    self.data = data

                def json(self):
                    return {"data": self.data}

            responses = [
                Response({
                    "received": 1,
                    "inserted": 0,
                    "updated": 0,
                    "price_changes": 0,
                    "rejected": 1,
                    "rejected_rows": [{
                        "reason": ('duplicate key value violates unique constraint '
                                   '"market_listings_raw_url_key" — 23505')
                    }],
                }),
                Response({"clients_processed": 0, "proposals_created": 0,
                          "notifications_skipped": 0}),
            ]
            with patch.dict(os.environ, {
                "REMI_API_URL": "https://remi.invalid",
                "REMI_INGESTION_SECRET": "test-secret",
                "REMI_CSV_PATH": path,
                "REMI_SKIP_MATCHING": "0",
            }), patch("scripts.sync_to_remi.post_json",
                     side_effect=responses) as post_json:
                self.assertEqual(main(), 0)
                self.assertEqual(post_json.call_count, 2)

    def test_other_rejection_does_not_call_matching(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "listings.csv")
            with open(path, "w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["Източник", "Описание"])
                writer.writeheader()
                writer.writerow({"Източник": "https://www.olx.bg/ad/IDa5b6w.html",
                                 "Описание": "Апартамент"})

            class Response:
                status_code = 200
                text = ""

                def json(self):
                    return {"data": {
                        "received": 1,
                        "inserted": 0,
                        "updated": 0,
                        "price_changes": 0,
                        "rejected": 1,
                        "rejected_rows": [{"reason": "invalid payload"}],
                    }}

            with patch.dict(os.environ, {
                "REMI_API_URL": "https://remi.invalid",
                "REMI_INGESTION_SECRET": "test-secret",
                "REMI_CSV_PATH": path,
            }), patch("scripts.sync_to_remi.post_json", return_value=Response()) as post_json:
                self.assertEqual(main(), 5)
                self.assertEqual(post_json.call_count, 1)

    def test_http_413_splits_batch_and_retries_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "listings.csv")
            with open(path, "w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["Източник", "Описание"])
                writer.writeheader()
                for number in range(3):
                    writer.writerow({
                        "Източник": f"https://www.imot.bg/obiava-{number}",
                        "Описание": f"Обява {number}",
                    })

            class Response:
                def __init__(self, status_code, data=None, text=""):
                    self.status_code = status_code
                    self.data = data or {}
                    self.text = text

                def json(self):
                    return {"data": self.data}

            def imported(count):
                return Response(200, {
                    "received": count,
                    "inserted": count,
                    "updated": 0,
                    "price_changes": 0,
                    "rejected": 0,
                })

            responses = [
                Response(413, text="FUNCTION_PAYLOAD_TOO_LARGE"),
                imported(1),
                imported(2),
                Response(200, {"clients_processed": 0, "proposals_created": 0,
                               "notifications_skipped": 0}),
            ]
            with patch.dict(os.environ, {
                "REMI_API_URL": "https://remi.invalid",
                "REMI_INGESTION_SECRET": "test-secret",
                "REMI_CSV_PATH": path,
                "REMI_SKIP_MATCHING": "0",
            }), patch("scripts.sync_to_remi.post_json",
                     side_effect=responses) as post_json:
                self.assertEqual(main(), 0)
                self.assertEqual(post_json.call_count, 4)
                sent_counts = [len(call.args[1]["properties"])
                               for call in post_json.call_args_list[:3]]
                self.assertEqual(sent_counts, [3, 1, 2])


if __name__ == "__main__":
    unittest.main()
