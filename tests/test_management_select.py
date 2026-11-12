import importlib.util
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VIEW = ROOT / "views" / "management"
HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<link rel="stylesheet" href="/style.css"></head><body>
<label>结果 <select id="result" aria-describedby="help">
<option value="all">全部</option><option value="proposed">拟执行</option>
<option value="disabled" disabled>不可用</option><option value="failed">全部失败</option>
</select><span id="help">筛选结果</span></label>
<label for="action">执行方式</label><select id="action" class="action-type">
<option value="auto">自动</option><option value="confirm">人工确认</option></select>
<label>模型名称 <input id="model" list="models"><datalist id="models">
<option value="model-alpha"></option><option value="model-beta"></option></datalist></label>
<div id="dynamic"></div><button id="outside" type="button">其他</button>
<div style="height: 1000px"></div><label>底部选择 <select id="bottom"></select></label>
<div style="height: 100px"></div><script>
for (let i = 0; i < 15; i++) document.querySelector('#bottom').add(new Option('选项 ' + i, String(i)));
window.events = []; for (const type of ['input', 'change']) document.querySelector('#result').addEventListener(type, () => window.events.push(type));
</script><script type="module" src="/select.js"></script></body></html>"""
BRIDGE = """<script>
window.AstrBotPluginView = {
  async ready() {},
  async apiGet(endpoint) {
    const data = {
      status: {monitoring: 'dry_run', backend: 'llm'},
      settings: {monitoring_mode: 'allowlist', monitoring_groups: [], decision_backend: 'llm',
        rule_selection_strategy: 'priority', llm_provider_id: 'test-model', default_action_mode: 'auto',
        default_execution_mode: 'record', protected_target_policy: 'exempt', protected_target_template: 'hello',
        message_retention_days: 30, jev_provider: 'typesafe', llm_no_action_reason_enabled: true},
      providers: [{id: 'test-model'}], 'ui-state': {platform_id: 'p', group_id: 'g'},
      platforms: [{id: 'p', type: 'qq_official'}],
      groups: [{platform_id: 'p', group_id: 'g', alias: '测试群', rule_count: 0}],
      rules: [], messages: [], summary: {total_messages: 0, status_counts: {}, recent_24h: {messages: 0, proposed: 0, no_action: 0}},
    };
    return data[endpoint] ?? [];
  },
  async apiPost() { return {}; },
  async subscribeSSE() { return 'test'; }, async unsubscribeSSE() {},
};
</script>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            body = HTML.encode("utf-8")
            content_type = "text/html; charset=utf-8"
        elif self.path == "/management":
            html = (VIEW / "index.html").read_text(encoding="utf-8")
            body = html.replace('<script type="module" src="./select.js">', BRIDGE + '<script type="module" src="./select.js">').encode("utf-8")
            content_type = "text/html; charset=utf-8"
        elif self.path in ("/select.js", "/style.css", "/app.js", "/motion.js"):
            body = (VIEW / self.path[1:]).read_bytes()
            content_type = "text/javascript; charset=utf-8" if self.path.endswith(".js") else "text/css; charset=utf-8"
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
class ManagementSelectTests(unittest.TestCase):
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
        self.page = self.browser.new_page(viewport={"width": 800, "height": 600})
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(self.url, wait_until="load")
        self.page.wait_for_selector("#result + .custom-select-trigger")

    def tearDown(self):
        self.page.close()
        self.assertEqual(self.errors, [])

    def trigger(self, name):
        return self.page.locator(f"#{name} + .custom-select-trigger")

    def test_selection_events_and_programmatic_updates(self):
        from playwright.sync_api import expect

        trigger = self.trigger("result")
        expect(trigger).to_have_attribute("aria-label", "结果: 全部")
        trigger.click()
        self.page.get_by_role("option", name="拟执行").click()
        self.assertEqual(self.page.locator("#result").input_value(), "proposed")
        self.assertEqual(self.page.evaluate("window.events"), ["input", "change"])
        expect(trigger).to_have_attribute("aria-label", "结果: 拟执行")
        trigger.click()
        self.page.get_by_role("option", name="拟执行").click()
        self.assertEqual(self.page.evaluate("window.events"), ["input", "change"])
        self.page.evaluate("document.querySelector('#result').value = 'failed'")
        expect(trigger).to_have_attribute("aria-label", "结果: 全部失败")
        self.page.evaluate("document.querySelector('#result option[value=failed]').textContent = '<img src=x>'")
        expect(trigger).to_have_attribute("aria-label", "结果: <img src=x>")
        trigger.click()
        expect(self.page.get_by_role("option", name="<img src=x>")).to_be_visible()
        self.assertEqual(self.page.locator("#custom-select-menu img").count(), 0)
        self.page.evaluate("document.querySelector('#result').disabled = true")
        expect(trigger).to_be_disabled()
        expect(trigger).to_have_attribute("aria-expanded", "false")
        self.page.evaluate("document.querySelector('#result').disabled = false")
        expect(trigger).to_be_enabled()

    def test_rapid_toggles_do_not_leave_a_stale_menu(self):
        from playwright.sync_api import expect

        trigger = self.trigger("result")
        trigger.evaluate("element => { element.click(); element.click(); element.click(); }")
        expect(trigger).to_have_attribute("aria-expanded", "true")
        expect(self.page.get_by_role("option", name="拟执行")).to_be_visible()
        trigger.evaluate("element => { element.click(); element.click(); element.click(); }")
        expect(trigger).to_have_attribute("aria-expanded", "false")
        expect(self.page.locator("#custom-select-menu")).to_have_attribute("aria-hidden", "true")

    def test_native_controls_remain_available_without_script(self):
        from playwright.sync_api import expect

        self.page.route("**/select.js", lambda route: route.abort())
        self.page.reload(wait_until="load")
        expect(self.page.locator("#result")).to_be_visible()
        expect(self.page.locator("#model")).to_have_attribute("list", "models")

    def test_keyboard_and_labels(self):
        from playwright.sync_api import expect

        trigger = self.trigger("result")
        self.page.locator("#result").evaluate("element => element.focus()")
        expect(trigger).to_be_focused()
        self.page.keyboard.press("ArrowDown")
        expect(trigger).to_have_attribute("aria-expanded", "true")
        self.page.keyboard.press("ArrowDown")
        self.page.keyboard.press("ArrowDown")
        self.page.keyboard.press("Enter")
        self.assertEqual(self.page.locator("#result").input_value(), "failed")
        expect(trigger).to_be_focused()
        self.page.keyboard.press("Space")
        self.page.keyboard.press("Escape")
        expect(trigger).to_have_attribute("aria-expanded", "false")
        self.page.locator("label[for=action]").click()
        expect(self.trigger("action")).to_have_attribute("aria-expanded", "true")
        expect(self.trigger("action")).to_have_attribute("aria-label", "执行方式: 自动")
        self.page.locator("#outside").click()
        expect(self.trigger("action")).to_have_attribute("aria-expanded", "false")

    def test_dynamic_options_and_controls(self):
        from playwright.sync_api import expect

        self.page.evaluate("""() => {
          const label = document.createElement('label'); label.append('选择群 ');
          const select = document.createElement('select'); select.id = 'dynamic-select';
          select.add(new Option('所有群', '')); select.add(new Option('测试群', 'test'));
          label.append(select); document.querySelector('#dynamic').append(label);
        }""")
        trigger = self.trigger("dynamic-select")
        expect(trigger).to_have_attribute("aria-label", "选择群: 所有群")
        self.page.evaluate("document.querySelector('#dynamic-select').value = 'test'")
        expect(trigger).to_have_attribute("aria-label", "选择群: 测试群")
        self.page.evaluate("""() => {
          const select = document.querySelector('#dynamic-select');
          select.replaceChildren(new Option('新的群', 'new')); select.value = 'new';
        }""")
        expect(trigger).to_have_attribute("aria-label", "选择群: 新的群")
        trigger.click()
        expect(self.page.get_by_role("option", name="新的群")).to_be_visible()
        self.page.evaluate("document.querySelector('#dynamic').replaceChildren()")
        expect(self.page.locator("#custom-select-menu")).to_have_attribute("aria-hidden", "true")

    def test_editable_suggestions_filter_and_keep_custom_values(self):
        from playwright.sync_api import expect

        model = self.page.locator("#model")
        model.focus()
        expect(model).to_have_attribute("aria-expanded", "true")
        expect(self.page.get_by_role("option", name="model-alpha")).to_be_visible()
        model.fill("beta")
        expect(self.page.get_by_role("option", name="model-beta")).to_be_visible()
        self.assertEqual(self.page.get_by_role("option", name="model-alpha").count(), 0)
        self.page.keyboard.press("ArrowDown")
        self.page.keyboard.press("Enter")
        expect(model).to_have_value("model-beta")
        expect(model).to_have_attribute("aria-expanded", "false")
        model.fill("custom-model")
        expect(model).to_have_attribute("aria-expanded", "false")
        self.page.get_by_role("button", name="显示模型名称选项").click()
        expect(self.page.get_by_role("option", name="model-alpha")).to_be_visible()
        expect(self.page.get_by_role("option", name="model-beta")).to_be_visible()
        expect(model).to_have_value("custom-model")
        self.page.keyboard.press("Escape")
        self.page.locator("#outside").click()
        expect(model).to_have_value("custom-model")
        self.page.evaluate("document.querySelector('#models').append(new Option('custom-model-two', 'custom-model-two'))")
        model.focus()
        expect(self.page.get_by_role("option", name="custom-model-two")).to_be_visible()
        self.page.get_by_role("option", name="custom-model-two").click()
        expect(model).to_have_value("custom-model-two")
        expect(model).to_have_attribute("aria-expanded", "false")

    def test_placement_theme_and_reduced_motion(self):
        from playwright.sync_api import expect

        self.page.emulate_media(reduced_motion="reduce")
        self.page.evaluate("document.documentElement.dataset.theme = 'dark'")
        trigger = self.trigger("bottom")
        trigger.scroll_into_view_if_needed()
        self.page.evaluate("""() => {
          const rect = document.querySelector('#bottom + button').getBoundingClientRect();
          window.scrollBy(0, rect.bottom - innerHeight + 40);
        }""")
        trigger.click()
        menu = self.page.locator("#custom-select-menu")
        expect(menu).to_have_class("custom-select-menu opens-up is-open")
        box = menu.bounding_box()
        self.assertGreaterEqual(box["y"], 0)
        self.assertLessEqual(box["y"] + box["height"], 600)
        self.assertEqual(menu.evaluate("element => getComputedStyle(element).backgroundColor"), "rgb(26, 26, 26)")
        duration = menu.evaluate("element => getComputedStyle(element).transitionDuration")
        self.assertNotIn("0.44s", duration)

    def test_management_page_dynamic_rule_controls(self):
        from playwright.sync_api import expect

        self.page.goto(self.url + "management", wait_until="load")
        expect(self.trigger("group-select")).to_have_attribute("aria-label", "管理的群: 测试群")
        self.page.locator("#tab-settings").click()
        expect(self.trigger("setting-rule_selection_strategy")).to_have_attribute("aria-label", "默认多规则命中策略: 按规则优先级")
        self.trigger("setting-decision_backend").click()
        self.page.get_by_role("option", name="Jev", exact=True).click()
        expect(self.page.locator("#jev-settings")).to_have_attribute("open", "")
        expect(self.trigger("setting-decision_backend")).to_have_attribute("aria-label", "默认任务模型类型: Jev")
        self.page.locator("#setting-llm_provider_id").focus()
        expect(self.page.get_by_role("option", name="test-model", exact=True)).to_be_visible()
        self.page.keyboard.press("Escape")
        self.page.locator("#tab-rules").click()
        self.page.locator("#new-rule").click()
        self.page.locator("#add-action").click()
        trigger = self.page.locator(".action-type-control .custom-select-trigger")
        expect(trigger).to_have_attribute("aria-label", "动作类型: 请选择动作")
        trigger.click()
        self.page.get_by_role("option", name="禁言", exact=True).click()
        expect(trigger).to_have_attribute("aria-label", "动作类型: 禁言")
        expect(self.page.get_by_role("combobox", name="禁言时长模式: 严格映射")).to_be_visible()
        self.page.get_by_role("button", name="添加级别").click()
        expect(self.page.locator(".severity-row .custom-select-trigger")).to_have_attribute("aria-label", "单位: 秒")

    def test_management_page_fits_phone_and_tablet(self):
        from playwright.sync_api import expect

        for width in (320, 390, 768, 1440):
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": 800})
                self.page.goto(self.url + "management", wait_until="load")
                self.page.locator("#tab-settings").click()
                control = self.trigger("setting-rule_selection_strategy")
                expect(control).to_be_visible()
                control.click()
                expect(self.page.get_by_role("option", name="按规则优先级", exact=True)).to_be_visible()
                dimensions = self.page.evaluate("""() => {
                  const rect = document.querySelector('#custom-select-menu').getBoundingClientRect();
                  return {width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
                    left: rect.left, right: rect.right};
                }""")
                self.assertLessEqual(dimensions["scrollWidth"], dimensions["width"])
                self.assertGreaterEqual(dimensions["left"], 0)
                self.assertLessEqual(dimensions["right"], dimensions["width"])
                self.page.keyboard.press("Escape")


if __name__ == "__main__":
    unittest.main()
