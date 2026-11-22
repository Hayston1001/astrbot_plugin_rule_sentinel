import importlib.util
import re
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

    def test_reply_options_follow_saved_globals_and_keep_independent_overrides(self):
        from playwright.sync_api import expect

        page = self.page
        page.emulate_media(reduced_motion="reduce")
        page.evaluate("""() => {
          const api = window.AstrBotPluginView;
          const get = api.apiGet, post = api.apiPost;
          api.apiPost = async (endpoint, body) => {
            if (endpoint === 'rules/preview') window.previewedReply = body.actions[0];
            return post(endpoint, body);
          };
          api.apiGet = async (endpoint, params) => {
            const data = await get(endpoint, params);
            return endpoint === 'rules' && params.group_id === 'g'
              ? data.map(rule => ({...rule, actions: [{
                type: 'reply', mode: 'auto', template: 'please follow the rules',
              }, {type: 'recall', mode: 'auto'}]})) : data;
          };
        }""")
        page.locator("#tab-settings").click()
        quote = page.locator("#setting-reply_quote")
        mention = page.locator("#setting-reply_mention_sender")
        expect(quote).to_be_checked()
        expect(mention).not_to_be_checked()
        quote.uncheck()
        mention.check()
        page.locator("#settings-save").click()
        page.wait_for_function("window.quickStartMock.settings.reply_mention_sender")
        page.locator("#tab-rules").click()
        self.select_group("other")
        self.select_group("g")
        card = page.locator(".rule-card").filter(has_text="please follow the rules")
        card.get_by_role("button", name="编辑", exact=True).click()
        controls = page.locator(".reply-options:visible")
        expect(controls).to_have_count(1)
        expect(controls.get_by_role("combobox", name=re.compile("全局\\(不引用\\)"))).to_be_visible()
        expect(controls.get_by_role("combobox", name=re.compile("全局\\(自动 @\\)"))).to_be_visible()
        for width in (1440, 768, 390):
            page.set_viewport_size({"width": width, "height": 750})
            self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        controls.get_by_role("combobox", name=re.compile("全局\\(不引用\\)")).click()
        page.get_by_role("option", name="引用", exact=True).click()
        controls.get_by_role("combobox", name=re.compile("全局\\(自动 @\\)")).click()
        page.get_by_role("option", name="不自动 @", exact=True).click()
        page.locator("#preview").click()
        page.wait_for_function("window.previewedReply?.quote === true && window.previewedReply.mention_sender === false")

    def test_cumulative_rule_config(self):
        from playwright.sync_api import expect

        page = self.page
        page.set_viewport_size({"width": 390, "height": 750})
        page.emulate_media(color_scheme="dark", reduced_motion="reduce")
        page.evaluate("""() => {
          const api = window.AstrBotPluginView;
          const get = api.apiGet, post = api.apiPost;
          window.savedCumulative = {};
          const cumulative = {type: 'mute', mode: 'confirm', duration_mode: 'cumulative',
                              window_seconds: 3600, window_unit: 60, tiers_seconds: [60, 300], tier_units: [1, 60]};
          api.apiGet = async (endpoint, params) => endpoint === 'rules'
            ? (await get(endpoint, params)).map(rule => ({...rule, actions: [cumulative]}))
            : get(endpoint, params);
          api.apiPost = async (endpoint, params) => {
            window.savedCumulative[endpoint] = params;
            return post(endpoint, params);
          };
        }""")
        page.set_default_timeout(5000)
        self.select_group("g1")
        card = page.locator(".rule-card").filter(has_text="内累计").first
        expect(card).to_contain_text("60分钟内累计: 60秒 / 5分钟")
        card.get_by_role("button", name="编辑", exact=True).click()
        editor = page.locator(".cumulative-mute-fields")
        expect(editor).to_be_visible()
        window_value = editor.get_by_role("spinbutton", name="统计窗口", exact=True)
        window_unit = editor.get_by_role("combobox", name="统计窗口单位: 分钟", exact=True)
        expect(window_value).to_have_value("60")
        expect(window_value).to_have_accessible_description("只统计最近这段时间内计入的违规, 更早的不算.")
        help_text = editor.get_by_text("只统计最近这段时间内计入的违规, 更早的不算.", exact=True)
        tier_value = editor.get_by_role("spinbutton", name="第 1 次违规", exact=True)
        tier_unit = editor.get_by_role("combobox", name="第 1 档禁言单位: 秒", exact=True)
        fields = [node.element_handle() for node in (window_value, window_unit, help_text, tier_value, tier_unit)]
        for width, theme in ((390, "dark"), (1100, "light")):
            with self.subTest(width=width, theme=theme):
                page.set_viewport_size({"width": width, "height": 750})
                page.emulate_media(color_scheme=theme, reduced_motion="reduce")
                # Resizing can reposition the page between separate browser calls.
                value, unit, help_box, tier_amount, tier_scale = page.evaluate(
                    "nodes => nodes.map(node => node.getBoundingClientRect().toJSON())", fields
                )
                self.assertAlmostEqual(value["y"], unit["y"], delta=1)
                self.assertGreater(unit["x"], value["x"] + value["width"])
                self.assertGreater(help_box["y"], unit["y"] + unit["height"])
                self.assertLessEqual(unit["width"], 76)
                self.assertAlmostEqual(tier_amount["y"], tier_scale["y"], delta=1)
                self.assertAlmostEqual(tier_scale["width"], unit["width"], delta=1)
        window_unit.click()
        page.get_by_role("option", name="秒", exact=True).click()
        window_value.fill("900")
        editor.get_by_role("button", name="添加档位").click()
        editor.get_by_role("spinbutton", name="第 3 次违规", exact=True).fill("15")
        page.locator("#preview").click()
        page.wait_for_function("window.savedCumulative['rules/preview']?.actions[0].tiers_seconds.length === 3")
        self.assertEqual(page.evaluate("window.savedCumulative['rules/preview'].actions[0].window_seconds"), 900)
        self.assertEqual(page.evaluate("window.savedCumulative['rules/preview'].actions[0].window_unit"), 1)
        self.assertEqual(page.evaluate("window.savedCumulative['rules/preview'].actions[0].tier_units"), [1, 60, 60])

    def test_shared_count_group_editor_keeps_groups_separate(self):
        from playwright.sync_api import expect

        page = self.page
        page.set_viewport_size({"width": 390, "height": 750})
        page.emulate_media(color_scheme="dark", reduced_motion="reduce")
        page.evaluate("""() => {
          const api = window.AstrBotPluginView;
          const get = api.apiGet, post = api.apiPost;
          window.savedGroups = [];
          const action = {type: 'mute', mode: 'auto', duration_mode: 'cumulative',
                          window_seconds: 3600, tiers_seconds: [60, 300]};
          api.apiGet = async (endpoint, params) => {
            if (endpoint !== 'rules' || params.group_id !== 'g') return get(endpoint, params);
            return ['advertising', 'links', 'flood', 'spam'].map(rule_id => ({rule_id, platform_id: 'p', group_id: 'g',
              text: rule_id === 'links' ? 'Promotion links' : `Rule ${rule_id}`, version: 1, enabled: true,
              actions: [{type: 'recall', mode: 'auto'}, action]}));
          };
          api.apiPost = async (endpoint, params) => {
            if (endpoint === 'group-settings' || endpoint.startsWith('rules/')) window.savedGroups.push({endpoint, params});
            return post(endpoint, params);
          };
        }""")
        self.select_group("g")
        card = page.locator(".rule-card").filter(has_text="Rule advertising")
        card.get_by_role("button", name="编辑", exact=True).click()
        section = card.get_by_role("region", name="共享计数", exact=True)
        expect(section).to_have_count(1)
        duration = page.locator("#action-list").get_by_role("combobox", name=re.compile("按违规次数累计"))
        duration.click()
        page.get_by_role("option", name="严格映射", exact=True).click()
        expect(section).to_contain_text("选择按违规次数累计后可配置共享计数")
        expect(section.get_by_role("button", name="编辑共享")).to_have_count(0)
        page.locator("#action-list").get_by_role("combobox", name=re.compile("严格映射")).click()
        page.get_by_role("option", name="按违规次数累计", exact=True).click()
        expect(section.locator(".shared-count-summary")).to_have_text("单独计数")
        page.locator("#text").fill("尚未保存的规则正文")
        section.get_by_role("button", name="编辑共享").click()
        editor = section.locator(".shared-count-editor")
        expect(editor).to_contain_text("与当前规则成组")
        search = editor.get_by_role("searchbox")
        search.fill("Promotion")
        expect(editor.get_by_role("checkbox")).to_have_count(1)
        expect(editor.get_by_role("button", name="保存分组")).to_be_disabled()
        editor.get_by_role("checkbox", name=re.compile("links")).press("Space")
        editor.get_by_role("button", name="与当前规则成组", exact=True).press("Enter")
        editor.get_by_role("button", name="新建组").click()
        expect(editor.locator(".shared-count-destination")).to_have_text("移动目标: 新建共享组")
        expect(editor.get_by_role("button", name="保存分组")).to_be_enabled()
        search.clear()
        editor.get_by_role("checkbox", name=re.compile("flood")).check()
        expect(editor.get_by_role("button", name="用所选规则成组")).to_be_disabled()
        search.fill("spam")
        expect(editor.locator(".shared-count-selection")).to_contain_text("已选 1 条")
        editor.get_by_role("checkbox", name=re.compile("spam")).check()
        editor.get_by_role("button", name="用所选规则成组").click()
        self.assertTrue(section.evaluate("node => node.scrollWidth <= node.clientWidth"))
        editor.get_by_role("button", name="保存分组").click()
        expect(editor).to_be_hidden()
        self.assertEqual(page.evaluate("window.savedGroups"), [{"endpoint": "group-settings", "params": {
            "platform_id": "p", "group_id": "g", "expected_groups": [], "shared_violation_groups": [
                {"name": "", "rule_ids": ["advertising", "links"]},
                {"name": "", "rule_ids": ["flood", "spam"]},
            ],
        }}])
        expect(page.locator("#text")).to_have_value("尚未保存的规则正文")
        expect(section.locator(".shared-count-summary")).to_contain_text("advertising (当前)")
        expect(section.locator(".shared-count-summary")).to_contain_text("links")
        expect(section.locator(".shared-count-summary strong")).to_have_count(0)

        page.set_viewport_size({"width": 1250, "height": 1350})
        page.emulate_media(color_scheme="light", reduced_motion="reduce")
        section.get_by_role("button", name="编辑共享").click()
        board = editor.locator(".shared-count-board")
        first = board.locator(".shared-count-lane").filter(has_text="advertising (当前)")
        second = board.locator(".shared-count-lane").filter(has_text="spam")
        solo = board.locator(".shared-count-lane").filter(has_text="单独计数")
        second.get_by_role("button").press("Enter")
        expect(editor.locator(".shared-count-destination")).to_contain_text("flood / spam")
        expect(second.get_by_role("button")).to_be_focused()
        first.get_by_role("button").press("Enter")
        expect(first.get_by_role("button")).to_have_attribute("aria-pressed", "true")
        expect(editor.locator(".shared-count-destination")).to_contain_text("advertising / links")
        editor.get_by_role("button", name="新建组").click()
        first.get_by_text("links", exact=True).drag_to(board.get_by_role("button", name="移动目标: 新建共享组"))
        expect(board.get_by_text("再添加一条规则", exact=True)).to_be_visible()
        expect(editor.get_by_role("button", name="保存分组")).to_be_disabled()
        editor.get_by_role("button", name="移除未完成组").click()
        expect(first).to_contain_text("links")
        expect(editor.locator(".shared-count-impact")).to_be_hidden()
        editor.get_by_role("button", name="取消新组").click()
        second.get_by_text("flood", exact=True).drag_to(first.get_by_role("button"))
        expect(first).to_contain_text("flood")
        expect(second).to_have_count(0)
        expect(editor.get_by_role("status")).to_contain_text("已在草稿中解散")
        expect(editor.locator(".shared-count-impact")).to_have_text("保存后将单独计数: spam.")
        editor.get_by_role("button", name="撤回上一步").click()
        expect(second).to_contain_text("flood")
        expect(editor.locator(".shared-count-impact")).to_be_hidden()
        expect(editor.get_by_role("button", name="保存分组")).to_be_disabled()
        second.get_by_text("flood", exact=True).drag_to(first.get_by_role("button"))
        first.get_by_text("flood", exact=True).drag_to(solo.get_by_text("单独计数", exact=True))
        expect(first).not_to_contain_text("flood")
        expect(editor.get_by_role("checkbox", name=re.compile("flood"))).to_have_accessible_name(re.compile("单独计数"))
        editor.get_by_role("button", name="编辑组名").click()
        editor.get_by_role("textbox", name="组名 (选填)").fill("未保存的名称")
        editor.get_by_role("button", name="取消").click()
        expect(section.get_by_role("button", name="编辑共享")).to_be_focused()
        section.get_by_role("button", name="编辑共享").click()
        expect(board.get_by_text("spam", exact=True)).to_be_visible()
        expect(editor.get_by_role("textbox", name="组名 (选填)")).to_have_count(0)
        editor.get_by_role("button", name="编辑组名").click()
        expect(editor.get_by_role("textbox", name="组名 (选填)")).to_have_value("")
        self.assertEqual(page.evaluate("window.savedGroups.length"), 1)
        editor.get_by_role("textbox", name="组名 (选填)").fill("推广")
        expect(first.get_by_text("推广", exact=True)).to_be_visible()
        editor.get_by_role("button", name="保存分组").click()
        expect(section.locator(".shared-count-summary strong")).to_have_text("推广")
        self.assertEqual(page.evaluate("window.savedGroups.length"), 2)
        page.locator("#tab-groups").click()
        expect(page.locator("#panel-groups").get_by_text("共享计数", exact=True)).to_have_count(0)

    def select_group(self, group_id):
        self.page.evaluate("""groupId => {
          const select = document.querySelector('#group-select');
          select.value = groupId ? JSON.stringify(['p', groupId]) : '';
          select.dispatchEvent(new Event('change', {bubbles: true}));
        }""", group_id)

    def test_dragging_rule_action_does_not_brighten_its_border(self):
        from playwright.sync_api import expect
        page = self.page
        page.set_viewport_size({"width": 1200, "height": 1600})
        page.locator(".rule-card").first.get_by_role("button", name="编辑", exact=True).click()
        row = page.locator("#action-list .form-panel").first
        handle = row.locator(".action-drag-handle")
        other = page.locator("#action-list .form-panel").nth(1)
        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                page.emulate_media(color_scheme=theme, reduced_motion="reduce")
                handle.scroll_into_view_if_needed()
                baseline = row.evaluate("node => ({border: getComputedStyle(node).borderColor, background: getComputedStyle(node).backgroundColor})")
                badge = row.locator(".action-order-number").evaluate("node => getComputedStyle(node).backgroundColor")
                box, target = handle.bounding_box(), other.bounding_box()
                page.mouse.move(box["x"] + 5, box["y"] + 5)
                page.mouse.down()
                page.mouse.move(target["x"] + 10, target["y"] + target["height"] - 10, steps=5)
                expect(row).to_have_class(re.compile("is-dragging"))
                self.assertEqual(row.evaluate("node => getComputedStyle(node).borderColor"), baseline["border"])
                marker = page.locator(".action-drop-placeholder")
                expect(marker).to_be_visible()
                self.assertEqual(marker.evaluate("node => getComputedStyle(node).borderTopColor"), badge)
                self.assertEqual(marker.evaluate("node => getComputedStyle(node).backgroundColor"), baseline["background"])
                page.keyboard.press("Escape")
                page.mouse.up()
                self.assertNotIn("is-dragging", row.get_attribute("class"))

    def test_rule_details_are_exclusive_and_history_preserves_draft(self):
        from playwright.sync_api import expect

        page = self.page
        card = page.locator(".rule-card").first
        edit = card.locator(".rule-edit")
        history = card.get_by_role("button", name="修改记录", exact=True)
        page.evaluate("""() => {
          const api = window.AstrBotPluginView.apiGet;
          window.AstrBotPluginView.apiGet = async (endpoint, params) => endpoint === 'rules/history'
            ? [{version: 0, text: 'older rule', enabled: true, actions: []}, {version: 1, actions: []}]
            : api(endpoint, params);
          window.detailFrames = [];
          window.captureDetails = true;
          const capture = () => {
            const card = document.querySelector('.rule-card');
            const visible = node => !!node && !node.hidden && node.getBoundingClientRect().height > 1;
            window.detailFrames.push({editor: visible(card.querySelector('#rule-form')),
              history: visible(card.querySelector('.rule-history'))});
            if (window.captureDetails) requestAnimationFrame(capture);
          };
          requestAnimationFrame(capture);
        }""")
        edit.click()
        page.locator("#text").fill("unsaved draft")
        history.click()
        expect(page.locator("#rule-form")).to_be_hidden()
        expect(card.locator(".rule-history")).to_contain_text("older rule")
        expect(history).to_have_attribute("aria-expanded", "true")
        expect(edit).to_have_attribute("aria-expanded", "false")
        edit.click()
        expect(card.locator(".rule-history")).to_be_hidden()
        expect(page.locator("#rule-form")).to_be_visible()
        expect(page.locator("#text")).to_have_value("unsaved draft")
        expect(edit).to_have_text("收起")
        page.wait_for_timeout(300)
        frames = page.evaluate("window.captureDetails = false; window.detailFrames")
        self.assertTrue(frames)
        self.assertFalse(any(frame["editor"] and frame["history"] for frame in frames), frames)
        edit.click()
        expect(page.locator("#rule-form")).to_be_hidden()
        edit.click()
        expect(page.locator("#text")).to_have_value("original rule")

    def test_history_cards_show_readable_configuration_across_themes_and_widths(self):
        from playwright.sync_api import expect

        page = self.page
        page.evaluate("""() => {
          const api = window.AstrBotPluginView.apiGet;
          window.AstrBotPluginView.apiGet = async (endpoint, params) => endpoint === 'rules/history'
            ? [{version: 1, text: 'A long rule\\nwith another line', enabled: true,
                modified_by: 'admin', created_at: '2026-11-17T10:00:00Z', priority: 5,
                actions: [{type: 'recall', mode: 'auto'}]}]
            : api(endpoint, params);
        }""")
        card = page.locator(".rule-card").first
        card.get_by_role("button", name="修改记录", exact=True).click()
        entry = card.locator(".history-entry")
        expect(entry.locator(".history-entry-header strong")).to_have_text("创建规则")
        expect(entry.locator(".history-entry-meta")).to_contain_text("操作人: admin")
        expect(entry.locator(".history-entry-text")).to_have_text("A long rule\nwith another line")
        expect(entry.locator(".history-entry-actions li")).to_contain_text("撤回")
        for width in (1100, 390, 320):
            for theme in ("light", "dark"):
                with self.subTest(width=width, theme=theme):
                    page.set_viewport_size({"width": width, "height": 750})
                    page.emulate_media(color_scheme=theme, reduced_motion="reduce")
                    self.assertTrue(entry.evaluate("node => node.scrollWidth <= node.clientWidth"))
                    self.assertNotEqual(entry.evaluate("node => getComputedStyle(node).backgroundColor"),
                                        card.evaluate("node => getComputedStyle(node).backgroundColor"))

    def test_history_loading_and_fast_reversal_do_not_jump_or_restore_stale_content(self):
        from playwright.sync_api import expect

        page = self.page
        card = page.locator(".rule-card").first
        history = card.get_by_role("button", name="修改记录", exact=True)
        edit = card.locator(".rule-edit")
        page.evaluate("""() => {
          const api = window.AstrBotPluginView.apiGet;
          window.historyReads = [];
          window.AstrBotPluginView.apiGet = (endpoint, params) => {
            if (endpoint !== 'rules/history') return api(endpoint, params);
            return new Promise(resolve => window.historyReads.push(resolve));
          };
          window.heights = [];
          window.captureHeights = true;
          const capture = () => {
            window.heights.push(document.querySelector('.rule-card').getBoundingClientRect().height);
            if (window.captureHeights) requestAnimationFrame(capture);
          };
          requestAnimationFrame(capture);
        }""")
        history.click()
        expect(card.locator(".history-loading")).to_be_visible()
        expect(card.locator(".rule-history")).to_have_attribute("aria-busy", "true")
        page.wait_for_timeout(350)
        page.evaluate("window.historyReads[0]([{version: 0, text: 'old version', enabled: true, actions: []}])")
        expect(card.locator(".rule-history")).to_contain_text("old version")
        expect(card.locator(".rule-history")).to_have_attribute("aria-busy", "false")
        page.wait_for_timeout(350)
        heights = page.evaluate("window.captureHeights = false; window.heights")
        self.assertGreater(len(heights), 10)
        self.assertGreater(heights[-1], heights[0])
        self.assertGreaterEqual(min(b - a for a, b in zip(heights, heights[1:])), -2, heights)
        history.click()
        history.click()
        page.wait_for_function("window.historyReads.length === 2")
        edit.click()
        page.evaluate("window.historyReads[1]([{version: 0, text: 'stale version', enabled: true, actions: []}])")
        expect(page.locator("#rule-form")).to_be_visible()
        expect(card.locator(".rule-history")).to_be_hidden()
        history.click()
        page.wait_for_function("window.historyReads.length === 3")
        page.evaluate("window.historyReads[2]([{version: 0, text: 'fresh version', enabled: true, actions: []}])")
        expect(card.locator(".rule-history")).to_contain_text("fresh version")
        expect(card.locator(".rule-history")).not_to_contain_text("stale version")

    def test_fast_history_response_fades_in_without_reversing_card_height(self):
        from playwright.sync_api import expect

        page = self.page
        page.evaluate("""() => {
          const api = window.AstrBotPluginView.apiGet;
          window.AstrBotPluginView.apiGet = async (endpoint, params) => endpoint === 'rules/history'
            ? [{version: 0, text: 'old rule', enabled: true, actions: []}] : api(endpoint, params);
          window.fastFrames = [];
          window.captureFast = true;
          const capture = () => {
            const card = document.querySelector('.rule-card');
            const history = card.querySelector('.rule-history');
            const loading = history.querySelector('.history-loading');
            window.fastFrames.push({height: card.getBoundingClientRect().height,
              loading: !!loading, opacity: Number(getComputedStyle(history).opacity)});
            if (window.captureFast) requestAnimationFrame(capture);
          };
          requestAnimationFrame(capture);
        }""")
        page.locator(".rule-history-toggle").click()
        expect(page.locator(".history-loading")).to_be_visible()
        expect(page.locator(".rule-history")).to_contain_text("old rule")
        page.wait_for_timeout(350)
        frames = page.evaluate("window.captureFast = false; window.fastFrames")
        self.assertTrue(any(frame["loading"] for frame in frames), frames)
        self.assertTrue(any(not frame["loading"] for frame in frames), frames)
        heights = [frame["height"] for frame in frames]
        self.assertGreaterEqual(min(b - a for a, b in zip(heights, heights[1:])), -2, frames)

    def test_rule_details_are_exclusive_across_cards_with_reduced_motion(self):
        from playwright.sync_api import expect

        page = self.page
        page.emulate_media(reduced_motion="reduce")
        self.select_group("")
        expect(page.locator(".rule-card")).to_have_count(2)
        first, second = page.locator(".rule-card").all()
        first.get_by_role("button", name="编辑", exact=True).click()
        page.locator("#text").fill("draft in first rule")
        second.get_by_role("button", name="修改记录", exact=True).click()
        expect(page.locator("#rule-form")).to_be_hidden()
        expect(second.locator(".rule-history")).to_be_visible()
        expect(first.get_by_role("button", name="编辑", exact=True)).to_have_attribute("aria-expanded", "false")
        first.get_by_role("button", name="编辑", exact=True).click()
        expect(second.locator(".rule-history")).to_be_hidden()
        expect(page.locator("#text")).to_have_value("draft in first rule")

    def test_suspended_draft_survives_group_switch_and_new_rule_closes_history(self):
        from playwright.sync_api import expect

        page = self.page
        page.emulate_media(reduced_motion="reduce")
        first = page.locator(".rule-card").first
        first.locator(".rule-edit").click()
        page.locator("#text").fill("draft retained through group switch")
        first.locator(".rule-history-toggle").click()
        expect(page.locator("#rule-form")).to_be_hidden()
        self.select_group("other")
        expect(page.locator(".rule-card strong").first).to_have_text("rule-b")
        self.select_group("g")
        expect(page.locator(".rule-card strong").first).to_have_text("rule-a")
        expect(page.locator("#rule-form")).to_be_hidden()
        page.locator(".rule-edit").click()
        expect(page.locator("#text")).to_have_value("draft retained through group switch")
        page.locator(".rule-history-toggle").click()
        expect(page.locator(".rule-history")).to_be_visible()
        page.locator("#new-rule").click()
        expect(page.locator(".rule-history")).to_be_hidden()
        expect(page.locator("#rule-form")).to_be_visible()
        expect(page.locator("#rule-form-title")).to_have_text("新建规则")

    def test_record_details_show_loading_before_fading_in_content_and_table_cells_are_centered(self):
        from playwright.sync_api import expect

        page = self.page
        page.evaluate("""() => {
          const api = window.AstrBotPluginView.apiGet;
          window.AstrBotPluginView.apiGet = async (endpoint, params) => {
            if (endpoint === 'messages') return [{id: 4, platform_id: 'p', group_id: 'g', message_id: 'm1',
              status: 'no_action', detail: JSON.stringify({reason: 'a long reason '.repeat(12), model_latency_ms: 875}),
              created_at: '2026-11-01T12:34:00Z'}];
            if (endpoint === 'message-content') return {content: 'original message'};
            return api(endpoint, params);
          };
        }""")
        page.locator("#tab-records").click()
        page.locator("#refresh-group").click()
        row = page.locator("#records-table tr.record")
        expect(row).to_have_count(1)
        styles = row.locator("td").evaluate_all("nodes => nodes.map(node => [getComputedStyle(node).textAlign, getComputedStyle(node).verticalAlign])")
        self.assertTrue(all(style == ["center", "middle"] for style in styles), styles)
        page.evaluate("""() => {
          window.messageFrames = [];
          window.captureMessage = true;
          const capture = () => {
            const original = [...document.querySelectorAll('.record-detail-section')]
              .find(node => node.querySelector('h3')?.textContent === '消息原文')?.querySelector('p');
            if (original && original.getClientRects().length) window.messageFrames.push({text: original.textContent,
              loading: original.classList.contains('is-loading'), opacity: Number(getComputedStyle(original).opacity)});
            if (window.captureMessage) requestAnimationFrame(capture);
          };
          requestAnimationFrame(capture);
        }""")
        row.click()
        original = page.locator(".record-detail-section").filter(has=page.get_by_role("heading", name="消息原文")).locator("p")
        expect(original).to_have_text("正在读取消息原文...")
        expect(original).to_have_text("original message")
        page.wait_for_timeout(250)
        frames = page.evaluate("window.captureMessage = false; window.messageFrames")
        self.assertTrue(any(frame["loading"] for frame in frames), frames)
        self.assertTrue(any(frame["text"] == "original message" and frame["opacity"] < .99 for frame in frames), frames)
        page.set_viewport_size({"width": 390, "height": 750})
        self.assertEqual(row.locator("td").first.evaluate("node => getComputedStyle(node).textAlign"), "left")

    def test_rule_buttons_and_refresh_remain_stable_while_saving(self):
        from playwright.sync_api import expect

        page = self.page
        card = page.locator(".rule-card").first
        self.assertEqual(card.locator(":scope > button").all_text_contents(), ["修改记录", "编辑"])
        card.get_by_role("button", name="编辑", exact=True).click()
        expect(card.get_by_role("button", name="收起", exact=True)).to_be_visible()
        expect(page.locator("#rule")).to_be_hidden()
        expect(page.locator("#rule-id-value")).to_have_text("rule-a")
        self.assertEqual(page.locator("#rule-priority-help").evaluate(
            "node => node.closest('label')?.querySelector('input')?.id"), "priority")
        self.assertIn("不可修改", page.locator("#rule").evaluate("node => node.parentElement.textContent"))
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
        expect(page.locator("#version")).to_have_value("2")
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
        expect(page.locator("#version")).to_have_value("2")
        expect(page.locator("#version")).to_have_value("2")
        expect(page.locator("#text")).to_have_value("second unsaved edit")
        expect(page.locator("#feedback-region")).to_contain_text("新修改尚未保存")
        expect(page.locator("#preview")).to_be_enabled()
        page.locator("#preview").click()
        expect(page.locator("#version")).to_have_value("3")
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
        expect(other.locator("#version")).to_have_value("2")
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
        expect(first.locator("#version")).to_have_value("2")
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
        expect(page.locator("#version")).to_have_value("2")
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
                    self.assertEqual(buttons.all_text_contents(), ["修改记录", "编辑"])
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

    def test_fault_notification_recipient_search_select_and_clear(self):
        from playwright.sync_api import expect

        page = self.page
        page.set_viewport_size({"width": 390, "height": 750})
        page.emulate_media(color_scheme="dark", reduced_motion="reduce")
        page.locator("#tab-settings").click()
        opener = page.get_by_role("button", name="选择接收人", exact=True)
        opener.click()
        dialog = page.get_by_role("dialog", name="选择失败提示接收人")
        search = dialog.get_by_role("searchbox")
        expect(search).to_be_focused()
        expect(dialog.get_by_role("checkbox")).to_have_count(2)
        self.assertTrue(dialog.evaluate("node => node.scrollWidth <= node.clientWidth"))
        dialog.get_by_role("button", name="下一页").click()
        dialog.get_by_role("checkbox", name=re.compile("小张")).check()
        search.fill("小林")
        expect(dialog.get_by_role("checkbox")).to_have_count(2)
        dialog.get_by_role("checkbox", name=re.compile("值班管理员.*bot-b")).check()
        expect(dialog.get_by_role("heading", name="已选择 (2)")).to_be_visible()
        dialog.get_by_role("button", name="取消").click()
        expect(dialog).to_be_hidden()
        expect(opener).to_be_focused()
        expect(page.locator("#notification-recipient-summary")).to_have_text("未设置接收人")
        opener.click()
        page.evaluate("window.quickStartMock.failRecipientLookup = true")
        dialog.get_by_role("button", name="刷新").click()
        expect(dialog.get_by_role("status")).to_contain_text("无法读取")
        page.evaluate("window.quickStartMock.failRecipientLookup = false")
        search.fill("值班")
        checkbox = dialog.get_by_role("checkbox", name=re.compile("值班管理员.*bot-b"))
        expect(checkbox).to_be_visible()
        checkbox.press("Space")
        search.fill("小林")
        dialog.get_by_role("checkbox", name=re.compile("小林.*bot-a")).check()
        expect(checkbox).to_be_checked()
        expect(dialog.get_by_role("heading", name="已选择 (2)")).to_be_visible()
        dialog.get_by_role("button", name="确认选择").click()
        expect(dialog).to_be_hidden()
        expect(page.locator("#notification-recipient-summary")).to_have_text("值班管理员 (bot-b), 小林 (bot-a)")
        self.assertEqual(page.evaluate("window.quickStartMock.settings.notification_targets"), [])
        page.locator("#setting-notification_interval_seconds").fill("120")
        page.locator("#settings-save").click()
        page.wait_for_function("window.quickStartMock.settings.notification_targets.length === 2")
        expect(page.locator("#settings-save")).to_be_enabled()
        self.assertEqual(page.evaluate("window.quickStartMock.settings.notification_targets"),
                         ["bot-b:FriendMessage:100", "bot-a:FriendMessage:100"])
        opener.click()
        expect(dialog.get_by_role("heading", name="已选择 (2)")).to_be_visible()
        dialog.get_by_role("button", name=re.compile("移除 值班管理员")).click()
        expect(checkbox).not_to_be_checked()
        expect(dialog.get_by_role("heading", name="已选择 (1)")).to_be_visible()
        dialog.get_by_role("button", name="全部清除").click()
        dialog.get_by_role("button", name="确认选择").click()
        expect(page.locator("#setting-notification_interval_seconds")).to_have_value("0")
        page.locator("#settings-save").click()
        page.wait_for_function("window.quickStartMock.settings.notification_targets.length === 0")

    def test_global_chat_query_source_switch_preserves_saved_choices_and_group_draft(self):
        from playwright.sync_api import expect

        page = self.page
        page.set_viewport_size({"width": 390, "height": 750})
        page.emulate_media(color_scheme="dark", reduced_motion="reduce")
        page.locator("#tab-groups").click()
        page.locator(".group-row").first.get_by_role("button", name="配置", exact=True).click()
        group_rules = page.locator("#group-chat-query-rules")
        group_mute = page.locator("#group-chat-query-mute")
        source = page.locator("#group-chat-source")
        inherit = page.get_by_role("switch", name="使用全局配置", exact=True)
        expect(inherit).to_be_checked()
        expect(inherit).to_be_enabled()
        expect(group_mute).to_be_disabled()
        open_global = page.get_by_role("button", name="前往全局问答设置")
        expect(page.locator(".group-chat-heading").get_by_role("button", name="前往全局问答设置")).to_be_visible()
        open_global.click()
        expect(page.get_by_role("tab", name="设置", exact=True)).to_have_attribute("aria-selected", "true")
        section = page.get_by_role("region", name="群规守卫问答", exact=True)
        global_options = section.get_by_role("group", name="机器人可回答的内容")
        heading = section.get_by_role("heading", name="群规守卫问答")
        expect(heading).to_be_focused()
        expect(heading).to_be_in_viewport()
        expect(section.get_by_role("switch")).to_have_count(0)
        expect(global_options).to_be_visible()
        heading.hover()
        expect(global_options.locator(".group-chat-dependent")).to_have_css("background-color", "rgba(0, 0, 0, 0)")
        page.locator("#global-chat-query-rules").check()
        page.locator("#settings-save").click()
        expect(page.locator("#settings-save")).to_be_enabled()
        page.locator("#tab-groups").click()
        expect(group_rules).not_to_be_checked()
        expect(group_rules).to_be_disabled()
        expect(source).to_contain_text("当前按全局清单回答")
        open_global.click()
        expect(heading).to_be_in_viewport()
        for width, theme in ((390, "dark"), (768, "light"), (1100, "light")):
            with self.subTest(width=width, theme=theme):
                page.set_viewport_size({"width": width, "height": 750})
                page.emulate_media(color_scheme=theme, reduced_motion="reduce")
                self.assertTrue(section.evaluate("node => node.scrollWidth <= node.clientWidth"))
                self.assertTrue(global_options.evaluate("node => node.scrollWidth <= node.clientWidth"))
        page.locator("#tab-groups").click()
        expect(inherit).to_be_enabled()
        expect(inherit).to_be_checked()
        expect(group_rules).to_be_disabled()
        inherit.uncheck()
        expect(group_rules).not_to_be_checked()
        group_rules.check()
        group_mute.uncheck()
        page.locator("#group-settings-save").click()
        expect(page.locator("#group-settings-status")).not_to_contain_text("尚未保存")
        expect(inherit).not_to_be_checked()
        self.assertEqual(page.evaluate("window.quickStartMock.settings.group_overrides['p/g'].chat_query_items"),
                         ["status", "rules", "recall", "reply"])
        group_rules.uncheck()
        inherit.check()
        expect(group_rules).to_be_disabled()
        expect(group_rules).not_to_be_checked()
        expect(group_mute).not_to_be_checked()
        expect(source).to_contain_text("当前按本群清单回答. 保存后改用全局清单.")
        page.locator("#group-context-mode").select_option("custom")
        page.locator("#group-context-messages").fill("4")
        page.locator("#group-settings-save").click()
        expect(page.locator("#group-settings-status")).to_contain_text("本群问答草稿已保留")
        expect(source).to_contain_text("当前按全局清单回答")
        self.assertEqual(page.evaluate("window.quickStartMock.settings.group_overrides['p/g']"), {
            "context_messages": 4, "chat_query_items": ["status", "rules", "recall", "reply"],
        })

        open_global.click()
        expect(heading).to_be_in_viewport()
        nav = page.get_by_role("navigation", name="设置分类")
        for name in ("监控与执行", "文字判定", "图片判定", "群规守卫问答", "失败提示", "消息记录", "运行限制"):
            expect(nav.get_by_role("link", name=name, exact=True)).to_be_visible()
        nav.get_by_role("link", name="图片判定").press("Enter")
        expect(page.get_by_role("heading", name="图片判定", exact=True)).to_be_focused()
        page.locator("#tab-groups").click()
        expect(inherit).to_be_checked()
        expect(inherit).to_be_enabled()
        expect(group_rules).not_to_be_checked()
        expect(group_mute).not_to_be_checked()
        inherit.uncheck()
        expect(group_rules).not_to_be_checked()
        expect(group_rules).to_be_enabled()
        expect(group_mute).not_to_be_checked()

    def test_group_chat_query_choices_save_with_their_group_and_enforce_dependency(self):
        from playwright.sync_api import expect

        page = self.page
        page.set_viewport_size({"width": 390, "height": 750})
        page.emulate_media(color_scheme="dark", reduced_motion="reduce")
        page.locator("#tab-groups").click()
        rows = page.locator(".group-row")
        rows.first.get_by_role("button", name="配置", exact=True).click()
        options = page.get_by_role("region", name="群规守卫问答")
        options.get_by_role("switch", name="使用全局配置").uncheck()
        status = options.get_by_role("checkbox", name=re.compile("运行状态"))
        rules = options.get_by_role("checkbox", name=re.compile("已启用规则"))
        mute = options.get_by_role("checkbox", name=re.compile("^禁言结果"))
        target = options.get_by_role("checkbox", name=re.compile("同时公开禁言对象"))
        expect(status).to_be_checked()
        expect(rules).not_to_be_checked()
        expect(target).not_to_be_checked()
        target.check()
        mute.uncheck()
        expect(target).to_be_disabled()
        expect(target).not_to_be_checked()
        rules.check()
        expect(options).to_be_visible()
        self.assertTrue(options.evaluate("node => node.scrollWidth <= node.clientWidth"))
        rows.nth(1).get_by_role("button", name="配置", exact=True).click()
        expect(mute).to_be_checked()
        expect(rules).not_to_be_checked()
        rows.first.get_by_role("button", name="配置", exact=True).click()
        expect(rules).to_be_checked()
        expect(target).to_be_disabled()
        page.locator("#group-settings-save").click()
        expect(page.locator("#group-settings-status")).not_to_contain_text("尚未保存")
        rows.nth(1).get_by_role("button", name="配置", exact=True).click()
        expect(mute).to_be_checked()
        rows.first.get_by_role("button", name="配置", exact=True).click()
        expect(rules).to_be_checked()
        expect(target).to_be_disabled()

    def test_group_context_inherits_global_and_saves_only_for_the_selected_group(self):
        from playwright.sync_api import expect

        page = self.page
        page.locator("#tab-groups").click()
        rows = page.locator(".group-row")
        rows.first.get_by_role("button", name="配置", exact=True).click()
        mode = page.locator("#group-context-mode")
        field = page.locator("#group-context-messages")
        expect(mode).to_have_value("")
        expect(page.locator("#group-context-inherit")).to_have_text("全局(关闭)")
        expect(field).to_be_hidden()
        mode.select_option("custom")
        expect(field).to_be_visible()
        expect(field).to_have_value("0")
        expect(field).to_have_accessible_description("填 0 关闭本群上下文. 最多 100 条, 条数较多时还受总字数限制.")
        field.fill("8")
        page.locator("#group-settings-save").click()
        expect(page.locator("#group-settings-status")).not_to_contain_text("尚未保存")
        rows.nth(1).get_by_role("button", name="配置", exact=True).click()
        expect(mode).to_have_value("")
        expect(field).to_have_value("0")
        rows.first.get_by_role("button", name="配置", exact=True).click()
        expect(field).to_have_value("8")

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
