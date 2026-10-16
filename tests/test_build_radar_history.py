import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.build_radar import build_raw_and_meta


class ListingHistoryTests(unittest.TestCase):
    def test_monthly_snapshots_and_price_reductions_are_retained(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            history = root / "history"
            history.mkdir()
            fields = ["Тип_имот", "Тип_продавач", "Тип_сделка", "Цена", "Площ_квм", "Телефон",
                      "Описание", "Източник", "Снимка_URL", "Локация-район", "Цена_кв.м"]
            for date, price in (("2026-09-30", "100000"), ("2026-10-16", "90000")):
                with (history / f"properties_varna_{date}.csv").open("w", encoding="utf-8", newline="") as file:
                    writer = csv.DictWriter(file, fieldnames=fields)
                    writer.writeheader()
                    writer.writerow({
                        "Тип_имот": "апартамент",
                        "Тип_продавач": "частно лице",
                        "Тип_сделка": "продажба",
                        "Цена": price,
                        "Площ_квм": "50",
                        "Телефон": "",
                        "Описание": "двустаен",
                        "Източник": "https://example.test/ad",
                        "Снимка_URL": "",
                        "Локация-район": "Център",
                        "Цена_кв.м": str(int(price) / 50),
                    })

            with patch("scripts.build_radar.HIST_DIR", str(history)):
                rows, meta = build_raw_and_meta()

        self.assertEqual(meta["latest"], "2026-10-16")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reductionCount"], 1)
        self.assertEqual(rows[0]["lastReductionDate"], "2026-10-16")
        self.assertEqual(rows[0]["monthly"], [
            {"month": "2026-09", "ppm": 2000.0},
            {"month": "2026-10", "ppm": 1800.0},
        ])
        self.assertEqual(rows[0]["marketDays"], 16)


if __name__ == "__main__":
    unittest.main()
