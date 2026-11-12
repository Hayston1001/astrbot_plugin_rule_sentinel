import importlib.util
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
VIEW = ROOT / "views" / "management"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            html = (VIEW / "index.html").read_text(encoding="utf-8")
            body = html.replace('<script type="module" src="./select.js">',
                                '<script src="/fixture.js"></script><script type="module" src="./select.js">').encode("utf-8")
            content_type = "text/html; charset=utf-8"
        elif path == "/fixture.js":
            body = (ROOT / "tests" / "management_quick_start_fixture.js").read_bytes()
            content_type = "text/javascript; charset=utf-8"
        elif path in ("/app.js", "/select.js", "/motion.js", "/style.css"):
            body = (VIEW / path[1:]).read_bytes()
            content_type = "text/css; charset=utf-8" if path.endswith(".css") else "text/javascript; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright is unavailable")
class ManagementQuickStartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls.playwright = sync_playwright().start()
        if not Path(cls.playwright.chromium.executable_path).is_file():
            cls.playwright.stop()
            raise unittest.SkipTest("Playwright Chromium is unavailable")
        cls.browser = cls.playwright.chromium.launch(headless=True)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page(viewport={"width": 1200, "height": 850}, reduced_motion="reduce")
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def tearDown(self):
        self.page.close()
        self.assertEqual(self.errors, [])

    def open_status(self, scenario="empty"):
        self.page.goto(self.url + "?scenario=" + scenario)
        self.page.locator("#tab-status").click()
        self.page.wait_for_function("document.querySelector('#quick-start-progress').textContent !== '正在读取进度...'")

    def test_guided_setup_tracks_saved_changes_and_opens_destinations(self):
        from playwright.sync_api import expect

        page = self.page
        self.open_status()
        expect(page.locator("#quick-start")).to_be_visible()
        expect(page.locator("#quick-start-progress")).to_have_text("已完成 0 / 3")
        page.locator("#quick-start-rule").click()
        expect(page.locator("#tab-rules")).to_have_attribute("aria-selected", "true")
        expect(page.locator("#new-rule")).to_be_disabled()
        page.locator("#tab-status").click()
        page.locator("#quick-start-model").click()
        expect(page.locator("#tab-settings")).to_have_attribute("aria-selected", "true")
        expect(page.locator("#setting-decision_backend + .custom-select-trigger")).to_be_focused()
        page.locator("#setting-llm_provider_id").fill("model-a")
        page.locator("#tab-status").click()
        expect(page.locator("#quick-start-model-state")).to_have_text("未完成")
        page.locator("#quick-start-model").click()
        expect(page.locator("#setting-llm_provider_id")).to_have_value("model-a")
        page.locator("#settings-save").click()
        page.locator("#tab-status").click()
        expect(page.locator("#quick-start-model-state")).to_have_text("已完成")
        expect(page.locator("#quick-start-progress")).to_have_text("已完成 1 / 3")

        page.locator("#quick-start-group").click()
        expect(page.locator("#tab-groups")).to_have_attribute("aria-selected", "true")
        expect(page.locator("#add-group-panel")).to_have_attribute("open", "")
        page.locator("#available-group-select + .custom-select-trigger").click()
        page.get_by_role("option", name="测试群", exact=True).click()
        page.locator("#add-group").click()
        expect(page.locator("#group-detail-panel")).to_be_visible()
        expect(page.locator("#monitor-group")).to_be_focused()
        page.locator("#tab-status").click()
        expect(page.locator("#quick-start-group-state")).to_have_text("未完成")
        page.locator("#tab-groups").click()
        page.locator("#monitor-group").check()
        page.locator("#tab-status").click()
        expect(page.locator("#quick-start-progress")).to_have_text("已完成 2 / 3")

        page.locator("#quick-start-rule").click()
        expect(page.locator("#tab-rules")).to_have_attribute("aria-selected", "true")
        expect(page.locator("#rule-form")).to_be_visible()
        expect(page.locator("#rule")).to_be_focused()
        page.locator("#rule").fill("first-rule")
        page.locator("#text").fill("不要发送广告")
        page.locator("#tab-status").click()
        expect(page.locator("#quick-start-rule-state")).to_have_text("未完成")
        page.locator("#quick-start-rule").click()
        expect(page.locator("#rule")).to_have_value("first-rule")
        expect(page.locator("#text")).to_have_value("不要发送广告")
        page.locator("#preview").click()
        page.locator("#tab-status").click()
        expect(page.locator("#quick-start")).to_be_hidden()
        page.locator("#tab-groups").click()
        page.locator("#monitor-group").uncheck()
        page.locator("#tab-status").click()
        expect(page.locator("#quick-start")).to_be_hidden()
        page.reload()
        page.locator("#tab-status").click()
        expect(page.locator("#quick-start")).to_be_hidden()

    def test_completion_is_global_and_model_types_are_checked(self):
        from playwright.sync_api import expect

        for scenario in ("complete", "blocklist", "jev-ready", "scope"):
            with self.subTest(scenario=scenario):
                self.open_status(scenario)
                expect(self.page.locator("#quick-start")).to_be_hidden()
        self.open_status("jev")
        expect(self.page.locator("#quick-start")).to_be_visible()
        expect(self.page.locator("#quick-start-model-state")).to_have_text("未完成")
        expect(self.page.locator("#quick-start-progress")).to_have_text("已完成 2 / 3")
        self.open_status("stale-group")
        expect(self.page.locator("#quick-start-group-state")).to_have_text("未完成")
        expect(self.page.locator("#quick-start-rule-state")).to_have_text("未完成")

    def test_failed_reads_do_not_count_as_completed(self):
        from playwright.sync_api import expect

        for scenario, state in (("groups-error", "group"), ("settings-error", "model")):
            with self.subTest(scenario=scenario):
                self.open_status(scenario)
                expect(self.page.locator("#quick-start")).to_be_visible()
                expect(self.page.locator(f"#quick-start-{state}-state")).to_have_text("状态不可用")
                expect(self.page.locator("#quick-start-progress")).to_have_text("部分进度不可用")
        self.open_status("state-error")
        expect(self.page.locator("#quick-start")).to_be_visible()
        expect(self.page.locator("#quick-start-progress")).to_have_text("无法读取进度, 请刷新页面重试.")
        self.assertNotIn("quick-start/complete", self.page.evaluate("window.quickStartMock.requests"))
        self.open_status("save-error")
        expect(self.page.locator("#quick-start")).to_be_visible()
        expect(self.page.locator("#quick-start-progress")).to_have_text("无法保存进度, 请刷新页面重试.")
        self.assertEqual(self.page.evaluate("window.quickStartMock.requests.filter(x => x === 'quick-start/complete').length"), 1)

    def test_layout_and_controls_remain_usable_in_both_themes(self):
        from playwright.sync_api import expect

        for width in (1440, 768, 390, 320):
            for theme in ("light", "dark"):
                with self.subTest(width=width, theme=theme):
                    self.page.set_viewport_size({"width": width, "height": 850})
                    self.page.emulate_media(color_scheme=theme)
                    self.open_status()
                    expect(self.page.locator("#quick-start")).to_be_visible()
                    for control in ("model", "group", "rule"):
                        expect(self.page.locator(f"#quick-start-{control}")).to_be_visible()
                    self.assertFalse(self.page.evaluate("document.documentElement.scrollWidth > innerWidth"))


if __name__ == "__main__" and "--serve" in sys.argv:
    ThreadingHTTPServer(("127.0.0.1", 8876), Handler).serve_forever()
elif __name__ == "__main__":
    unittest.main()
