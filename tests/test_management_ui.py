import shutil
import subprocess
import unittest
import importlib.util
import re
from pathlib import Path


class ManagementUiTests(unittest.TestCase):
    def test_group_names_and_monitoring_badge_states(self):
        if not shutil.which("node"):
            self.skipTest("node is unavailable")
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which("node"), str(root / "tests" / "management_status_probe.mjs"),
                                 str(root / "views" / "management" / "app.js")],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_feedback_timers_and_close_are_independent(self):
        if not shutil.which("node"):
            self.skipTest("node is unavailable")
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which("node"), str(root / "tests" / "management_feedback_probe.mjs"),
                                 str(root / "views" / "management" / "app.js")],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_operation_feedback_is_accessible_and_respects_reduced_motion(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "views" / "management" / "index.html").read_text(encoding="utf-8")
        css = (root / "views" / "management" / "style.css").read_text(encoding="utf-8")
        script = (root / "views" / "management" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="feedback-region" class="feedback-region" aria-live="polite" aria-relevant="additions"', html)
        self.assertIn('close.setAttribute("aria-label", "关闭提示")', script)
        self.assertRegex(css, r'\.feedback-region\s*\{[^}]*top:\s*8px;\s*right:\s*8px')
        self.assertIn('flex-direction: column', css)
        self.assertRegex(css, r'\.feedback-toast\s*\{[^}]*min-height:\s*56px')
        self.assertRegex(css, r'\.feedback-toast\s*\{[^}]*border:\s*3px solid var\(--feedback-border\)')
        for tone in ("success", "warning", "error"):
            self.assertIn(f'.feedback-toast[data-tone="{tone}"]', css)
        self.assertIn('@keyframes feedback-enter', css)
        self.assertIn('@keyframes feedback-exit', css)
        self.assertIn('.feedback-toast.is-leaving { animation: feedback-exit 220ms', css)
        self.assertIn('.feedback-toast, .feedback-toast.is-leaving { animation: none; }', css)
        self.assertIn('.feedback-toast.has-countdown { border-color: var(--feedback-border); }', css)
        self.assertIn('stroke-dasharray: 100 100', css)
        self.assertIn('countdown?.pause()', script)

    def test_unmodified_astrbot_bridge_detects_silent_close(self):
        if not shutil.which("node"):
            self.skipTest("node is unavailable")
        spec = importlib.util.find_spec("astrbot")
        if spec is None or spec.origin is None:
            self.skipTest("AstrBot bridge is unavailable")
        bridge = Path(spec.origin).parent / "dashboard" / "plugin_page_bridge.js"
        if not bridge.is_file():
            self.skipTest("AstrBot bridge script is unavailable")
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which("node"), str(root / "tests" / "management_sse_probe.mjs"),
                                 str(root / "views" / "management" / "app.js"), str(bridge)],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_records_view_uses_bridge_and_text_rendering(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "views" / "management" / "index.html").read_text(encoding="utf-8")
        script = (root / "views" / "management" / "app.js").read_text(encoding="utf-8")
        for identifier in ("refresh-group", "records-next", "records-status", "records-table", "records-body"):
            self.assertIn(f'id="{identifier}"', html)
        for heading in ("时间", "结果", "规则 ID / 版本", "理由摘要", "模型延迟"):
            self.assertIn(heading, html)
        headers = html.split('<table id="records-table">', 1)[1].split('</thead>', 1)[0]
        for removed in ("执行状态", "动作 / 时长", "消息标识"):
            self.assertNotIn(removed, headers)
        for section in ("消息标识", "判定理由", "处理动作", "判定过程", "查看原始记录"):
            self.assertIn(section, script)
        self.assertIn('{before_id: recordsCursor}', script)
        self.assertIn('>加载更早</button>', html)
        self.assertIn('JSON.stringify(latest, null, 2)', script)
        self.assertIn('tr.addEventListener("click", toggle)', script)
        self.assertIn('id="audit"', html)
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("fetch(", script)
        self.assertNotIn("localStorage", script)
        self.assertNotIn("sessionStorage", script)
        for code in ("model_not_configured", "invalid_configuration", "lock_in_use", "start_failed"):
            self.assertIn(code, script)
        self.assertIn("<summary>排查</summary>", html)
        self.assertIn('id="platform-select"', html)
        self.assertIn('id="group-select"', html)
        css = (root / "views" / "management" / "style.css").read_text(encoding="utf-8")
        self.assertIn(':root[data-theme="dark"]', css)
        self.assertIn(':root[data-theme="light"]', css)
        self.assertIn('prefers-color-scheme: dark', css)
        for tab in ("records", "rules", "groups", "status", "settings"):
            self.assertIn(f'id="tab-{tab}"', html)
            self.assertIn(f'id="panel-{tab}"', html)

    def test_management_layout_preserves_responsive_controls(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "views" / "management" / "index.html").read_text(encoding="utf-8")
        script = (root / "views" / "management" / "app.js").read_text(encoding="utf-8")
        css = (root / "views" / "management" / "style.css").read_text(encoding="utf-8")
        self.assertEqual(html.count("<h1"), 1)
        for identifier in ("page-title", "filter-options", "filter-summary", "rule-group-picker", "stat-latency"):
            self.assertIn(f'id="{identifier}"', html)
        self.assertNotIn('id="cancel-rule"', html)
        self.assertNotIn('取消编辑</button>', html)
        self.assertIn('id="new-rule" class="primary" type="button" aria-controls="rule-form" aria-expanded="false"', html)
        self.assertIn('id="preview" class="primary" type="button">保存</button>', html)
        self.assertNotIn('id="confirm"', html)
        self.assertNotIn('id="preview-result"', html)
        self.assertIn('>所有群</option>', html)
        self.assertIn('.editor-footer > button { width: 100%; min-height: 56px; }', css)
        self.assertIn('.group-settings-save { width: 100%; min-height: 56px; }', css)
        self.assertNotIn("确认保存</button>", html)
        self.assertEqual(html.count('<option value="priority">按规则优先级</option>'), 2)
        self.assertNotIn("管理员优先级", html + script)
        self.assertIn('aria-describedby="rule-priority-help"', html)
        self.assertIn('数值越大越优先', html)
        self.assertIn('最高优先级并列时转人工复核', html)
        self.assertIn('id="action-list" role="list"', html)
        self.assertIn('id="action-order-status"', html)
        self.assertIn('按方向键', html)
        self.assertIn('#action-list .form-panel, #action-list .action-drop-placeholder', css)
        self.assertIn('--action-card-highlight: hsl(var(--action-hue, 180) 55% var(--action-badge-lightness))', css)
        self.assertRegex(css, r'\.form-panel\.is-dragging\s*\{\s*border-color:\s*var\(--action-card-highlight\);\s*\}')
        self.assertRegex(css, r'\.form-panel\.is-dragging\s*>\s*\*\s*\{[^}]*opacity:\s*\.5')
        self.assertRegex(css, r'\.action-drop-placeholder\s*\{[^}]*border:\s*2px dashed var\(--action-card-highlight\)')
        self.assertRegex(css, r'\.action-drop-placeholder\s*\{[^}]*color:\s*var\(--action-card-highlight\)')
        self.assertRegex(css, r'\.action-drop-placeholder\s*\{[^}]*background:\s*var\(--action-card-background\)')
        self.assertIn('cell.setAttribute("data-label"', script)
        self.assertIn('event.key === "ArrowRight"', script)
        self.assertIn("@media (max-width: 900px)", css)
        self.assertIn('content: attr(data-label)', css)
        self.assertIn('<header class="app-header">', html)
        self.assertNotIn('class="sidebar"', html)
        self.assertRegex(css, r"\.workspace\s*\{[^}]*\bwidth:\s*100%")
        self.assertNotIn("grid-template-columns: 208px", css)
        self.assertNotIn('class="scope-bar"', html)
        self.assertLess(html.index('id="panel-groups"'), html.index('id="platform-select"'))
        self.assertLess(html.index('id="panel-groups"'), html.index('id="monitor-group"'))
        self.assertIn('id="version" type="hidden"', html)
        self.assertIn('id="groups-status-body"', html)

    def test_typography_distinguishes_headings_help_and_field_values(self):
        root = Path(__file__).resolve().parents[1]
        css = (root / "views" / "management" / "style.css").read_text(encoding="utf-8")
        for token in ("--weight-heading: 700", "--weight-section: 650", "--weight-label: 600",
                      "--weight-value: 500", "--weight-support: 400"):
            self.assertIn(token, css)
        self.assertRegex(css, r'h1\s*\{[^}]*font-weight:\s*var\(--weight-heading\)')
        self.assertRegex(css, r'label\s*\{[^}]*font-weight:\s*var\(--weight-label\)')
        self.assertRegex(css, r'select\s*\{[^}]*font-size:\s*14px;\s*font-weight:\s*var\(--weight-value\)')
        self.assertRegex(css, r'textarea::placeholder\s*\{[^}]*font-weight:\s*var\(--weight-support\)')
        self.assertIn('.group-row > div:first-child > p {', css)
        self.assertNotIn('.scope-id', css)

    def test_record_and_rule_emphasis_are_distinct(self):
        root = Path(__file__).resolve().parents[1]
        css = (root / "views" / "management" / "style.css").read_text(encoding="utf-8")
        self.assertIn('box-shadow: inset 4px 0 0 var(--text)', css)
        self.assertIn('#records-table tr.record td:first-child { padding-left: 14px;', css)
        self.assertIn('#records-table tr.record td:first-child { padding-left: 0; }', css)
        self.assertIn('#records-table tr.record[aria-expanded="true"] { box-shadow: inset 4px', css)
        self.assertNotIn('.rule-card::before', css)
        current = css.split('.rule-current-config {', 1)[1].split('}', 1)[0]
        self.assertNotIn('border-left:', current)
        self.assertIn('background: var(--surface)', current)
        self.assertIn('.rule-action-number', css)

    def test_record_table_column_sizes_do_not_depend_on_details(self):
        root = Path(__file__).resolve().parents[1]
        css = (root / "views" / "management" / "style.css").read_text(encoding="utf-8")
        self.assertIn('#records-table { table-layout: fixed; }', css)
        for column in (2, 4, 5):
            self.assertRegex(css, rf'#records-table th:nth-child\({column}\)\s*\{{\s*width:\s*\d+px;')
        self.assertIn('scrollbar-gutter: stable', css)
        self.assertIn('#records-table th:last-child, #records-table tr.record td:last-child { padding-right: 14px; }', css)

    def test_settings_save_and_monitor_status_presentation(self):
        root = Path(__file__).resolve().parents[1]
        css = (root / "views" / "management" / "style.css").read_text(encoding="utf-8")
        html = (root / "views" / "management" / "index.html").read_text(encoding="utf-8")
        footer = css.split('.settings-footer {', 1)[1].split('}', 1)[0]
        self.assertNotIn('background:', footer)
        self.assertNotIn('padding:', footer)
        self.assertIn('.settings-footer > button { width: 100%; min-height: 56px; }', css)
        self.assertIn('id="settings-status" class="notice" aria-live="polite"', html)
        self.assertIn('id="live-status" class="badge live-status" data-tone="warning"', html)
        self.assertIn('<th>监控状态</th>', html)
        self.assertIn('<table id="groups-status-table"><colgroup>', html)
        self.assertIn('#groups-status-table col:nth-child(7) { width: 21%; }', css)
        status = css.split('.status-main {', 1)[1].split('}', 1)[0]
        self.assertNotIn('border-left:', status)
        self.assertIn('.status-main > strong::before { content: ""; width: 10px; height: 10px; border-radius: 50%; background: currentColor; }', css)
        for tone in ('success', 'warning'):
            self.assertIn(f'.status-main[data-tone="{tone}"] > strong {{ color: var(--signal-{tone}-line); }}', css)

    def test_live_badge_alignment_and_shared_corner_radius(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "views" / "management" / "index.html").read_text(encoding="utf-8")
        css = (root / "views" / "management" / "style.css").read_text(encoding="utf-8")
        script = (root / "views" / "management" / "app.js").read_text(encoding="utf-8")
        actions = html.split('<div id="records-actions" class="records-actions">', 1)[1].split('</div>', 1)[0]
        self.assertIn('id="live-status" class="badge live-status"', actions)
        self.assertIn('id="refresh-group"', actions)
        self.assertIn('.records-actions { display: flex; align-items: center; gap: 10px; }', css)
        self.assertIn('el("records-actions").hidden = name !== "records"', script)
        self.assertIn('--radius: 15px;', css)
        self.assertIn('--radius-sm: var(--radius);', css)
        self.assertIn('--radius-lg: var(--radius);', css)
        self.assertRegex(css, r'\.monitor-badge, \.badge\s*\{[^}]*border-radius:\s*999px;\s*font-size:\s*12px;')
        self.assertNotIn('setting-monitoring_enabled', html + script)
        self.assertNotIn('启用群消息监控', html)
        self.assertIn('setting-action_execution_enabled', html)
        self.assertIn('id="monitor-group"', html)

    def test_button_colors_remain_distinct_from_surfaces(self):
        root = Path(__file__).resolve().parents[1]
        css = (root / "views" / "management" / "style.css").read_text(encoding="utf-8")
        button = css.split('\nbutton {', 1)[1].split('}', 1)[0]
        card = css.split('.rule-card, .status-panel {', 1)[1].split('}', 1)[0]
        self.assertIn('border-radius: var(--radius-lg)', button)
        self.assertIn('border-radius: var(--radius-lg)', card)
        self.assertIn('background: var(--button-bg)', button)
        self.assertIn('border: 1px solid var(--button-border)', button)
        self.assertIn('background: var(--button-hover-bg)', css)
        self.assertIn('background: var(--button-disabled-bg)', css)
        self.assertIn('rx: calc(var(--radius) - 1.5px)', css)
        self.assertIn('.primary { color: var(--primary-text); background: var(--primary)', css)
        self.assertIn('.tabs button {', css)
        self.assertIn('background: transparent', css.split('.feedback-close {', 1)[1].split('}', 1)[0])

        def contrast(a, b):
            def luminance(color):
                channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
                linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
                return sum(v * w for v, w in zip(linear, (0.2126, 0.7152, 0.0722)))
            high, low = sorted((luminance(a), luminance(b)), reverse=True)
            return (high + 0.05) / (low + 0.05)

        for section in (css.split(':root {', 1)[1].split('}', 1)[0],
                        css.split(':root[data-theme="dark"] {', 1)[1].split('}', 1)[0]):
            tokens = dict(re.findall(r'(--[\w-]+):\s*(#[0-9a-fA-F]{6})\s*;', section))
            self.assertNotEqual(tokens['--button-bg'], tokens['--raised'])
            self.assertGreaterEqual(contrast(tokens['--text'], tokens['--button-bg']), 4.5)
            for surface in ('--button-bg', '--raised', '--surface'):
                self.assertGreaterEqual(contrast(tokens['--button-border'], tokens[surface]), 3)

    def test_motion_asset_is_loaded_and_respects_reduced_motion(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "views" / "management" / "index.html").read_text(encoding="utf-8")
        motion = (root / "views" / "management" / "motion.js").read_text(encoding="utf-8")
        css = (root / "views" / "management" / "style.css").read_text(encoding="utf-8")
        self.assertIn('src="./motion.js"', html)
        self.assertIn("prefers-reduced-motion: reduce", css)
        self.assertIn("prefers-reduced-motion", (root / "views" / "management" / "app.js").read_text(encoding="utf-8"))
        self.assertNotIn('stat-', motion)
        self.assertNotIn("textContent =", motion)
        self.assertIn('.tabs button[aria-selected="true"]::after', css)
        self.assertIn('.tabs.has-glide button[aria-selected="true"]::after', css)
        script = (root / "views" / "management" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn('behavior: "smooth"', script)
        self.assertEqual(script.count('behavior: prefersReducedMotion() ? "auto" : "smooth"'), 4)

    def test_rule_and_group_hover_share_a_strong_animated_outline(self):
        root = Path(__file__).resolve().parents[1]
        css = (root / "views" / "management" / "style.css").read_text(encoding="utf-8")
        common = css.split('.rule-card, .group-row {', 1)[1].split('}', 1)[0]
        self.assertIn('transition: border-color var(--dur-base) var(--ease-out), box-shadow var(--dur-base) var(--ease-out)', common)
        self.assertIn('@media (hover: hover)', css)
        hover = css.split('.rule-card:hover, .group-row:hover {', 1)[1].split('}', 1)[0]
        self.assertIn('border-color: var(--text)', hover)
        self.assertIn('box-shadow: 0 0 0 2px var(--text)', hover)
        self.assertIn('prefers-reduced-motion: reduce', css)

    def test_neutral_theme_anchors_and_no_action_switch(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "views" / "management" / "index.html").read_text(encoding="utf-8")
        css = (root / "views" / "management" / "style.css").read_text(encoding="utf-8")
        self.assertIn('id="setting-llm_no_action_reason_enabled" type="checkbox"', html)
        self.assertIn("未违反规则时记录理由", html)
        self.assertIn("判定消息没有违反任何规则时", html)
        self.assertIn("修改后对之后开始的判定生效", html)
        self.assertIn("不影响违规理由和历史记录", html)
        self.assertNotIn("无动作", html)
        self.assertIn("--ground: #ffffff", css)
        self.assertIn("--ground: #1a1a1a", css)
        self.assertIn("--raised: #242424", css)
        self.assertNotIn("--green", css)
        self.assertIn('.monitor-badge[data-tone="success"]', css)
        self.assertIn('.monitor-badge[data-tone="warning"]', css)
        self.assertIn('--signal-success-bg: #b8e0c0', css)
        self.assertIn('--signal-success-ink: #14532d', css)
        self.assertIn('--signal-warning-bg: #f3d18a', css)
        self.assertIn('--signal-warning-ink: #663f00', css)
        for selector, tone in (('.monitor-badge[data-tone="success"]', 'success'),
                               ('.monitor-badge[data-tone="warning"]', 'warning'),
                               ('.badge.record-executed', 'success'), ('.badge.record-planned', 'warning')):
            rule = css.split(f'{selector} {{', 1)[1].split('}', 1)[0]
            self.assertIn(f'background: var(--signal-{tone}-bg)', rule)
            self.assertIn(f'color: var(--signal-{tone}-ink)', rule)
        self.assertIn('--amber: var(--signal-warning-ink)', css)
        self.assertIn('--amber-bg: var(--signal-warning-bg)', css)
        reason_rule = css.split('.record-reason-fallback {', 1)[1].split('}', 1)[0]
        self.assertIn('color: var(--muted)', reason_rule)
        self.assertIn('font-style: italic', reason_rule)
        self.assertNotIn('font-family', reason_rule)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_record_status_rules_filters_and_sort(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which("node"), str(root / "tests" / "management_redesign_probe.mjs"),
                                 str(root / "views" / "management" / "app.js")],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_group_selection_loads_restores_and_refreshes(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which("node"), str(root / "tests" / "management_selection_probe.mjs"),
                                 str(root / "views" / "management" / "app.js")],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_rule_history_uses_scoped_request_and_text_rendering(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "views" / "management" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="history"', html)
        result = subprocess.run([shutil.which("node"), str(root / "tests" / "management_ui_probe.mjs"),
                                 str(root / "views" / "management" / "app.js")],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
