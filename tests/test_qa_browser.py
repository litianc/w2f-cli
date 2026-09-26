"""Safety/classification tests; real browser scenarios run through the QA runner."""
import unittest
from unittest.mock import MagicMock, patch

from qa.browser_check import CHECKS, run_checks
from qa.browser_harness import BoundaryBlocked, isolated_manager


class BrowserHarnessTests(unittest.TestCase):
    def test_missing_browser_library_never_becomes_product_bug(self):
        with patch("qa.browser_check._load_playwright", side_effect=ImportError("missing")):
            results = run_checks()
        self.assertEqual(len(results), len(CHECKS))
        self.assertTrue(all(result["status"] == "blocked" for result in results))

    def test_chromium_startup_error_never_becomes_product_bug(self):
        driver = MagicMock()
        driver.return_value.__enter__.return_value.chromium.launch.side_effect = RuntimeError("no browser")
        with patch("qa.browser_check._load_playwright", return_value=(driver, TimeoutError)):
            results = run_checks()
        self.assertTrue(all(result["status"] == "blocked" for result in results))
        self.assertEqual(len(results), len(CHECKS))

    def test_fake_boundaries_and_temporary_database_are_cleaned_up(self):
        from agents import manage, wechat_feishu_mirror
        with isolated_manager() as fixture:
            root = fixture.root
            fixture.store.create_person("QA disposable person")
            self.assertEqual(len(fixture.store.list_people()), 1)
            self.assertEqual(manage.run_cli(["fake-reader", "contacts", "--groups-only", "--limit", "100000"])["data"]["contacts"], [])
            with self.assertRaises(BoundaryBlocked):
                manage.run_cli(["lark-cli", "im", "+messages-send"])
            with self.assertRaises(BoundaryBlocked):
                wechat_feishu_mirror.run_lark({}, ["+messages-send"])
            with self.assertRaises(BoundaryBlocked):
                manage.subprocess.run(["lark-cli", "im", "+messages-send"])
            self.assertEqual(len(fixture.violations), 3)
        self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
