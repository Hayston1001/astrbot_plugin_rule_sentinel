import importlib.util
import re
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from test_management_quick_start import Handler


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright is unavailable")
class ManagementOverviewTests(unittest.TestCase):
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
        self.page.goto(self.url + "?scenario=overview")
        self.page.locator("#overview-customize").wait_for(state="visible")
        self.page.wait_for_function("!document.querySelector('#overview-customize').disabled")

    def tearDown(self):
        self.page.close()
        self.assertEqual(self.errors, [])

    def test_pending_grouping_pagination_scope_and_single_action_confirmation(self):
        from playwright.sync_api import expect
        page = self.page
        expect(page.locator("#page-title")).to_have_text("总览")
        expect(page.locator("#overview-pending-count")).to_have_text("25 个动作, 涉及 24 条消息")
        expect(page.locator(".overview-pending-message")).to_have_count(5)
        expect(page.locator("#overview-pending-more")).to_be_visible()
        for count in (10, 15, 20, 24):
            page.locator("#overview-pending-more").click()
            expect(page.locator(".overview-pending-message")).to_have_count(count)
        expect(page.locator(".overview-pending-message")).to_have_count(24)
        expect(page.locator("#overview-pending-more")).to_be_hidden()
        first = page.locator(".overview-pending-message").first
        first.locator("summary").click()
        expect(first).to_contain_text("<script>示例消息</script>")
        expect(first).to_contain_text("重复推广")
        expect(first).to_contain_text("执行结果尚未产生")
        self.assertEqual(first.locator("script").count(), 0)
        first.get_by_role("button", name=re.compile("确认并尝试禁言")).first.click()
        expect(page.locator("#feedback-region")).to_contain_text("禁言已执行.")
        expect(page.locator("#overview-pending-count")).to_have_text("24 个动作, 涉及 24 条消息")
        self.assertEqual(page.evaluate("window.quickStartMock.confirmationPosts"), [{"intent_id": 1}])
        page.evaluate("""() => { const node = document.querySelector('#group-select');
            node.value = JSON.stringify(['p2', 'g']); node.dispatchEvent(new Event('change', {bubbles: true})); }""")
        expect(page.locator("#overview-pending-count")).to_have_text("1 个动作, 涉及 1 条消息")
        expect(page.locator("#overview-pending-scope")).to_have_text("另一个群")
        page.locator(".overview-pending-message summary").click()
        expect(page.locator(".overview-pending-message")).to_contain_text("另一个群的消息")

    def test_layout_control_is_next_to_title_and_tables_center_on_desktop(self):
        from playwright.sync_api import expect

        page = self.page
        title = page.locator("#page-title").bounding_box()
        button = page.locator("#overview-customize").bounding_box()
        self.assertGreater(button["x"], title["x"] + title["width"])
        self.assertLess(abs(button["y"] + button["height"] / 2 - title["y"] - title["height"] / 2), 2)
        self.assertLess(button["height"], 36)
        for selector in ("#groups-status-table th", "#groups-status-table td"):
            styles = page.locator(selector).evaluate_all("nodes => nodes.map(node => [getComputedStyle(node).textAlign, getComputedStyle(node).verticalAlign])")
            self.assertTrue(styles, selector)
            self.assertTrue(all(style == ["center", "middle"] for style in styles), styles)
        page.locator("#tab-records").click()
        expect(page.locator("#overview-customize")).to_be_hidden()
        styles = page.locator("#records-table th").evaluate_all("nodes => nodes.map(node => [getComputedStyle(node).textAlign, getComputedStyle(node).verticalAlign])")
        self.assertTrue(all(style == ["center", "middle"] for style in styles), styles)
        page.set_viewport_size({"width": 390, "height": 850})
        page.locator("#tab-status").click()
        expect(page.locator("#overview-customize")).to_be_visible()
        self.assertGreaterEqual(page.locator("#overview-customize").bounding_box()["height"], 44)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)

    def test_pending_empty_state_is_only_shown_after_a_successful_empty_read(self):
        from playwright.sync_api import expect

        page = self.page
        empty = page.locator("#overview-pending-empty")
        count = page.locator("#overview-pending-count")
        expect(empty).to_be_hidden()
        expect(count).to_contain_text("25 个动作")
        page.evaluate("""async () => {
          window.pendingSample = structuredClone(window.quickStartMock.pending[0]);
          window.quickStartMock.pending = [];
          await window.SentinelOverview.refresh();
        }""")
        expect(empty).to_be_visible()
        expect(empty).to_contain_text("暂无待确认禁言")
        expect(empty).to_contain_text("需要确认的禁言会显示在这里.")
        expect(count).to_be_hidden()
        self.assertEqual(empty.get_attribute("role"), "status")
        self.assertEqual(empty.locator("svg").get_attribute("aria-hidden"), "true")
        for width in (1200, 390):
            for theme in ("light", "dark"):
                with self.subTest(width=width, theme=theme):
                    page.set_viewport_size({"width": width, "height": 850})
                    page.emulate_media(color_scheme=theme)
                    icon, title = empty.locator("svg").bounding_box(), empty.locator("strong").bounding_box()
                    self.assertLess(icon["x"], title["x"])
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
        page.evaluate("""async () => { window.quickStartMock.failPending = true; await window.SentinelOverview.refresh(); }""")
        expect(empty).to_be_hidden()
        expect(count).to_be_visible()
        expect(count).to_have_text("待确认列表不可用.")
        page.evaluate("""async () => {
          window.quickStartMock.failPending = false;
          window.quickStartMock.pending = [window.pendingSample];
          await window.SentinelOverview.refresh();
        }""")
        expect(empty).to_be_hidden()
        expect(count).to_contain_text("2 个动作")

        page.evaluate("""async () => {
          const api = window.AstrBotPluginView.apiGet;
          window.AstrBotPluginView.apiGet = (endpoint, params) => endpoint === 'pending-confirmations'
            ? Promise.resolve({messages: [], total_messages: 1, total_actions: 0, next_before_id: null})
            : api(endpoint, params);
          await window.SentinelOverview.refresh();
        }""")
        expect(empty).to_be_hidden()
        expect(count).to_have_text("待确认列表不可用.")

    def test_pending_empty_load_fades_through_loading_state(self):
        from playwright.sync_api import expect

        page = self.page
        page.emulate_media(reduced_motion="no-preference")
        page.evaluate("""() => {
          window.quickStartMock.pending = [];
          window.pendingFrames = [];
          window.capturePending = true;
          const capture = () => {
            const count = document.querySelector('#overview-pending-count');
            const empty = document.querySelector('#overview-pending-empty');
            window.pendingFrames.push({loading: !count.hidden && count.classList.contains('is-loading'),
              empty: !empty.hidden, opacity: Number(getComputedStyle(empty).opacity)});
            if (window.capturePending) requestAnimationFrame(capture);
          };
          requestAnimationFrame(capture);
          window.SentinelOverview.setScope({all_groups: 'true'});
        }""")
        expect(page.locator("#overview-pending-count")).to_have_text("正在读取...")
        expect(page.locator("#overview-pending-empty")).to_be_visible()
        page.wait_for_timeout(220)
        frames = page.evaluate("window.capturePending = false; window.pendingFrames")
        self.assertTrue(any(frame["loading"] for frame in frames), frames)
        self.assertTrue(any(frame["empty"] and frame["opacity"] < .99 for frame in frames), frames)

    def test_pending_pagination_does_not_keep_stale_rows_when_the_queue_is_empty(self):
        from playwright.sync_api import expect

        page = self.page
        expect(page.locator(".overview-pending-message")).to_have_count(5)
        page.evaluate("window.quickStartMock.pending = []")
        page.locator("#overview-pending-more").click()
        expect(page.locator("#overview-pending-empty")).to_be_visible()
        expect(page.locator(".overview-pending-message")).to_have_count(0)
        expect(page.locator("#overview-pending-more")).to_be_hidden()

    def test_status_icons_use_the_selected_lucide_shape(self):
        page = self.page
        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                page.emulate_media(color_scheme=theme)
                icons = page.evaluate("""() => {
                  const read = selector => {
                    const style = getComputedStyle(document.querySelector(selector), '::before');
                    return {content: style.content, mask: style.maskImage, color: style.backgroundColor,
                      width: style.width, height: style.height};
                  };
                  const status = document.querySelector('#status');
                  status.dataset.tone = 'success';
                  const success = read('#status');
                  status.dataset.tone = 'warning';
                  const warning = read('#status');
                  status.dataset.tone = 'success';
                  return {success, warning};
                }""")
                self.assertIn("M21.801", icons["success"]["mask"])
                self.assertIn("circle", icons["warning"]["mask"])
                for icon in icons.values():
                    self.assertEqual(icon["content"], '""')
                    self.assertEqual((icon["width"], icon["height"]), ("15px", "15px"))
                    self.assertNotEqual(icon["color"], "rgba(0, 0, 0, 0)")

    def test_layout_draft_cancel_save_reload_and_all_cards_hidden(self):
        from playwright.sync_api import expect
        page = self.page
        page.locator("#overview-customize").click()
        pending = page.locator('[data-card="pending"]')
        pending.locator(".overview-width").click()
        expect(pending).to_have_attribute("data-width", "half")
        page.locator('[data-visibility="pending"]').uncheck()
        expect(pending).to_be_hidden()
        page.locator("#overview-cancel").click()
        expect(pending).to_be_visible()
        expect(pending).to_have_attribute("data-width", "full")
        page.locator("#overview-customize").click()
        for card in ("pending", "overall", "recent", "groups", "platforms", "diagnostics"):
            checkbox = page.locator(f'[data-visibility="{card}"]')
            if checkbox.is_checked():
                checkbox.uncheck()
        page.locator("#overview-save").click()
        expect(page.locator("#overview-customize")).to_be_enabled()
        self.assertEqual(page.locator(".overview-card:visible").count(), 0)
        page.reload()
        page.wait_for_function("!document.querySelector('#overview-customize').disabled")
        self.assertEqual(page.locator(".overview-card:visible").count(), 0)
        page.locator("#overview-customize").click()
        page.locator('[data-visibility="pending"]').check()
        expect(pending).to_be_visible()
        page.locator("#overview-reset").click()
        expect(page.locator('[data-card="overall"]')).to_be_visible()
        page.locator("#overview-cancel").click()
        self.assertEqual(page.locator(".overview-card:visible").count(), 0)

    def test_width_change_animates_card_and_neighbor_without_teleporting(self):
        page = self.page
        page.set_viewport_size({"width": 1200, "height": 1400})
        page.emulate_media(reduced_motion="no-preference")
        page.wait_for_function("document.querySelector('#overview-pending-count').textContent.includes('25 个动作')")
        page.locator("#overview-customize").click()
        start = page.evaluate("""() => {
          const card = document.querySelector('[data-card="overall"]');
          const other = document.querySelector('[data-card="recent"]');
          const read = () => {
            const rect = card.getBoundingClientRect(), neighbor = other.getBoundingClientRect();
            return {left: rect.left, top: rect.top, width: rect.width, height: rect.height,
              neighborLeft: neighbor.left, neighborTop: neighbor.top, neighborWidth: neighbor.width, neighborHeight: neighbor.height,
              headingHeight: card.querySelector('h2').getBoundingClientRect().height,
              scroll: scrollY};
          };
          const before = read();
          card.querySelector('.overview-width').click();
          const immediate = read();
          window.widthFrames = [];
          window.captureWidth = true;
          const capture = () => { widthFrames.push(read()); if (captureWidth) requestAnimationFrame(capture); };
          requestAnimationFrame(capture);
          return {before, immediate};
        }""")
        self.assertAlmostEqual(start["immediate"]["width"], start["before"]["width"], delta=3, msg=start)
        self.assertAlmostEqual(start["immediate"]["neighborTop"], start["before"]["neighborTop"], delta=3, msg=start)
        page.wait_for_timeout(700)
        result = page.evaluate("""() => {
          window.captureWidth = false;
          const card = document.querySelector('[data-card="overall"]');
          return {frames: widthFrames, width: card.getBoundingClientRect().width, layout: card.dataset.width,
            focused: document.activeElement === card.querySelector('.overview-width'),
            rows: document.querySelector('#overview-grid').style.gridTemplateRows};
        }""")
        self.assertEqual(result["layout"], "full")
        self.assertGreater(result["width"], start["before"]["width"] * 1.8, result)
        self.assertGreater(len(set(round(frame["width"]) for frame in result["frames"])), 3, result)
        self.assertGreater(len(set(round(frame["neighborTop"]) for frame in result["frames"])), 3, result)
        self.assertTrue(all(abs(frame["headingHeight"] - start["before"]["headingHeight"]) < 2
                            for frame in result["frames"]), result)
        overlaps = [max(0, min(frame["left"] + frame["width"], frame["neighborLeft"] + frame["neighborWidth"])
                        - max(frame["left"], frame["neighborLeft"])) *
                    max(0, min(frame["top"] + frame["height"], frame["neighborTop"] + frame["neighborHeight"])
                        - max(frame["top"], frame["neighborTop"])) for frame in result["frames"]]
        self.assertLess(max(overlaps, default=0), 25, result)
        self.assertTrue(result["focused"], result)
        self.assertEqual(result["rows"], "")

    def test_width_change_reverses_smoothly_during_an_existing_transition(self):
        page = self.page
        page.set_viewport_size({"width": 1200, "height": 1400})
        page.emulate_media(reduced_motion="no-preference")
        page.wait_for_function("document.querySelector('#overview-pending-count').textContent.includes('25 个动作')")
        page.locator("#overview-customize").click()
        original = page.locator('[data-card="overall"]').bounding_box()
        page.locator('[data-card="overall"] .overview-width').evaluate("button => button.click()")
        page.wait_for_timeout(120)
        reversal = page.evaluate("""() => {
          const card = document.querySelector('[data-card="overall"]');
          const read = () => { const r = card.getBoundingClientRect();
            return {left: r.left, top: r.top, width: r.width, height: r.height}; };
          const before = read();
          card.querySelector('.overview-width').click();
          return {before, immediate: read()};
        }""")
        for key in ("left", "top", "width", "height"):
            self.assertAlmostEqual(reversal["before"][key], reversal["immediate"][key], delta=3, msg=reversal)
        page.wait_for_timeout(700)
        result = page.evaluate("""() => {
          const card = document.querySelector('[data-card="overall"]');
          return {width: card.getBoundingClientRect().width, layout: card.dataset.width,
            focused: document.activeElement === card.querySelector('.overview-width'),
            rows: document.querySelector('#overview-grid').style.gridTemplateRows,
            spacers: document.querySelectorAll('.overview-resize-spacer').length,
            animating: document.querySelectorAll('.is-width-animating, .is-width-target').length};
        }""")
        self.assertEqual(result["layout"], "half")
        self.assertAlmostEqual(result["width"], original["width"], delta=2, msg=result)
        self.assertTrue(result["focused"], result)
        self.assertEqual((result["rows"], result["spacers"], result["animating"]), ("", 0, 0))

    def test_width_change_respects_reduced_motion_and_single_column(self):
        page = self.page
        page.set_viewport_size({"width": 1200, "height": 1400})
        page.locator("#overview-customize").click()
        result = page.evaluate("""() => {
          const card = document.querySelector('[data-card="overall"]');
          const before = card.getBoundingClientRect().width;
          card.querySelector('.overview-width').click();
          return {before, after: card.getBoundingClientRect().width, layout: card.dataset.width,
            animations: card.getAnimations().length, focused: document.activeElement === card.querySelector('.overview-width'),
            spacer: document.querySelector('.overview-resize-spacer') !== null};
        }""")
        self.assertGreater(result["after"], result["before"] * 1.8, result)
        self.assertEqual(result["layout"], "full")
        self.assertEqual(result["animations"], 0)
        self.assertTrue(result["focused"], result)
        self.assertFalse(result["spacer"], result)
        page.set_viewport_size({"width": 390, "height": 850})
        page.emulate_media(reduced_motion="no-preference")
        page.locator('[data-card="overall"] .overview-width').scroll_into_view_if_needed()
        result = page.evaluate("""() => {
          const card = document.querySelector('[data-card="overall"]');
          const before = card.getBoundingClientRect().width;
          card.querySelector('.overview-width').click();
          return {before, after: card.getBoundingClientRect().width, layout: card.dataset.width,
            animations: card.getAnimations().length, focused: document.activeElement === card.querySelector('.overview-width'),
            rows: document.querySelector('#overview-grid').style.gridTemplateRows,
            spacer: document.querySelector('.overview-resize-spacer') !== null};
        }""")
        self.assertAlmostEqual(result["after"], result["before"], delta=1, msg=result)
        self.assertEqual(result["layout"], "half")
        self.assertEqual(result["animations"], 0)
        self.assertTrue(result["focused"], result)
        self.assertEqual(result["rows"], "")
        self.assertFalse(result["spacer"], result)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)

    def test_width_change_follows_smoothly_when_the_page_becomes_shorter(self):
        page = self.page
        page.set_viewport_size({"width": 1200, "height": 650})
        page.locator("#overview-customize").click()
        page.locator('[data-card="recent"] .overview-width').click()
        page.locator('[data-card="groups"] .overview-width').click()
        page.evaluate("""() => {
          for (const card of document.querySelectorAll('#overview-grid > [data-card]')) card.style.minHeight = '400px';
          const card = document.querySelector('[data-card="recent"]');
          window.scrollTo(0, card.getBoundingClientRect().top + window.scrollY - 100);
        }""")
        page.emulate_media(reduced_motion="no-preference")
        start = page.evaluate("""() => {
          const card = document.querySelector('[data-card="recent"]');
          const before = scrollY;
          const width = card.getBoundingClientRect().width;
          card.querySelector('.overview-width').click();
          window.widthScrollFrames = [];
          window.captureWidthScroll = true;
          const capture = () => { widthScrollFrames.push(scrollY); if (captureWidthScroll) requestAnimationFrame(capture); };
          requestAnimationFrame(capture);
          return {before, immediate: scrollY, widthBefore: width, widthImmediate: card.getBoundingClientRect().width};
        }""")
        self.assertGreater(start["before"], 300)
        self.assertAlmostEqual(start["immediate"], start["before"], delta=3, msg=start)
        self.assertAlmostEqual(start["widthBefore"], start["widthImmediate"], delta=3, msg=start)
        page.wait_for_timeout(700)
        result = page.evaluate("""() => {
          window.captureWidthScroll = false;
          const card = document.querySelector('[data-card="recent"]');
          const control = card.querySelector('.overview-width').getBoundingClientRect();
          return {frames: widthScrollFrames, scroll: scrollY, max: document.scrollingElement.scrollHeight - innerHeight,
            top: control.top, bottom: control.bottom, height: innerHeight, layout: card.dataset.width,
            focused: document.activeElement === card.querySelector('.overview-width'),
            spacers: document.querySelectorAll('.overview-resize-spacer').length};
        }""")
        self.assertEqual(result["layout"], "half")
        self.assertGreater(len(set(round(y) for y in result["frames"])), 3, result)
        self.assertGreaterEqual(min(result["frames"]), min(start["before"], result["scroll"]) - 10, result)
        self.assertLessEqual(result["scroll"], result["max"] + 1, result)
        self.assertGreaterEqual(result["top"], 80, result)
        self.assertLessEqual(result["bottom"], result["height"], result)
        self.assertTrue(result["focused"], result)
        self.assertEqual(result["spacers"], 0)

    def test_reversing_width_near_page_end_keeps_the_current_scroll_and_frame(self):
        page = self.page
        page.set_viewport_size({"width": 1200, "height": 650})
        page.locator("#overview-customize").click()
        page.locator('[data-card="recent"] .overview-width').click()
        page.locator('[data-card="groups"] .overview-width').click()
        page.evaluate("""() => {
          for (const card of document.querySelectorAll('#overview-grid > [data-card]')) card.style.minHeight = '400px';
          const card = document.querySelector('[data-card="recent"]');
          window.scrollTo(0, card.getBoundingClientRect().top + scrollY - 100);
        }""")
        page.emulate_media(reduced_motion="no-preference")
        page.locator('[data-card="recent"] .overview-width').evaluate("button => button.click()")
        page.wait_for_timeout(100)
        result = page.evaluate("""() => {
          const card = document.querySelector('[data-card="recent"]');
          const read = () => { const rect = card.getBoundingClientRect();
            return {scroll: scrollY, top: rect.top, left: rect.left, width: rect.width, height: rect.height}; };
          const before = read();
          card.querySelector('.overview-width').click();
          return {before, immediate: read()};
        }""")
        self.assertGreater(result["before"]["scroll"], 300, result)
        for key in ("scroll", "top", "left", "width", "height"):
            self.assertAlmostEqual(result["before"][key], result["immediate"][key], delta=3, msg=result)
        page.wait_for_timeout(700)
        self.assertEqual(page.locator('[data-card="recent"]').get_attribute("data-width"), "full")
        self.assertEqual(page.locator(".overview-resize-spacer, .is-width-animating, .is-width-target").count(), 0)
        self.assertEqual(page.locator("#overview-grid").evaluate("node => node.style.gridTemplateRows"), "")

    def test_manual_scrolling_interrupts_width_transition_and_cleans_up(self):
        page = self.page
        page.set_viewport_size({"width": 1200, "height": 650})
        page.emulate_media(reduced_motion="no-preference")
        page.wait_for_function("document.querySelector('#overview-pending-count').textContent.includes('25 个动作')")
        page.locator("#overview-customize").click()
        page.locator('[data-card="overall"] .overview-width').evaluate("button => button.click()")
        page.wait_for_timeout(70)
        page.mouse.move(1100, 500)
        page.mouse.wheel(0, 180)
        page.wait_for_timeout(100)
        result = page.evaluate("""() => ({scroll: scrollY, layout: document.querySelector('[data-card="overall"]').dataset.width,
          rows: document.querySelector('#overview-grid').style.gridTemplateRows,
          spacers: document.querySelectorAll('.overview-resize-spacer').length,
          animating: document.querySelectorAll('.is-width-animating, .is-width-target').length})""")
        page.wait_for_timeout(400)
        self.assertAlmostEqual(page.evaluate("window.scrollY"), result["scroll"], delta=1, msg=result)
        self.assertEqual(result["layout"], "full")
        self.assertEqual((result["rows"], result["spacers"], result["animating"]), ("", 0, 0))

    def test_resizing_the_viewport_interrupts_the_width_transition(self):
        page = self.page
        page.set_viewport_size({"width": 1200, "height": 850})
        page.emulate_media(reduced_motion="no-preference")
        page.wait_for_function("document.querySelector('#overview-pending-count').textContent.includes('25 个动作')")
        page.locator("#overview-customize").click()
        page.locator('[data-card="overall"] .overview-width').evaluate("button => button.click()")
        page.set_viewport_size({"width": 390, "height": 850})
        page.wait_for_function("""() => !document.querySelector('.is-width-target') &&
          !document.querySelector('.overview-resize-spacer')""")
        result = page.evaluate("""() => ({layout: document.querySelector('[data-card="overall"]').dataset.width,
          rows: document.querySelector('#overview-grid').style.gridTemplateRows,
          overflow: document.documentElement.scrollWidth - innerWidth})""")
        self.assertEqual(result["layout"], "full")
        self.assertEqual(result["rows"], "")
        self.assertLessEqual(result["overflow"], 0, result)

    def test_width_controls_work_from_keyboard_and_preserve_expanded_content(self):
        page = self.page
        page.set_viewport_size({"width": 1200, "height": 1400})
        page.emulate_media(reduced_motion="no-preference")
        page.wait_for_function("document.querySelector('#overview-pending-count').textContent.includes('25 个动作')")
        page.locator("#overview-customize").click()
        first = page.locator('.overview-pending-message').first
        first.locator('summary').click()
        self.assertIsNotNone(first.get_attribute("open"))
        width = page.locator('[data-card="pending"] .overview-width')
        width.focus()
        page.keyboard.press("Space")
        self.assertEqual(page.locator('[data-card="pending"]').get_attribute("data-width"), "half")
        self.assertIsNotNone(first.get_attribute("open"))
        page.wait_for_timeout(100)
        page.keyboard.press("Enter")
        self.assertEqual(page.locator('[data-card="pending"]').get_attribute("data-width"), "full")
        self.assertIsNotNone(first.get_attribute("open"))
        page.wait_for_timeout(700)
        self.assertTrue(page.locator('[data-card="pending"] .overview-width').evaluate("node => document.activeElement === node"))
        self.assertIsNone(page.evaluate("sessionStorage.getItem('fixture-layout-overview')"))
        self.assertEqual(page.locator(".overview-resize-spacer, .is-width-animating, .is-width-target").count(), 0)

    def test_switching_another_card_mid_transition_keeps_visible_positions(self):
        page = self.page
        page.set_viewport_size({"width": 1200, "height": 1400})
        page.emulate_media(reduced_motion="no-preference")
        page.wait_for_function("document.querySelector('#overview-pending-count').textContent.includes('25 个动作')")
        page.locator("#overview-customize").click()
        page.locator('[data-card="overall"] .overview-width').evaluate("button => button.click()")
        page.wait_for_timeout(100)
        result = page.evaluate("""() => {
          const read = () => [...document.querySelectorAll('#overview-grid > [data-card]')].filter(card => !card.hidden)
            .map(card => { const r = card.getBoundingClientRect(); return {id: card.dataset.card,
              left: r.left, top: r.top, width: r.width, height: r.height}; });
          const before = read();
          document.querySelector('[data-card="recent"] .overview-width').click();
          return {before, immediate: read()};
        }""")
        for before, after in zip(result["before"], result["immediate"]):
            self.assertEqual(before["id"], after["id"])
            for key in ("left", "top", "width", "height"):
                self.assertAlmostEqual(before[key], after[key], delta=3, msg=result)
        page.wait_for_timeout(700)
        self.assertEqual(page.locator('[data-card="overall"]').get_attribute("data-width"), "full")
        self.assertEqual(page.locator('[data-card="recent"]').get_attribute("data-width"), "full")
        self.assertTrue(page.locator('[data-card="recent"] .overview-width').evaluate("node => document.activeElement === node"))
        self.assertEqual(page.locator(".overview-resize-spacer, .is-width-animating, .is-width-target").count(), 0)

    def test_each_card_changes_width_in_both_directions_without_a_layout_jump(self):
        page = self.page
        page.set_viewport_size({"width": 1200, "height": 1500})
        page.emulate_media(reduced_motion="no-preference")
        for card in ("pending", "overall", "recent", "groups"):
            page.reload()
            page.wait_for_function("!document.querySelector('#overview-customize').disabled")
            page.wait_for_function("document.querySelector('#overview-pending-count').textContent.includes('25 个动作')")
            page.locator("#overview-customize").click()
            for direction in range(2):
                with self.subTest(card=card, direction=direction):
                    result = page.evaluate("""id => {
                      const grid = document.querySelector('#overview-grid');
                      const nodes = [...grid.querySelectorAll(':scope > [data-card]')].filter(node => !node.hidden);
                      const measure = () => nodes.map(node => { const r = node.getBoundingClientRect();
                        return {id: node.dataset.card, left: r.left, top: r.top + scrollY, width: r.width, height: r.height}; });
                      const before = measure(), raf = window.requestAnimationFrame;
                      window.requestAnimationFrame = callback => { window.widthStep = callback; return 987654; };
                      let immediate, overflow = 0;
                      try {
                        grid.querySelector(`[data-card="${id}"] .overview-width`).click();
                        immediate = measure();
                        for (let time = 0; time <= 320; time += 16) {
                          window.widthStep(time);
                          overflow = Math.max(overflow, document.documentElement.scrollWidth - innerWidth);
                        }
                      } finally { window.requestAnimationFrame = raf; }
                      return {before, immediate, overflow, rows: grid.style.gridTemplateRows,
                        spacer: Boolean(document.querySelector('.overview-resize-spacer'))};
                    }""", card)
                    for before, after in zip(result["before"], result["immediate"]):
                        self.assertEqual(before["id"], after["id"])
                        for key in ("left", "top", "width", "height"):
                            self.assertAlmostEqual(before[key], after[key], delta=3, msg=result)
                    self.assertEqual(result["overflow"], 0, result)
                    self.assertEqual(result["rows"], "")
                    self.assertFalse(result["spacer"])

    def test_keyboard_and_pointer_reorder_only_in_edit_mode(self):
        from playwright.sync_api import expect
        page = self.page
        self.assertEqual(page.locator(".overview-card-tools").count(), 0)
        page.locator("#overview-customize").click()
        handle = page.locator('[data-card="overall"] .overview-drag-handle')
        handle.focus()
        page.keyboard.press("ArrowRight")
        order = page.locator("#overview-grid > [data-card]:visible").evaluate_all("cards => cards.map(card => card.dataset.card)")
        self.assertEqual(order[:4], ["pending", "recent", "overall", "groups"])
        expect(page.locator("#overview-order-status")).to_contain_text("整体状态已移动")
        handle = page.locator('[data-card="overall"] .overview-drag-handle')
        target = page.locator('[data-card="groups"]')
        target.scroll_into_view_if_needed()
        handle.scroll_into_view_if_needed()
        source_box, target_box = handle.bounding_box(), target.bounding_box()
        page.mouse.move(source_box["x"] + 10, source_box["y"] + 10)
        page.mouse.down()
        page.mouse.move(target_box["x"] + 15, target_box["y"] + target_box["height"] - 8, steps=8)
        expect(page.locator(".overview-drop-placeholder")).to_be_visible()
        page.mouse.up()
        order = page.locator("#overview-grid > [data-card]:visible").evaluate_all("cards => cards.map(card => card.dataset.card)")
        self.assertEqual(order[-1], "overall")
        page.locator("#overview-save").click()
        page.reload()
        page.wait_for_function("!document.querySelector('#overview-customize').disabled")
        order = page.locator("#overview-grid > [data-card]:visible").evaluate_all("cards => cards.map(card => card.dataset.card)")
        self.assertEqual(order[-1], "overall")

    def test_drag_previews_the_new_order_before_release_and_escape_restores_it(self):
        page = self.page
        page.set_viewport_size({"width": 1200, "height": 1800})
        page.locator("#overview-customize").click()
        handle = page.locator('[data-card="overall"] .overview-drag-handle')
        target = page.locator('[data-card="recent"]')
        source_box, target_box = handle.bounding_box(), target.bounding_box()
        page.mouse.move(source_box["x"] + 10, source_box["y"] + 10)
        page.mouse.down()
        page.mouse.move(target_box["x"] + target_box["width"] * .8, target_box["y"] + 30, steps=8)
        preview = page.evaluate("""() => {
          const grid = document.querySelector('#overview-grid');
          const card = grid.querySelector('[data-card="overall"]');
          const slot = grid.querySelector('.overview-drop-placeholder');
          return {order: [...grid.children].filter(node => !node.hidden && node !== card)
              .map(node => node === slot ? 'slot' : node.dataset.card),
            floating: getComputedStyle(card).position === 'fixed',
            left: card.getBoundingClientRect().left, right: card.getBoundingClientRect().right, width: innerWidth,
            slotVisible: slot && getComputedStyle(slot).visibility === 'visible',
            stored: sessionStorage.getItem('fixture-layout-overview')};
        }""")
        self.assertEqual(preview["order"][:4], ["pending", "recent", "slot", "groups"])
        self.assertTrue(preview["floating"])
        self.assertTrue(preview["slotVisible"])
        self.assertGreaterEqual(preview["left"], 0, preview)
        self.assertLessEqual(preview["right"], preview["width"], preview)
        self.assertIsNone(preview["stored"])
        page.keyboard.press("Escape")
        page.mouse.up()
        order = page.locator("#overview-grid > [data-card]:visible").evaluate_all("nodes => nodes.map(node => node.dataset.card)")
        self.assertEqual(order[:4], ["pending", "overall", "recent", "groups"])
        self.assertEqual(page.locator(".overview-drop-placeholder").count(), 0)
        self.assertIsNone(page.evaluate("sessionStorage.getItem('fixture-layout-overview')"))

    def test_live_preview_commits_the_position_on_release(self):
        page = self.page
        page.set_viewport_size({"width": 1200, "height": 1800})
        page.locator("#overview-customize").click()
        handle = page.locator('[data-card="overall"] .overview-drag-handle')
        target = page.locator('[data-card="recent"]')
        source_box, target_box = handle.bounding_box(), target.bounding_box()
        page.mouse.move(source_box["x"] + 10, source_box["y"] + 10)
        page.mouse.down()
        page.mouse.move(target_box["x"] + target_box["width"] * .8, target_box["y"] + 30, steps=8)
        preview = page.locator("#overview-grid > .overview-drop-placeholder")
        self.assertEqual(preview.count(), 1)
        self.assertTrue(preview.is_visible())
        page.mouse.up()
        order = page.locator("#overview-grid > [data-card]:visible").evaluate_all("nodes => nodes.map(node => node.dataset.card)")
        self.assertEqual(order[:4], ["pending", "recent", "overall", "groups"])
        self.assertEqual(page.locator(".overview-drop-placeholder").count(), 0)
        self.assertIsNone(page.evaluate("sessionStorage.getItem('fixture-layout-overview')"))
        page.locator("#overview-save").click()
        page.wait_for_function("!document.querySelector('#overview-customize').disabled")
        saved = page.evaluate("JSON.parse(sessionStorage.getItem('fixture-layout-overview')).cards")
        self.assertEqual([item["id"] for item in saved[:4]], ["pending", "recent", "overall", "groups"])

    def test_live_preview_stays_stable_while_other_cards_animate(self):
        page = self.page
        page.set_viewport_size({"width": 1200, "height": 1800})
        page.emulate_media(reduced_motion="no-preference")
        page.wait_for_function("document.querySelector('#overview-pending-count').textContent.includes('25 个动作')")
        page.locator("#overview-customize").click()
        for card in ("pending", "groups"):
            page.locator(f'[data-card="{card}"] .overview-width').click()
        point = page.evaluate("""() => {
          const handle = document.querySelector('[data-card="overall"] .overview-drag-handle');
          const start = handle.getBoundingClientRect();
          const target = document.querySelector('[data-card="pending"]').getBoundingClientRect();
          const point = {x: target.left + target.width * .1, y: target.top + target.height * .1};
          const pointer = (type, x, y) => new PointerEvent(type, {pointerId: 77, pointerType: 'touch',
            isPrimary: true, bubbles: true, cancelable: true, button: 0, clientX: x, clientY: y});
          handle.dispatchEvent(pointer('pointerdown', start.left + 10, start.top + 10));
          window.dispatchEvent(pointer('pointermove', point.x, point.y));
          return point;
        }""")
        samples = []
        for index in range(16):
            samples.append(page.evaluate("""p => {
              window.dispatchEvent(new PointerEvent('pointermove', {pointerId: 77, pointerType: 'touch',
                isPrimary: true, bubbles: true, cancelable: true, clientX: p.x, clientY: p.y}));
              const grid = document.querySelector('#overview-grid');
              return {order: [...grid.children].filter(node => !node.hidden && !node.classList.contains('is-dragging'))
                  .map(node => node.classList.contains('overview-drop-placeholder') ? 'slot' : node.dataset.card),
                left: document.querySelector('[data-card="pending"]').getBoundingClientRect().left};
            }""", {"x": point["x"] + index % 3 - 1, "y": point["y"] + index % 3 - 1}))
            page.wait_for_timeout(25)
        self.assertTrue(all(sample["order"] == samples[0]["order"] for sample in samples), samples)
        self.assertEqual(samples[0]["order"][:4], ["slot", "pending", "recent", "groups"])
        self.assertGreater(abs(samples[-1]["left"] - samples[0]["left"]), 10, samples)
        page.evaluate("window.dispatchEvent(new PointerEvent('pointercancel', {pointerId: 77, pointerType: 'touch'}))")
        self.assertEqual(page.locator(".overview-drop-placeholder").count(), 0)

    def test_mixed_width_live_previews_stay_stable_and_match_the_drop(self):
        page = self.page
        page.set_viewport_size({"width": 1200, "height": 2600})
        page.emulate_media(reduced_motion="no-preference")
        cases = (
            ("overall", "pending", .25, .25),
            ("overall", "recent", .75, .25),
            ("pending", "groups", .25, .75),
            ("groups", "overall", .25, .25),
            ("recent", "groups", .25, .75),
        )
        for source, target, xpart, ypart in cases:
            with self.subTest(source=source, target=target):
                page.reload()
                page.wait_for_function("!document.querySelector('#overview-customize').disabled")
                page.wait_for_function("document.querySelector('#overview-pending-count').textContent.includes('25 个动作')")
                page.locator("#overview-customize").click()
                result = page.evaluate("""async ({source, target, xpart, ypart}) => {
                  const grid = document.querySelector('#overview-grid');
                  const handle = grid.querySelector(`[data-card="${source}"] .overview-drag-handle`);
                  const start = handle.getBoundingClientRect();
                  const bounds = grid.querySelector(`[data-card="${target}"]`).getBoundingClientRect();
                  const point = {x: bounds.left + bounds.width * xpart, y: bounds.top + bounds.height * ypart};
                  const pointer = (type, x, y) => new PointerEvent(type, {pointerId: 80, pointerType: 'touch',
                    isPrimary: true, bubbles: true, cancelable: true, button: 0, clientX: x, clientY: y});
                  const order = () => [...grid.children].filter(node => !node.hidden && !node.classList.contains('is-dragging'))
                    .map(node => node.classList.contains('overview-drop-placeholder') ? 'slot' : node.dataset.card);
                  handle.dispatchEvent(pointer('pointerdown', start.left + 10, start.top + 10));
                  window.dispatchEvent(pointer('pointermove', point.x, point.y));
                  const previews = [order()];
                  for (let index = 0; index < 12; index++) {
                    await new Promise(resolve => setTimeout(resolve, 25));
                    window.dispatchEvent(pointer('pointermove', point.x + index % 3 - 1, point.y + index % 3 - 1));
                    previews.push(order());
                  }
                  window.dispatchEvent(pointer('pointerup', point.x, point.y));
                  return {previews, final: [...grid.querySelectorAll(':scope > [data-card]')]
                    .filter(node => !node.hidden).map(node => node.dataset.card)};
                }""", {"source": source, "target": target, "xpart": xpart, "ypart": ypart})
                self.assertTrue(all(preview == result["previews"][0] for preview in result["previews"]), result)
                expected = [source if item == "slot" else item for item in result["previews"][0]]
                self.assertEqual(result["final"], expected, result)
                self.assertNotEqual(result["final"][:4], ["pending", "overall", "recent", "groups"], result)

    def test_touch_drag_scrolls_at_the_edge_and_pointercancel_restores_the_draft(self):
        page = self.page
        page.set_viewport_size({"width": 390, "height": 700})
        page.wait_for_function("document.querySelector('#overview-pending-count').textContent.includes('25 个动作')")
        page.locator("#overview-customize").click()
        before = page.evaluate("""() => {
          for (const card of document.querySelectorAll('#overview-grid > [data-card]')) card.style.minHeight = '400px';
          document.querySelector('[data-card="groups"]').style.minHeight = '700px';
          const card = document.querySelector('[data-card="overall"]');
          card.style.setProperty('left', '11px', 'important');
          window.scrollTo(0, card.getBoundingClientRect().top + window.scrollY - 120);
          const handle = card.querySelector('.overview-drag-handle').getBoundingClientRect();
          const pointer = (type, x, y) => new PointerEvent(type, {pointerId: 78, pointerType: 'touch',
            isPrimary: true, bubbles: true, cancelable: true, button: 0, clientX: x, clientY: y});
          document.querySelector('[data-card="overall"] .overview-drag-handle')
            .dispatchEvent(pointer('pointerdown', handle.left + 10, handle.top + 10));
          window.dispatchEvent(pointer('pointermove', handle.left + 10, innerHeight - 10));
          return scrollY;
        }""")
        page.wait_for_timeout(450)
        during = page.evaluate("""() => {
          const grid = document.querySelector('#overview-grid');
          const order = [...grid.children].filter(node => !node.hidden && !node.classList.contains('is-dragging'))
            .map(node => node.classList.contains('overview-drop-placeholder') ? 'slot' : node.dataset.card);
          return {scroll: scrollY, order};
        }""")
        self.assertGreater(during["scroll"], before + 40, during)
        self.assertGreater(during["order"].index("slot"), 1, during)
        page.evaluate("window.dispatchEvent(new PointerEvent('pointercancel', {pointerId: 78, pointerType: 'touch'}))")
        after = page.evaluate("""() => {
          const card = document.querySelector('[data-card="overall"]');
          return {scroll: scrollY, left: card.style.getPropertyValue('left'), priority: card.style.getPropertyPriority('left'),
            position: getComputedStyle(card).position, markers: document.querySelectorAll('.overview-drop-placeholder').length,
            order: [...document.querySelectorAll('#overview-grid > [data-card]')].filter(node => !node.hidden).map(node => node.dataset.card),
            stored: sessionStorage.getItem('fixture-layout-overview')};
        }""")
        page.wait_for_timeout(150)
        self.assertAlmostEqual(page.evaluate("window.scrollY"), after["scroll"], delta=1)
        self.assertEqual((after["left"], after["priority"]), ("11px", "important"))
        self.assertNotEqual(after["position"], "fixed")
        self.assertEqual(after["markers"], 0)
        self.assertEqual(after["order"][:4], ["pending", "overall", "recent", "groups"])
        self.assertIsNone(after["stored"])

    def test_moved_card_stays_in_view_after_mouse_drag_without_jumping_to_top(self):
        from playwright.sync_api import expect

        page = self.page
        page.set_viewport_size({"width": 1200, "height": 1000})
        page.emulate_media(reduced_motion="no-preference")
        page.locator("#overview-customize").click()
        for card in ("overall", "recent"):
            page.locator(f'[data-card="{card}"] .overview-width').click()
        page.evaluate("""() => {
          for (const card of document.querySelectorAll('#overview-grid > [data-card]')) card.style.minHeight = '400px';
          const source = document.querySelector('[data-card="overall"]');
          window.scrollTo(0, source.getBoundingClientRect().top + window.scrollY - 150);
        }""")
        handle = page.locator('[data-card="overall"] .overview-drag-handle')
        target = page.locator('[data-card="recent"]')
        source_box, target_box = handle.bounding_box(), target.bounding_box()
        page.mouse.move(source_box["x"] + 10, source_box["y"] + 10)
        page.mouse.down()
        page.mouse.move(target_box["x"] + 25, target_box["y"] + target_box["height"] * .72, steps=6)
        expect(page.locator(".overview-drop-placeholder")).to_be_visible()
        before = page.evaluate("""() => {
          window.dropScrollFrames = [];
          window.captureDropScroll = true;
          const capture = () => {
            window.dropScrollFrames.push(window.scrollY);
            if (window.captureDropScroll) requestAnimationFrame(capture);
          };
          requestAnimationFrame(capture);
          return window.scrollY;
        }""")
        self.assertGreater(before, 300)
        page.mouse.up()
        page.wait_for_timeout(800)
        result = page.evaluate("""() => {
          window.captureDropScroll = false;
          const rect = document.querySelector('[data-card="overall"]').getBoundingClientRect();
          return {frames: window.dropScrollFrames, scroll: window.scrollY, top: rect.top, bottom: rect.bottom,
            height: innerHeight, header: parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop) || 0,
            order: [...document.querySelectorAll('#overview-grid > [data-card]')].filter(card => !card.hidden).map(card => card.dataset.card)};
        }""")
        self.assertEqual(result["order"][:4], ["pending", "recent", "overall", "groups"])
        self.assertTrue(result["frames"])
        self.assertGreater(min(result["frames"]), min(before, result["scroll"]) - 40, result)
        self.assertGreaterEqual(result["top"], result["header"] - 5, result)
        self.assertLessEqual(result["bottom"], result["height"] + 5, result)

    def test_keyboard_move_follows_card_with_and_without_reduced_motion(self):
        page = self.page
        page.set_viewport_size({"width": 1200, "height": 650})
        for reduced_motion in ("reduce", "no-preference"):
            with self.subTest(reduced_motion=reduced_motion):
                page.emulate_media(reduced_motion=reduced_motion)
                page.reload()
                page.wait_for_function("!document.querySelector('#overview-customize').disabled")
                page.wait_for_function("document.querySelector('#overview-pending-count').textContent.includes('25 个动作')")
                page.locator("#overview-customize").click()
                for card in ("overall", "recent"):
                    page.locator(f'[data-card="{card}"] .overview-width').click()
                page.evaluate("""() => {
                  for (const card of document.querySelectorAll('#overview-grid > [data-card]')) card.style.minHeight = '400px';
                  const card = document.querySelector('[data-card="recent"]');
                  window.scrollTo(0, card.getBoundingClientRect().top + window.scrollY - 200);
                }""")
                handle = page.locator('[data-card="recent"] .overview-drag-handle')
                handle.focus()
                before = page.evaluate("window.scrollY")
                self.assertGreater(before, 300)
                page.evaluate("""() => {
                  window.moveScrollFrames = [];
                  window.captureMoveScroll = true;
                  const capture = () => {
                    window.moveScrollFrames.push(window.scrollY);
                    if (window.captureMoveScroll) requestAnimationFrame(capture);
                  };
                  requestAnimationFrame(capture);
                }""")
                page.keyboard.press("ArrowUp")
                page.wait_for_timeout(800)
                result = page.evaluate("""() => {
                  window.captureMoveScroll = false;
                  const card = document.querySelector('[data-card="recent"]');
                  const rect = card.getBoundingClientRect();
                  return {frames: window.moveScrollFrames, scroll: window.scrollY, top: rect.top, bottom: rect.bottom,
                    height: innerHeight, header: parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop) || 0,
                    focused: card.contains(document.activeElement)};
                }""")
                self.assertTrue(result["frames"])
                self.assertGreater(min(result["frames"]), min(before, result["scroll"]) - 40, result)
                self.assertGreaterEqual(result["top"], result["header"] - 5, result)
                self.assertLessEqual(result["bottom"], result["height"] + 5, result)
                self.assertTrue(result["focused"], result)
                if reduced_motion == "no-preference":
                    self.assertGreater(len(set(round(y) for y in result["frames"])), 2, result)

    def test_follow_adjusts_when_content_above_changes_during_the_move(self):
        page = self.page
        page.set_viewport_size({"width": 1200, "height": 650})
        page.emulate_media(reduced_motion="no-preference")
        page.locator("#overview-customize").click()
        for card in ("overall", "recent"):
            page.locator(f'[data-card="{card}"] .overview-width').click()
        page.evaluate("""() => {
          for (const card of document.querySelectorAll('#overview-grid > [data-card]')) card.style.minHeight = '400px';
          const card = document.querySelector('[data-card="recent"]');
          window.scrollTo(0, card.getBoundingClientRect().top + window.scrollY - 200);
        }""")
        page.locator('[data-card="recent"] .overview-drag-handle').focus()
        page.keyboard.press("ArrowUp")
        page.wait_for_timeout(90)
        page.evaluate("document.querySelector('[data-card=\"pending\"]').style.minHeight = '750px'")
        page.wait_for_timeout(600)
        result = page.evaluate("""() => {
          const rect = document.querySelector('[data-card="recent"]').getBoundingClientRect();
          return {top: rect.top, bottom: rect.bottom, height: innerHeight};
        }""")
        self.assertGreaterEqual(result["top"], 80, result)
        self.assertLessEqual(result["bottom"], result["height"] + 5, result)

    def test_touch_move_follows_tall_card_and_keeps_handle_visible(self):
        page = self.page
        page.set_viewport_size({"width": 390, "height": 700})
        page.emulate_media(reduced_motion="no-preference")
        page.locator("#overview-customize").click()
        result = page.evaluate("""() => {
          const card = document.querySelector('[data-card="pending"]');
          const target = document.querySelector('[data-card="overall"]');
          const handle = card.querySelector('.overview-drag-handle');
          card.style.minHeight = '1000px';
          target.style.minHeight = '400px';
          window.scrollTo(0, card.getBoundingClientRect().top + window.scrollY - 100);
          const start = handle.getBoundingClientRect();
          const pointer = (type, x, y) => new PointerEvent(type, {pointerId: 75, pointerType: 'touch',
            isPrimary: true, bubbles: true, cancelable: true, button: 0, clientX: x, clientY: y});
          handle.dispatchEvent(pointer('pointerdown', start.left + 10, start.top + 10));
          window.scrollTo(0, target.getBoundingClientRect().top + window.scrollY - 100);
          const end = target.getBoundingClientRect();
          const x = end.left + 40, y = end.top + end.height * .75;
          window.dispatchEvent(pointer('pointermove', x, y));
          const marker = Boolean(document.querySelector('.overview-drop-placeholder'));
          const scroll = window.scrollY;
          window.dispatchEvent(pointer('pointerup', x, y));
          return {marker, scroll};
        }""")
        self.assertTrue(result["marker"])
        self.assertGreater(result["scroll"], 300)
        page.wait_for_timeout(700)
        result = page.evaluate("""() => {
          const card = document.querySelector('[data-card="pending"]');
          const handle = card.querySelector('.overview-drag-handle').getBoundingClientRect();
          return {height: card.getBoundingClientRect().height, handleTop: handle.top, handleBottom: handle.bottom,
            viewport: innerHeight, focused: card.contains(document.activeElement),
            order: [...document.querySelectorAll('#overview-grid > [data-card]')].filter(node => !node.hidden).map(node => node.dataset.card)};
        }""")
        self.assertEqual(result["order"][:2], ["overall", "pending"])
        self.assertGreater(result["height"], result["viewport"])
        self.assertGreaterEqual(result["handleTop"], 0, result)
        self.assertLessEqual(result["handleBottom"], result["viewport"], result)
        self.assertTrue(result["focused"], result)

    def test_original_position_drop_does_not_scroll_or_reorder(self):
        page = self.page
        page.emulate_media(reduced_motion="no-preference")
        page.locator("#overview-customize").click()
        before = page.evaluate("""() => {
          const card = document.querySelector('[data-card="pending"]');
          card.style.minHeight = '900px';
          window.scrollTo(0, card.getBoundingClientRect().top + window.scrollY - 100);
          const handle = card.querySelector('.overview-drag-handle');
          const rect = handle.getBoundingClientRect();
          const pointer = (type, x, y) => new PointerEvent(type, {pointerId: 76, pointerType: 'touch',
            isPrimary: true, bubbles: true, cancelable: true, button: 0, clientX: x, clientY: y});
          handle.dispatchEvent(pointer('pointerdown', rect.left + 10, rect.top + 10));
          window.dispatchEvent(pointer('pointermove', rect.left + 18, rect.top + 11));
          const scroll = window.scrollY;
          window.dispatchEvent(pointer('pointerup', rect.left + 18, rect.top + 11));
          return scroll;
        }""")
        page.wait_for_timeout(500)
        result = page.evaluate("""() => ({scroll: window.scrollY,
          order: [...document.querySelectorAll('#overview-grid > [data-card]')].filter(node => !node.hidden).map(node => node.dataset.card),
          markers: document.querySelectorAll('.overview-drop-placeholder').length})""")
        self.assertAlmostEqual(result["scroll"], before, delta=1)
        self.assertEqual(result["order"][:4], ["pending", "overall", "recent", "groups"])
        self.assertEqual(result["markers"], 0)

    def test_original_position_drop_returns_smoothly_or_immediately_with_reduced_motion(self):
        page = self.page
        page.set_viewport_size({"width": 1200, "height": 1800})
        for reduced in ("no-preference", "reduce"):
            with self.subTest(reduced_motion=reduced):
                page.emulate_media(reduced_motion=reduced)
                page.reload()
                page.wait_for_function("!document.querySelector('#overview-customize').disabled")
                page.wait_for_function("document.querySelector('#overview-pending-count').textContent.includes('25 个动作')")
                page.locator("#overview-customize").click()
                result = page.evaluate("""() => {
                  const card = document.querySelector('[data-card="overall"]');
                  const handle = card.querySelector('.overview-drag-handle');
                  const rect = handle.getBoundingClientRect();
                  const pointer = (type, x, y) => new PointerEvent(type, {pointerId: 79, pointerType: 'touch',
                    isPrimary: true, bubbles: true, cancelable: true, button: 0, clientX: x, clientY: y});
                  const scroll = scrollY;
                  handle.dispatchEvent(pointer('pointerdown', rect.left + 10, rect.top + 10));
                  window.dispatchEvent(pointer('pointermove', rect.left + 50, rect.top + 30));
                  window.dispatchEvent(pointer('pointerup', rect.left + 50, rect.top + 30));
                  return {animations: card.getAnimations().length, before: scroll, after: scrollY,
                    markers: document.querySelectorAll('.overview-drop-placeholder').length};
                }""")
                self.assertEqual(result["markers"], 0)
                self.assertAlmostEqual(result["before"], result["after"], delta=1)
                self.assertEqual(result["animations"] > 0, reduced == "no-preference", result)

    def test_move_buttons_follow_rapid_moves_and_stop_for_manual_scrolling(self):
        page = self.page
        page.set_viewport_size({"width": 1200, "height": 650})
        page.emulate_media(reduced_motion="no-preference")
        page.locator("#overview-customize").click()
        for card in ("overall", "recent"):
            page.locator(f'[data-card="{card}"] .overview-width').click()
        page.evaluate("""() => {
          for (const card of document.querySelectorAll('#overview-grid > [data-card]')) card.style.minHeight = '400px';
          const card = document.querySelector('[data-card="overall"]');
          window.scrollTo(0, card.getBoundingClientRect().top + window.scrollY - 160);
        }""")
        page.get_by_role("button", name="下移 整体状态").click()
        page.keyboard.press("ArrowUp")
        page.wait_for_timeout(700)
        result = page.evaluate("""() => {
          const card = document.querySelector('[data-card="overall"]');
          const rect = card.getBoundingClientRect();
          return {top: rect.top, bottom: rect.bottom, viewport: innerHeight, focused: card.contains(document.activeElement),
            order: [...document.querySelectorAll('#overview-grid > [data-card]')].filter(node => !node.hidden).map(node => node.dataset.card)};
        }""")
        self.assertEqual(result["order"][:4], ["pending", "overall", "recent", "groups"])
        self.assertGreaterEqual(result["top"], 80, result)
        self.assertLessEqual(result["bottom"], result["viewport"] + 1, result)
        self.assertTrue(result["focused"], result)
        page.get_by_role("button", name="下移 整体状态").click()
        page.wait_for_timeout(70)
        page.mouse.move(1100, 500)
        page.mouse.wheel(0, 180)
        page.wait_for_timeout(150)
        after_wheel = page.evaluate("window.scrollY")
        page.wait_for_timeout(400)
        self.assertAlmostEqual(page.evaluate("window.scrollY"), after_wheel, delta=1)

    def test_last_card_follows_within_page_boundaries(self):
        page = self.page
        page.set_viewport_size({"width": 1200, "height": 650})
        page.locator("#overview-customize").click()
        page.locator('[data-card="recent"] .overview-width').click()
        page.evaluate("""() => {
          for (const card of document.querySelectorAll('#overview-grid > [data-card]')) card.style.minHeight = '400px';
          const card = document.querySelector('[data-card="recent"]');
          window.scrollTo(0, card.getBoundingClientRect().top + window.scrollY - 160);
        }""")
        page.get_by_role("button", name="下移 近期处理").click()
        result = page.evaluate("""() => {
          const rect = document.querySelector('[data-card="recent"]').getBoundingClientRect();
          return {top: rect.top, bottom: rect.bottom, height: innerHeight, scroll: scrollY,
            max: document.scrollingElement.scrollHeight - innerHeight};
        }""")
        self.assertGreaterEqual(result["top"], 80, result)
        self.assertLessEqual(result["bottom"], result["height"] + 1, result)
        self.assertGreaterEqual(result["scroll"], 0)
        self.assertLessEqual(result["scroll"], result["max"] + 1)

    def test_card_tools_align_and_use_the_same_grip_as_rule_actions(self):
        page = self.page
        page.locator("#overview-customize").click()
        for width in (1200, 390):
            for theme in ("light", "dark"):
                with self.subTest(width=width, theme=theme):
                    page.set_viewport_size({"width": width, "height": 850})
                    page.emulate_media(color_scheme=theme)
                    offsets = page.locator(".overview-card:visible").evaluate_all("""cards => cards.map(card => {
                      const tools = card.querySelector('.overview-card-tools');
                      const grip = tools.querySelector('.overview-drag-handle');
                      const style = getComputedStyle(grip, '::before');
                      return {top: grip.getBoundingClientRect().top - card.getBoundingClientRect().top,
                        width: tools.getBoundingClientRect().width,
                        innerWidth: card.clientWidth - parseFloat(getComputedStyle(card).paddingLeft)
                          - parseFloat(getComputedStyle(card).paddingRight),
                        iconWidth: style.width, iconHeight: style.height, iconShadow: style.boxShadow};
                    })""")
                    self.assertLessEqual(max(item["top"] for item in offsets) - min(item["top"] for item in offsets), 1)
                    for item in offsets:
                        self.assertLessEqual(abs(item["width"] - item["innerWidth"]), 1)
                        self.assertEqual((item["iconWidth"], item["iconHeight"]), ("3px", "3px"))
                        self.assertNotEqual(item["iconShadow"], "none")

    def test_half_width_drag_keeps_a_stable_preview_at_a_fixed_pointer(self):
        from playwright.sync_api import expect
        page = self.page
        page.set_viewport_size({"width": 1200, "height": 1800})
        page.locator("#overview-customize").click()
        for card in ("pending", "groups"):
            page.locator(f'[data-card="{card}"] .overview-width').click()
        source = page.locator('[data-card="overall"] .overview-drag-handle')
        target = page.locator('[data-card="pending"]')
        origin, destination = source.bounding_box(), target.bounding_box()
        result = page.evaluate("""({origin, destination}) => {
          const handle = document.querySelector('[data-card="overall"] .overview-drag-handle');
          const point = {x: destination.x + destination.width * .1, y: destination.y + destination.height * .9};
          const pointer = (type, p) => new PointerEvent(type, {pointerId: 73, pointerType: 'touch', isPrimary: true,
            bubbles: true, cancelable: true, button: 0, clientX: p.x, clientY: p.y});
          handle.dispatchEvent(pointer('pointerdown', {x: origin.x + 5, y: origin.y + 5}));
          const layouts = [], markers = [];
          for (let index = 0; index < 12; index++) {
            window.dispatchEvent(pointer('pointermove', {x: point.x + index % 3 - 1, y: point.y + index % 3 - 1}));
            layouts.push([...document.querySelectorAll('#overview-grid > [data-card]')].filter(card => !card.hidden)
              .map(card => { const rect = card.getBoundingClientRect(); return [card.dataset.card, rect.x, rect.y]; }));
            const marker = document.querySelector('.overview-drop-placeholder');
            markers.push(marker ? [marker.getBoundingClientRect().x, marker.getBoundingClientRect().y] : null);
          }
          window.dispatchEvent(pointer('pointerup', point));
          return {layouts, markers};
        }""", {"origin": origin, "destination": destination})
        self.assertIsNotNone(result["markers"][0])
        self.assertTrue(all(layout == result["layouts"][0] for layout in result["layouts"]))
        self.assertTrue(all(marker == result["markers"][0] for marker in result["markers"]))
        expect(page.locator(".overview-drop-placeholder")).to_have_count(0)
        order = page.locator("#overview-grid > [data-card]:visible").evaluate_all("cards => cards.map(card => card.dataset.card)")
        self.assertEqual(order[:4], ["overall", "pending", "recent", "groups"])

    def test_long_single_column_card_previews_reordering_above_the_viewport(self):
        page = self.page
        page.set_viewport_size({"width": 390, "height": 700})
        page.locator("#overview-customize").click()
        result = page.evaluate("""() => {
          const target = document.querySelector('[data-card="pending"]');
          const handle = document.querySelector('[data-card="overall"] .overview-drag-handle');
          target.style.minHeight = '1400px';
          window.scrollTo(0, target.getBoundingClientRect().top + window.scrollY + 400);
          const before = target.getBoundingClientRect().top;
          const pointer = (type, x, y) => new PointerEvent(type, {pointerId: 74, pointerType: 'touch',
            isPrimary: true, bubbles: true, cancelable: true, button: 0, clientX: x, clientY: y});
          handle.dispatchEvent(pointer('pointerdown', 200, 500));
          window.dispatchEvent(pointer('pointermove', 200, 200));
          const hint = document.querySelector('#overview-grid > .overview-drop-placeholder');
          const result = {slot: Boolean(hint), shifted: target.getBoundingClientRect().top > before,
            first: [...document.querySelector('#overview-grid').children].filter(node => !node.hidden &&
              !node.classList.contains('is-dragging'))[0] === hint};
          window.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
          return result;
        }""")
        self.assertTrue(result["slot"], result)
        self.assertTrue(result["shifted"], result)
        self.assertTrue(result["first"], result)

    def test_failures_preserve_layout_and_do_not_show_unavailable_pending_as_zero(self):
        from playwright.sync_api import expect
        page = self.page
        page.evaluate("window.quickStartMock.failLayoutSave = true")
        page.locator("#overview-customize").click()
        page.locator('[data-visibility="pending"]').uncheck()
        page.locator("#overview-save").click()
        expect(page.locator("#overview-edit-status")).to_contain_text("保存失败")
        expect(page.locator("#overview-editor")).to_be_visible()
        expect(page.locator('[data-card="pending"]')).to_be_hidden()
        page.evaluate("window.quickStartMock.failLayoutSave = false")
        page.locator("#overview-cancel").click()
        expect(page.locator('[data-card="pending"]')).to_be_visible()
        page.evaluate("window.quickStartMock.failPending = true; window.SentinelOverview.refresh()")
        expect(page.locator("#overview-pending-count")).to_have_text("待确认列表不可用.")
        expect(page.locator("#overview-pending-status")).to_contain_text("无法读取")
        self.assertEqual(page.locator(".overview-pending-message").count(), 0)
        page.evaluate("window.quickStartMock.failPending = false; window.SentinelOverview.refresh()")
        expect(page.locator("#overview-pending-count")).to_have_text("25 个动作, 涉及 24 条消息")
        expect(page.locator("#overview-pending-status")).to_have_text("")

    def test_confirmation_failures_and_expired_actions_report_their_actual_state(self):
        from playwright.sync_api import expect
        cases = (
            ({"status": "not_claimed", "reason_code": "intent_expired"}, "动作已到期, 禁言未执行."),
            ({"status": "not_claimed", "reason_code": "rule_version_changed"}, "规则版本已变化, 禁言未执行."),
            ({"status": "failed"}, "禁言执行失败, 请查看判定记录."),
            ({"status": "unknown"}, "执行结果不明, 请人工核查."),
        )
        page = self.page
        for result, message in cases:
            with self.subTest(result=result):
                page.reload()
                expect(page.locator("#overview-pending-count")).to_have_text("25 个动作, 涉及 24 条消息")
                page.evaluate("value => { window.quickStartMock.confirmationResult = value; }", result)
                first = page.locator(".overview-pending-message").first
                first.locator("summary").click()
                first.get_by_role("button", name=re.compile("确认并尝试禁言")).first.click()
                expect(page.locator("#feedback-region")).to_contain_text(message)
                expect(page.locator("#overview-pending-count")).to_have_text("24 个动作, 涉及 24 条消息")
                self.assertEqual(page.evaluate("window.quickStartMock.confirmationPosts"), [{"intent_id": 1}])
        page.reload()
        expect(page.locator("#overview-pending-count")).to_have_text("25 个动作, 涉及 24 条消息")
        page.evaluate("window.quickStartMock.failConfirmation = true")
        first = page.locator(".overview-pending-message").first
        first.locator("summary").click()
        first.get_by_role("button", name=re.compile("确认并尝试禁言")).first.click()
        expect(page.locator("#feedback-region")).to_contain_text("无法确认动作结果, 请查看判定记录后再操作.")
        expect(page.locator("#overview-pending-count")).to_have_text("25 个动作, 涉及 24 条消息")
        self.assertEqual(page.evaluate("window.quickStartMock.confirmationPosts"), [{"intent_id": 1}])

    def test_touch_drag_shows_no_placeholder_in_original_position_and_escape_cancels(self):
        from playwright.sync_api import expect
        page = self.page
        page.locator("#overview-customize").click()
        handle = page.locator('[data-card="overall"] .overview-drag-handle')
        target = page.locator('[data-card="recent"]')
        handle.scroll_into_view_if_needed()
        source, other = handle.bounding_box(), target.bounding_box()
        x, y = source["x"] + 10, source["y"] + 10
        handle.dispatch_event("pointerdown", {"pointerId": 73, "pointerType": "touch", "isPrimary": True,
                                                "button": 0, "clientX": x, "clientY": y})
        page.evaluate("""p => window.dispatchEvent(new PointerEvent('pointermove', {pointerId: 73,
            pointerType: 'touch', isPrimary: true, bubbles: true, cancelable: true, clientX: p.x, clientY: p.y}))""",
            {"x": x + 8, "y": y + 1})
        expect(page.locator("#overview-grid > .overview-drop-placeholder")).to_be_hidden()
        page.evaluate("""p => window.dispatchEvent(new PointerEvent('pointermove', {pointerId: 73,
            pointerType: 'touch', isPrimary: true, bubbles: true, cancelable: true, clientX: p.x, clientY: p.y}))""",
            {"x": other["x"] + other["width"] - 10, "y": other["y"] + 20})
        expect(page.locator(".overview-drop-placeholder")).to_be_visible()
        page.evaluate("""p => window.dispatchEvent(new PointerEvent('pointermove', {pointerId: 73,
            pointerType: 'touch', isPrimary: true, bubbles: true, cancelable: true, clientX: p.x, clientY: p.y}))""",
            {"x": x + 8, "y": y + 1})
        expect(page.locator("#overview-grid > .overview-drop-placeholder")).to_be_hidden()
        page.evaluate("""p => window.dispatchEvent(new PointerEvent('pointermove', {pointerId: 73,
            pointerType: 'touch', isPrimary: true, bubbles: true, cancelable: true, clientX: p.x, clientY: p.y}))""",
            {"x": other["x"] + other["width"] - 10, "y": other["y"] + 20})
        expect(page.locator(".overview-drop-placeholder")).to_be_visible()
        page.keyboard.press("Escape")
        self.assertEqual(page.locator(".overview-drop-placeholder").count(), 0)
        expect(page.locator("#overview-order-status")).to_have_text("已取消排序.")
        order = page.locator("#overview-grid > [data-card]:visible").evaluate_all("cards => cards.map(card => card.dataset.card)")
        self.assertEqual(order[:4], ["pending", "overall", "recent", "groups"])

    def test_unknown_cards_survive_editing_and_saving(self):
        from playwright.sync_api import expect
        page = self.page
        page.evaluate("""() => sessionStorage.setItem('fixture-layout-overview', JSON.stringify({cards: [
          {id: 'future-card', width: 'half', visible: false}, {id: 'pending', width: 'half', visible: true}]}))""")
        page.reload()
        expect(page.locator("#overview-customize")).to_be_enabled()
        expect(page.locator('[data-card="pending"]')).to_have_attribute("data-width", "half")
        page.locator("#overview-customize").click()
        page.locator("#overview-save").click()
        page.wait_for_function("!document.querySelector('#overview-customize').disabled")
        layout = page.evaluate("JSON.parse(sessionStorage.getItem('fixture-layout-overview'))")
        self.assertIn({"id": "future-card", "width": "half", "visible": False}, layout["cards"])

    def test_layout_read_error_cannot_overwrite_saved_state(self):
        from playwright.sync_api import expect
        page = self.page
        page.goto(self.url + "?scenario=layout-read-error")
        expect(page.locator("#overview-layout-retry")).to_be_visible()
        expect(page.locator("#overview-customize")).to_be_disabled()
        expect(page.locator("#overview-layout-status")).to_contain_text("不会被覆盖")
        self.assertIsNone(page.evaluate("sessionStorage.getItem('fixture-layout-layout-read-error')"))
        page.evaluate("window.quickStartMock.failLayoutRead = false")
        page.locator("#overview-layout-retry").click()
        expect(page.locator("#overview-customize")).to_be_enabled()

    def test_realtime_status_and_updates_follow_the_selected_scope(self):
        from playwright.sync_api import expect
        page = self.page
        page.wait_for_function("Boolean(window.quickStartMock.stream)")
        page.evaluate("window.quickStartMock.stream.onOpen()")
        expect(page.locator("#overview-pending-live")).to_have_text("实时 · 已连接")
        page.evaluate("""() => {
          window.quickStartMock.pending.splice(0, 1);
          window.quickStartMock.stream.onMessage({parsed: {message_id: 'same', id: 999}});
        }""")
        expect(page.locator("#overview-pending-count")).to_have_text("23 个动作, 涉及 23 条消息")
        page.evaluate("window.oldStream = window.quickStartMock.stream; window.oldStream.onError()")
        expect(page.locator("#overview-pending-live")).to_have_text("实时 · 重连中")
        page.wait_for_function("window.quickStartMock.stream !== window.oldStream")
        page.evaluate("window.quickStartMock.stream.onError()")
        expect(page.locator("#overview-pending-live")).to_have_text("已断开, 使用轮询")

    def test_responsive_light_and_dark_layouts(self):
        from playwright.sync_api import expect
        for width in (1440, 768, 390, 320):
            for theme in ("light", "dark"):
                with self.subTest(width=width, theme=theme):
                    self.page.set_viewport_size({"width": width, "height": 850})
                    self.page.emulate_media(color_scheme=theme)
                    expect(self.page.locator("#overview-customize")).to_be_visible()
                    expect(self.page.locator('[data-card="pending"]')).to_be_visible()
                    self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), width)

    def test_status_text_and_progress_have_no_redundant_status_dots(self):
        from playwright.sync_api import expect
        page = self.page
        page.locator(".overview-pending-message summary").first.click()
        expect(page.locator(".overview-pending-message").first.locator(".action-progress li")).to_have_count(6)
        for selector in ("#status-label", ".action-progress li"):
            before = page.locator(selector).first.evaluate("node => getComputedStyle(node, '::before').content")
            self.assertIn(before, ("none", "normal"))
        self.assertNotEqual(page.locator("#status-label").evaluate("node => getComputedStyle(node).color"),
                            page.locator("#status-main").evaluate("node => getComputedStyle(node).color"))
