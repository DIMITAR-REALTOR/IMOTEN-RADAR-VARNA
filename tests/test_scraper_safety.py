import csv
import os
import tempfile
import unittest
from datetime import date, timedelta

import pandas as pd

from scraper_safety import (
    apply_manual_corrections,
    atomic_write_csv,
    build_report,
    cleanup_expired_photos,
    record_page,
    record_pass_start,
    record_pass_stop,
    record_parse_error,
    record_timeout,
    save_manual_correction,
    stable_listing_id,
    update_site,
    validate_snapshot,
)


class ScraperSafetyTests(unittest.TestCase):
    def base_df(self, count=100, site="https://www.olx.bg/ad/1"):
        return pd.DataFrame([{
            "Тип_имот": "друго",
            "Тип_продавач": "частно лице",
            "Тип_сделка": "продажба",
            "Локация-район": "Център",
            "Площ_квм": 50,
            "Цена": 100000,
            "Цена_кв.м": 2000,
            "Телефон": "",
            "Описание": "описание",
            "Източник": site,
            "Снимка_URL": "",
            "Снимка_локална": "",
            "Дата_обява": "",
            "Спешност": "NO",
            "Ново_строителство": "",
            "От_инвеститор": "",
            "Неопределено": "YES",
            "Класификация_сигурна": "",
            "ID_обява": "abc123",
        } for _ in range(count)])

    def test_disabled_site_is_not_error(self):
        report = build_report({"olx.bg": False})
        df = pd.DataFrame([{
            "Източник": "https://www.alo.bg/ad/1",
        }])
        df = pd.concat([df, pd.DataFrame(columns=df.columns)], ignore_index=True)
        errors = validate_snapshot(df, report, {"olx.bg": False})
        self.assertFalse(any("олх" in error.lower() or "olx" in error.lower() for error in errors))

    def test_confirmed_empty_included_site(self):
        report = build_report({"alo.bg": True})
        df = self.base_df(site="https://www.olx.bg/ad/1")
        errors = validate_snapshot(df, report, {"alo.bg": True})
        self.assertTrue(any("ало" in error.lower() or "alo.bg" in error.lower() for error in errors))

    def test_timeout_is_validation_error(self):
        report = build_report({"olx.bg": True})
        update_site(report, "olx.bg", "timeout", reason="timeout при зареждане")
        errors = validate_snapshot(self.base_df(100), report, {"olx.bg": True})
        self.assertTrue(any("timeout" in error.lower() or "olx.bg" in error.lower() for error in errors))

    def test_page_limit_is_validation_error(self):
        report = build_report({"imot.bg": True})
        update_site(report, "imot.bg", "успешен", limit_reached=True)
        errors = validate_snapshot(self.base_df(100), report, {"imot.bg": True})
        self.assertTrue(any("лимит" in error.lower() for error in errors))

    def test_drop_is_validation_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            previous = os.path.join(temp_dir, "previous.json")
            with open(previous, "w", encoding="utf-8") as handle:
                handle.write('{"listings": 1000}')
            report = build_report({"olx.bg": True})
            errors = validate_snapshot(
                self.base_df(600),
                report,
                {"olx.bg": True},
                previous_full_path=previous,
            )
        self.assertTrue(any("Рязък спад" in error for error in errors))

    def test_successful_atomic_publish_does_not_change_failed_destination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = os.path.join(temp_dir, "snapshot.csv")
            df = self.base_df(100)
            atomic_write_csv(df, output)
            with open(output, "rb") as handle:
                before = handle.read()
            report = build_report({"olx.bg": True})
            errors = validate_snapshot(df, report, {"olx.bg": True})
            self.assertEqual(errors, [])
            with open(output, "rb") as handle:
                after = handle.read()
            self.assertEqual(before, after)

    def test_failed_validation_preserves_previous_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = os.path.join(temp_dir, "snapshot.csv")
            previous = self.base_df(100)
            atomic_write_csv(previous, output)
            with open(output, "rb") as handle:
                before = handle.read()
            report = build_report({"olx.bg": True})
            update_site(report, "olx.bg", "грешка", reason="parse failure")
            bad = pd.DataFrame(columns=previous.columns)
            errors = validate_snapshot(bad, report, {"olx.bg": True})
            self.assertTrue(errors)
            with open(output, "rb") as handle:
                after = handle.read()
            self.assertEqual(before, after)

    def test_active_photo_is_not_deleted_under_retention(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            photos = os.path.join(temp_dir, "photos")
            history = os.path.join(temp_dir, "history")
            os.makedirs(photos)
            os.makedirs(history)
            photo = os.path.join(photos, "active.jpg")
            with open(photo, "wb") as handle:
                handle.write(b"photo")
            today = date(2026, 11, 22)
            row = {"ID_обява": "active", "Източник": "", "Снимка_локална": photo}
            df = pd.DataFrame([row])
            fields = ["ID_обява", "Източник", "Снимка_локална"]
            with open(os.path.join(history, "properties_varna_2026-11-20.csv"), "w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow(row)
            stats = cleanup_expired_photos(df, photos, history, True, [], 14, today)
            self.assertTrue(os.path.exists(photo))
            self.assertEqual(stats["deleted"], 0)

    def test_photos_are_deleted_only_over_14_days_and_listings_remain(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            photos = os.path.join(temp_dir, "photos")
            history = os.path.join(temp_dir, "history")
            os.makedirs(photos)
            os.makedirs(history)
            old = os.path.join(photos, "old.jpg")
            with open(old, "wb") as handle:
                handle.write(b"photo")
            today = date(2026, 11, 22)
            row = {"ID_обява": "old", "Източник": "https://example.test/old", "Снимка_локална": old}
            fields = ["ID_обява", "Източник", "Снимка_локална"]
            old_date = (today - timedelta(days=15)).isoformat()
            with open(os.path.join(history, f"properties_varna_{old_date}.csv"), "w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow(row)
            df = pd.DataFrame([{"ID_обява": "different", "Източник": "https://example.test/new"}])
            stats = cleanup_expired_photos(df, photos, history, True, [], 14, today)
            self.assertFalse(os.path.exists(old))
            self.assertEqual(stats["deleted"], 1)

    def test_manual_correction_is_saved_and_applied(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "corrections.csv")
            df = self.base_df(1)
            listing_id = stable_listing_id(df.iloc[0])
            save_manual_correction(path, listing_id, "друго", "апартамент",
                                   "описанието показва студио",
                                   df.iloc[0]["Източник"])
            df = apply_manual_corrections(df, path)
            self.assertEqual(df.iloc[0]["Тип_имот"], "апартамент")
            self.assertEqual(df.iloc[0]["Тип_преди"], "друго")

    def test_listing_id_matches_legacy_history_and_is_source_scoped(self):
        old_history_row = {"Източник": "https://www.olx.bg/ad/123?utm_source=old"}
        current_row = {"ID_обява": "123", "Източник": "https://www.olx.bg/ad/123"}
        other_site_row = {"ID_обява": "123", "Източник": "https://www.alo.bg/ad/123"}
        self.assertEqual(stable_listing_id(old_history_row), stable_listing_id(current_row))
        self.assertNotEqual(stable_listing_id(current_row), stable_listing_id(other_site_row))

    def test_active_listing_photo_survives_legacy_history_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            photos = os.path.join(temp_dir, "photos")
            history = os.path.join(temp_dir, "history")
            os.makedirs(photos)
            os.makedirs(history)
            photo = os.path.join(photos, "still-active.jpg")
            with open(photo, "wb") as handle:
                handle.write(b"photo")
            old_row = {
                "Източник": "https://www.olx.bg/ad/123",
                "Снимка_локална": photo,
            }
            old_date = date(2026, 11, 22) - timedelta(days=30)
            with open(os.path.join(history, f"properties_varna_{old_date}.csv"),
                      "w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(old_row))
                writer.writeheader()
                writer.writerow(old_row)
            current = pd.DataFrame([{
                "ID_обява": "a-different-source-local-id",
                "Източник": "https://www.olx.bg/ad/123?ref=latest",
            }])
            cleanup_expired_photos(current, photos, history, True, [], 14,
                                   date(2026, 11, 22))
            self.assertTrue(os.path.exists(photo))

    def test_page_limit_and_real_timeout_are_validation_errors(self):
        for event in ("limit", "timeout", "parse_error"):
            with self.subTest(event=event):
                report = build_report({"olx.bg": True})
                if event == "limit":
                    record_page(report, "olx.bg", "продажба/private", 20, 20, True)
                elif event == "timeout":
                    record_timeout(report, "olx.bg", "продажба/private", "timeout")
                else:
                    record_parse_error(report, "olx.bg", "продажба/private")
                errors = validate_snapshot(self.base_df(100), report,
                                           {"olx.bg": True})
                self.assertTrue(errors)

    def test_report_tracks_pages_stop_reason_and_duration_per_search(self):
        report = build_report({"olx.bg": True})
        record_pass_start(report, "olx.bg", "продажба/частно лице")
        record_page(report, "olx.bg", "продажба/частно лице", 20, 20, True)
        record_pass_stop(report, "olx.bg", "продажба/частно лице",
                         "page_limit_with_results")
        pass_report = report["sites"]["olx.bg"]["passes"]["продажба/частно лице"]
        self.assertEqual(pass_report["pages"], 1)
        self.assertEqual(pass_report["stop_reason"], "page_limit_with_results")
        self.assertGreaterEqual(pass_report["duration_seconds"], 0)

    def test_incomplete_snapshot_never_cleans_photos(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            photo = os.path.join(temp_dir, "photo.jpg")
            with open(photo, "wb") as handle:
                handle.write(b"photo")
            df = pd.DataFrame([{"ID_обява": "different"}])
            stats = cleanup_expired_photos(
                df,
                photos_dir=temp_dir,
                history_dir=temp_dir,
                full_snapshot=False,
                report_errors=["page limit reached"],
            )
            self.assertTrue(os.path.exists(photo))
            self.assertEqual(stats["deleted"], 0)

    def test_uncertain_view_is_present_in_dashboard_shell(self):
        shell = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "radar_shell.html")
        with open(shell, encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn("За уточняване", html)
        self.assertIn("r.uncertain", html)


if __name__ == "__main__":
    unittest.main()
