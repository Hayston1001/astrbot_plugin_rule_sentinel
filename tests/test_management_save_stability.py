import importlib.util
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from test_management_quick_start import Handler


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright is unavailable")
class ManagementSaveStabilityTests(unittest.TestCase):
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
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/?scenario=save-stability"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page(viewport={"width": 1100, "height": 750})
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(f"{error}\n{error.stack}"))
        self.page.goto(self.url)
        self.page.wait_for_selector(".rule-card")

    def tearDown(self):
        self.page.close()
        self.assertEqual(self.errors, [])

    def select_group(self, group_id):
        self.page.evaluate("""groupId => {
          const select = document.querySelector('#group-select');
          select.value = groupId ? JSON.stringify(['p', groupId]) : '';
          select.dispatchEvent(new Event('change', {bubbles: true}));
        }""", group_id)

    def test_rule_buttons_and_refresh_remain_stable_while_saving(self):
        from playwright.sync_api import expect

        page = self.page
        card = page.locator(".rule-card").first
        self.assertEqual(card.locator(":scope > button").all_text_contents(), ["历史版本", "编辑"])
        card.get_by_role("button", name="编辑", exact=True).click()
        expect(card.get_by_role("button", name="收起", exact=True)).to_be_visible()
        page.wait_for_timeout(450)
        page.locator("#text").fill("updated rule")
        page.evaluate("""() => {
          window.originalRuleCard = document.querySelector('.rule-card');
          window.originalRuleEditor = document.querySelector('#rule-form');
          window.quickStartMock.holdNextRuleRead();
          window.ruleSaveFrames = [];
          window.captureRuleFrames = true;
          function capture() {
            const card = document.querySelector('.rule-card');
            const editor = document.querySelector('#rule-form');
            const actions = [...document.querySelectorAll('#action-list .form-panel')];
            window.ruleSaveFrames.push({card: !!card, editor: !!card && card.contains(editor) && !editor.hidden,
              actionOpacity: actions.map((action) => Number(getComputedStyle(action).opacity))});
            if (window.captureRuleFrames) requestAnimationFrame(capture);
          }
          requestAnimationFrame(capture);
        }""")
        page.locator("#preview").click()
        page.wait_for_function("window.quickStartMock.ruleReadStarted")
        self.assertTrue(page.evaluate("window.originalRuleCard.isConnected && window.originalRuleCard.contains(window.originalRuleEditor)"))
        expect(page.locator("#rule-form")).to_be_visible()
        expect(page.locator("#current-rules")).not_to_contain_text("正在读取规则")
        page.locator("#text").fill("next unsaved draft")
        page.locator("#text").focus()
        page.locator("#text").evaluate("field => field.setSelectionRange(5, 13)")
        top_before = page.locator("#rule-form").evaluate("form => form.getBoundingClientRect().top")
        page.evaluate("window.quickStartMock.releaseRuleRead()")
        expect(page.locator(".rule-version")).to_have_text("当前版本: 第 2 版")
        expect(page.locator("#rule-form")).to_be_visible()
        expect(page.locator("#text")).to_have_value("next unsaved draft")
        expect(page.locator("#text")).to_be_focused()
        self.assertEqual(page.locator("#text").evaluate("field => [field.selectionStart, field.selectionEnd]"), [5, 13])
        top_after = page.locator("#rule-form").evaluate("form => form.getBoundingClientRect().top")
        self.assertLessEqual(abs(top_after - top_before), 2)
        expect(page.locator("#feedback-region")).to_contain_text("新修改尚未保存")
        page.wait_for_timeout(350)
        frames = page.evaluate("window.captureRuleFrames = false; window.ruleSaveFrames")
        self.assertTrue(frames)
        self.assertTrue(all(frame["card"] and frame["editor"] for frame in frames), frames)
        self.assertTrue(all(len(frame["actionOpacity"]) == 2 and all(value > .99 for value in frame["actionOpacity"])
                            for frame in frames), frames)
        page.locator(".rule-card > .rule-edit").click()
        expect(page.locator("#rule-form")).to_be_hidden()
        page.locator(".rule-card > .rule-edit").click()
        expect(page.locator("#text")).to_have_value("updated rule")

    def test_editing_during_confirmation_keeps_the_new_draft_and_version(self):
        from playwright.sync_api import expect

        page = self.page
        page.locator(".rule-edit").click()
        page.locator("#text").fill("first saved edit")
        page.evaluate("window.quickStartMock.holdNextRuleConfirm()")
        page.locator("#preview").click()
        page.wait_for_function("window.quickStartMock.ruleConfirmStarted")
        page.locator("#text").fill("second unsaved edit")
        page.evaluate("window.quickStartMock.releaseRuleConfirm()")
        expect(page.locator(".rule-version")).to_have_text("当前版本: 第 2 版")
        expect(page.locator("#version")).to_have_value("2")
        expect(page.locator("#text")).to_have_value("second unsaved edit")
        expect(page.locator("#feedback-region")).to_contain_text("新修改尚未保存")
        expect(page.locator("#preview")).to_be_enabled()
        page.locator("#preview").click()
        expect(page.locator(".rule-version")).to_have_text("当前版本: 第 3 版")
        self.assertEqual(page.evaluate("window.quickStartMock.rules.find(rule => rule.rule_id === 'rule-a').text"), "second unsaved edit")

    def test_all_groups_refresh_and_switch_use_the_right_scope(self):
        from playwright.sync_api import expect

        page = self.page
        self.select_group("")
        expect(page.locator(".rule-group")).to_have_count(2)
        other = page.locator(".rule-group").filter(has_text="另一个群")
        other.locator(".rule-edit").click()
        page.locator("#text").fill("saved in other group")
        page.evaluate("""() => {
          window.originalGroupRule = document.querySelectorAll('.rule-group')[1].querySelector('.rule-card');
          window.quickStartMock.holdNextRuleRead();
        }""")
        page.locator("#preview").click()
        page.wait_for_function("window.quickStartMock.ruleReadStarted")
        self.assertTrue(page.evaluate("window.originalGroupRule.isConnected && window.originalGroupRule.contains(document.querySelector('#rule-form'))"))
        expect(page.locator(".rule-group")).to_have_count(2)
        page.evaluate("window.quickStartMock.releaseRuleRead()")
        expect(other.locator(".rule-version")).to_have_text("当前版本: 第 2 版")
        expect(other.locator("#rule-form")).to_be_visible()
        expect(page.locator("#text")).to_have_value("saved in other group")

        page.locator("#text").fill("unsaved other group draft")
        page.evaluate("window.quickStartMock.holdNextRuleRead()")
        self.select_group("g")
        page.wait_for_function("window.quickStartMock.ruleReadStarted")
        expect(page.locator("#current-rules")).to_contain_text("正在读取规则")
        expect(page.locator(".rule-card")).to_have_count(0)
        expect(page.locator("#save-status")).to_contain_text("返回原群")
        page.evaluate("window.quickStartMock.releaseRuleRead()")
        expect(page.locator(".rule-card strong").first).to_have_text("rule-a")
        expect(page.locator("#text")).to_have_value("unsaved other group draft")
        self.select_group("other")
        expect(page.locator(".rule-card strong").first).to_have_text("rule-b")
        expect(page.locator(".rule-card #rule-form")).to_be_visible()
        expect(page.locator("#text")).to_have_value("unsaved other group draft")
        page.locator(".rule-edit").click()
        page.evaluate("""() => window.quickStartMock.rules.push({platform_id: 'p', group_id: 'g',
          rule_id: 'rule-c', version: 1, enabled: true, text: 'another rule', priority: 0, actions: []})""")
        self.select_group("")
        first = page.locator(".rule-group").filter(has_text="测试群")
        expect(first.locator(".rule-card")).to_have_count(2)
        first.locator(".rule-edit").first.click()
        page.locator("#text").fill("updated first group rule")
        page.evaluate("window.quickStartMock.holdNextRuleRead()")
        page.locator("#preview").click()
        page.wait_for_function("window.quickStartMock.ruleReadStarted")
        page.evaluate("window.quickStartMock.releaseRuleRead()")
        expect(first.locator(".rule-version").first).to_have_text("当前版本: 第 2 版")
        expect(first.locator(".rule-card").first.locator("#rule-form")).to_be_visible()
        expect(first.locator(".rule-card")).to_have_count(2)
        expect(page.locator("#text")).to_have_value("updated first group rule")

    def test_toggle_and_failed_saves_preserve_the_editor(self):
        from playwright.sync_api import expect

        page = self.page
        page.locator(".rule-edit").click()
        page.locator("#text").fill("unsaved draft")
        page.evaluate("window.quickStartMock.holdNextRuleRead()")
        page.locator(".rule-state-toggle input").uncheck()
        page.wait_for_function("window.quickStartMock.ruleReadStarted")
        expect(page.locator(".rule-card #rule-form")).to_be_visible()
        expect(page.locator("#text")).to_have_value("unsaved draft")
        page.evaluate("window.quickStartMock.releaseRuleRead()")
        expect(page.locator(".rule-version")).to_have_text("当前版本: 第 2 版")
        expect(page.locator("#version")).to_have_value("2")
        expect(page.locator("#text")).to_have_value("unsaved draft")

        page.evaluate("window.quickStartMock.failNextRuleConfirm = true")
        page.locator("#preview").click()
        expect(page.locator("#feedback-region")).to_contain_text("version conflict")
        expect(page.locator(".rule-card #rule-form")).to_be_visible()
        expect(page.locator("#text")).to_have_value("unsaved draft")
        expect(page.locator("#version")).to_have_value("2")

        page.evaluate("window.quickStartMock.failNextRuleRead = true")
        page.locator("#preview").click()
        expect(page.locator("#current-rules")).to_contain_text("rule read unavailable")
        expect(page.locator("#rule-form")).to_be_visible()
        expect(page.locator("#text")).to_have_value("unsaved draft")
        expect(page.locator("#version")).to_have_value("3")
        self.select_group("other")
        expect(page.locator(".rule-card strong").first).to_have_text("rule-b")
        self.select_group("g")
        expect(page.locator(".rule-card #rule-form")).to_be_visible()
        expect(page.locator("#text")).to_have_value("unsaved draft")
        page.evaluate("window.quickStartMock.malformedNextRuleRead = true")
        page.locator("#preview").click()
        expect(page.locator("#current-rules")).to_contain_text("操作失败")
        expect(page.locator("#rule-form")).to_be_visible()
        expect(page.locator("#text")).to_have_value("unsaved draft")
        expect(page.locator("#version")).to_have_value("4")

    def test_rule_buttons_fit_and_remain_ordered_across_themes_and_widths(self):
        from playwright.sync_api import expect

        page = self.page
        for width in (1100, 390, 320):
            for theme in ("light", "dark"):
                with self.subTest(width=width, theme=theme):
                    page.set_viewport_size({"width": width, "height": 750})
                    page.emulate_media(color_scheme=theme)
                    card = page.locator(".rule-card")
                    buttons = card.locator(":scope > button")
                    self.assertEqual(buttons.all_text_contents(), ["历史版本", "编辑"])
                    history, edit = [buttons.nth(index).bounding_box() for index in range(2)]
                    self.assertLess(history["x"], edit["x"])
                    self.assertLessEqual(edit["x"] + edit["width"], width)
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                    buttons.nth(1).click()
                    expect(buttons.nth(1)).to_have_text("收起")
                    page.locator("#text").fill(f"draft {width} {theme}")
                    buttons.nth(1).click()
                    buttons.nth(1).click()
                    expect(page.locator("#text")).to_have_value("original rule")
                    buttons.nth(1).click()

    def test_group_and_global_saves_keep_their_forms_in_place(self):
        from playwright.sync_api import expect

        page = self.page
        page.locator("#tab-groups").click()
        page.locator(".group-row").first.get_by_role("button", name="配置", exact=True).click()
        expect(page.locator("#group-detail-panel")).to_be_visible()
        page.locator("#group-alias").fill("renamed group")
        page.evaluate("""() => {
          window.originalGroupEditor = document.querySelector('#group-detail-panel');
          window.originalGroupCard = document.querySelector('.group-row');
        }""")
        page.locator("#save-group-alias").click()
        expect(page.locator(".group-row strong").first).to_have_text("renamed group")
        self.assertTrue(page.evaluate("window.originalGroupCard.isConnected && window.originalGroupCard.contains(window.originalGroupEditor)"))
        page.locator("#group-reason-language-mode + .custom-select-trigger").click()
        page.get_by_role("option", name="本群单独设置", exact=True).click()
        page.locator("#group-reason-language").fill("中文")
        page.wait_for_timeout(300)
        page.evaluate("""() => {
          window.groupSaveFrames = [];
          window.captureGroupFrames = true;
          function capture() {
            const panel = window.originalGroupEditor;
            window.groupSaveFrames.push({connected: panel.isConnected && window.originalGroupCard.contains(panel),
              opacity: Number(getComputedStyle(panel).opacity)});
            if (window.captureGroupFrames) requestAnimationFrame(capture);
          }
          requestAnimationFrame(capture);
        }""")
        page.locator("#group-settings-save").click()
        expect(page.locator("#group-settings-status")).not_to_contain_text("尚未保存")
        self.assertTrue(page.evaluate("window.originalGroupCard.isConnected && window.originalGroupCard.contains(window.originalGroupEditor)"))
        expect(page.locator("#group-reason-language")).to_have_value("中文")
        page.wait_for_timeout(300)
        frames = page.evaluate("window.captureGroupFrames = false; window.groupSaveFrames")
        self.assertTrue(frames)
        self.assertTrue(all(frame["connected"] and frame["opacity"] > .99 for frame in frames), frames)

        page.locator("#tab-settings").click()
        page.locator("#setting-llm_reason_language").fill("中文")
        page.evaluate("""() => {
          window.originalSettingsField = document.querySelector('#setting-llm_reason_language');
          window.settingsSaveFrames = [];
          window.captureSettingsFrames = true;
          function capture() {
            const field = window.originalSettingsField;
            window.settingsSaveFrames.push({connected: field.isConnected,
              visible: field.getClientRects().length > 0 && Number(getComputedStyle(field).opacity) > .99});
            if (window.captureSettingsFrames) requestAnimationFrame(capture);
          }
          requestAnimationFrame(capture);
        }""")
        page.locator("#settings-save").click()
        expect(page.locator("#feedback-region")).to_contain_text("设置已保存")
        self.assertTrue(page.evaluate("window.originalSettingsField.isConnected"))
        expect(page.locator("#setting-llm_reason_language")).to_have_value("中文")
        page.wait_for_timeout(300)
        frames = page.evaluate("window.captureSettingsFrames = false; window.settingsSaveFrames")
        self.assertTrue(frames)
        self.assertTrue(all(frame["connected"] and frame["visible"] for frame in frames), frames)


if __name__ == "__main__":
    unittest.main()
