import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";

process.env.TZ = "Asia/Shanghai";
function node() {
  let text = "";
  const classes = new Set();
  return {value: "", checked: false, disabled: false, children: [], handlers: {}, attributes: {},
    style: {setProperty(name, value) { this[name] = value; }},
    classList: {add(value) { classes.add(value); }, remove(value) { classes.delete(value); }, contains(value) { return classes.has(value); }},
    get parentNode() { return this.parent || null; },
    set textContent(value) { text = value; for (const child of this.children) child.parent = null; this.children = []; }, get textContent() { return text; },
    appendChild(child) { this.insertBefore(child, null); },
    insertBefore(child, anchor) {
      if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
      child.parent = this;
      const index = anchor ? this.children.indexOf(anchor) : this.children.length;
      assert.notEqual(index, -1);
      this.children.splice(index, 0, child);
    },
    remove() { if (this.parent) { this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; } },
    querySelector(selector) { return selector === ".rule-edit" ? this.children.find((child) => child.className === "rule-edit") : null; },
    setAttribute(name, value) { this.attributes[name] = value; }, scrollIntoView() { this.scrolled = true; },
    addEventListener(type, handler) { (this.handlers[type] ||= []).push(handler); },
    async trigger(type) { await Promise.all((this.handlers[type] || []).map((handler) => handler())); },
    set innerHTML(value) { throw new Error("untrusted HTML must not be rendered"); },
  };
}
const elements = {};
const latestFeedback = () => elements["feedback-region"].children[0];
globalThis.document = {hidden: false, addEventListener() {}, getElementById(id) { return elements[id] ||= node(); }, createElement: node};
for (const id of ["panel-rules", "current-rules", "rule-form", "group-detail-home", "group-detail-panel"]) document.getElementById(id);
elements["panel-rules"].appendChild(elements["current-rules"]);
elements["panel-rules"].appendChild(elements["rule-form"]);
elements["group-detail-home"].appendChild(elements["group-detail-panel"]);
const requests = [];
const saved = {platform_id: "p1", group_id: "long-group-12345678", active_tab: "records"};
let failSave = false;
let failSummary = false;
let heldSave = null;
let historyMode = "all";
let createdRule = null;
let failRuleConfirm = false;
let liveCallbacks;
let discoveryFailed = false;
let discovery = {groups: [], history_available: true, truncated: false};
const savedRules = new Map();
const settings = {quick_start_completed: true, action_execution_enabled: true, monitoring_groups: ["p1/long-group-12345678"],
  monitoring_mode: "allowlist", default_execution_mode: "execute", default_action_mode: "auto", message_retention_days: 30, llm_reason_language: "中文", llm_no_action_reason_enabled: true, group_overrides: {},
  decision_backend: "llm", llm_provider_id: "default-model", rule_selection_strategy: "priority",
  protected_target_policy: "exempt", protected_target_template: "global {rule_id}: {sender_id} {action}", protected_target_quote: true,
  max_duration_seconds: 2592000, model_concurrency: 2, model_timeout_seconds: 30, global_rate_per_minute: 60,
  group_rate_per_minute: 30, model_failure_threshold: 5, model_cooldown_seconds: 60, queue_capacity_per_group: 100,
  ingress_capacity: 256, notification_target: "", notification_interval_seconds: 0};
const groups = [
  {platform_id: "p1", group_id: "long-group-12345678", rule_count: 2, alias: "技术交流群", platform_name: "官方群名"},
  {platform_id: "p1", group_id: "other-group-87654321", rule_count: 1},
  {platform_id: "p2", group_id: "separate-group", rule_count: 0},
];
globalThis.window = {addEventListener() {}, AstrBotPluginView: {
  async subscribeSSE(endpoint, callbacks) { liveCallbacks = callbacks; return "test"; }, async unsubscribeSSE() {},
  async ready() {},
  async apiGet(endpoint, params) {
    requests.push({endpoint, params});
    if (endpoint === "status") return {monitoring: "dry_run", action_execution_enabled: true, action_execution_available: true};
    if (endpoint === "settings") return structuredClone(settings);
    if (endpoint === "providers") return [];
    if (endpoint === "ui-state") return saved;
    if (endpoint === "platforms") return [{id: "p1", type: "qq_official"}, {id: "p2", type: "qq_official"}];
    if (endpoint === "groups") return groups;
    if (endpoint === "available-groups") {
      if (discoveryFailed) throw new Error("unavailable");
      return structuredClone(discovery);
    }
    if (endpoint === "summary") {
      if (failSummary && params.group_id === "separate-group") throw new Error("unavailable");
      return {total_messages: 1, status_counts: {proposed: 1}, recent_24h: {messages: 1, proposed: 1, no_action: 0},
        latency_p95_ms: 5, truncated: params.group_id === "other-group-87654321"};
    }
    if (endpoint === "rules") return [savedRules.get(JSON.stringify([params.platform_id, params.group_id, "r"])) || {rule_id: "r", version: 3, enabled: true, text: "<script>",
      priority: 8, actions: [{type: "mute", mode: "auto", duration_mode: "strict", severity_seconds: JSON.parse('{"low":60,"custom level":90,"__proto__":120}')},
        {type: "reply", mode: "confirm", template: "<img src=x>"}, {type: "recall", mode: "auto"}]},
        ...(createdRule && createdRule.platform_id === params.platform_id && createdRule.group_id === params.group_id ? [createdRule] : [])];
    if (endpoint === "rules/history") {
      const history = [{version: 1, enabled: false, text: "<img src=x>", modified_by: "admin", created_at: "2026-01-01T00:00:00Z", actions: []},
        {version: 2, enabled: true, text: "old rule", modified_by: "admin", created_at: "2026-01-02T00:00:00Z", actions: [{type: "recall", mode: "auto"}]},
        {version: 3, enabled: true, text: "current rule", modified_by: "admin", created_at: "2026-02-01T00:00:00Z", actions: [{type: "mute", mode: "auto", duration_mode: "strict", severity_seconds: {low: 60}}]}];
      return historyMode === "current" ? history.slice(-1) : historyMode === "newer" ? [...history, {...history[2], version: 4}] : history;
    }
    if (endpoint === "message-content") return {content: `content from ${params.platform_id}/${params.group_id}`};
    if (endpoint === "messages" && params.all_groups === "true") return [
      {id: 5, platform_id: "p1", group_id: "long-group-12345678", message_id: "same", status: "no_action", detail: '{"model_latency_ms":100}', created_at: "2026-11-01T12:35:00Z"},
      {id: 4, platform_id: "p2", group_id: "separate-group", message_id: "same", status: "no_action", detail: '{"model_latency_ms":300}', created_at: "2026-11-01T12:34:00Z"},
    ];
    if (endpoint === "messages") return [{id: 1, status: "proposed", message_id: "ROBOT1.0_12345678901234567890abcdef",
      created_at: "2026-11-01T12:34:56.000Z", detail: "{}"}];
    throw new Error("unexpected endpoint");
  },
  async apiPost(endpoint, body) {
    requests.push({endpoint, body});
    if (failSave) throw new Error("private error");
    if (heldSave?.endpoint === endpoint) await heldSave.promise;
    if (endpoint === "rules/preview") return {fields: body};
    if (endpoint === "rules/confirm") {
      if (failRuleConfirm) throw new Error("version conflict");
      const version = body.fields.expected_version + 1;
      if (body.fields.rule_id === "created-rule") createdRule = {...body.fields, version};
      if (body.fields.rule_id === "r") savedRules.set(JSON.stringify([body.fields.platform_id, body.fields.group_id, "r"]), {...body.fields, version});
      return {version};
    }
    if (endpoint === "group-name") return {...body, alias: "", platform_name: "平台群名"};
    if (endpoint === "available-group-name") {
      if (body.group_id === "no-name") throw new Error("unavailable");
      return {...body, name: "自动获取的群名"};
    }
    if (endpoint === "group-alias") return {...body, alias: body.name, platform_name: "平台群名"};
    if (endpoint === "group-settings") {
      const key = `${body.platform_id}/${body.group_id}`;
      if (Object.keys(body.overrides).length) settings.group_overrides[key] = body.overrides;
      else delete settings.group_overrides[key];
      return {settings: structuredClone(settings)};
    }
    if (endpoint === "group-monitoring") {
      const key = `${body.platform_id}/${body.group_id}`;
      settings.monitoring_groups = settings.monitoring_groups.filter((item) => item !== key);
      if (body.enabled) settings.monitoring_groups.push(key);
      return {settings: structuredClone(settings)};
    }
    if (endpoint === "settings") {
      for (const [key, value] of Object.entries(body)) settings[key] = value;
      return {settings: structuredClone(settings)};
    }
    return body;
  },
}};
const source = await readFile(process.argv[2], "utf8");
const html = await readFile(process.argv[2].replace(/app\.js$/, "index.html"), "utf8");
await import("data:text/javascript;base64," + Buffer.from(source + "\nglobalThis.__disclosureTest = {setPanelVisibility, setDetailsOpen, updateVisiblePanel};\n").toString("base64"));
assert.deepEqual(requests.map((request) => request.endpoint).filter((endpoint) => !["settings", "providers"].includes(endpoint)), ["ui-state", "status", "platforms", "groups", "rules", "messages", "summary"]);
assert.equal(elements["group-select"].value, JSON.stringify(["p1", "long-group-12345678"]));
assert.equal(elements["group-detail-panel"].parentNode, elements["group-detail-home"]);
assert.deepEqual(elements["groups-list"].children.map((row) => row.children[3].attributes["aria-expanded"]), ["false", "false", "false"]);
assert.ok(elements["groups-list"].children.every((row) => row.className === "group-row"));
assert.ok(!html.includes('id="scope-id"'));
assert.equal(elements["group-select"].children.length, 4);
assert.equal(elements["group-select"].children[1].textContent, "技术交流群");
assert.equal(elements["group-select"].children[2].textContent, "未命名群 (1)");
assert.equal(elements["group-select"].children[3].textContent, "未命名群 (2)");
assert.equal(elements["platforms-list"].textContent, "QQ 官方机器人: 2 个实例");
assert.equal(elements["groups-list"].children[0].children[0].children[1].textContent, "2 条规则");
assert.equal(elements["group-alias"].value, "技术交流群");
assert.equal(elements["group-policy-inherit"].textContent, "全局(只记录)");
assert.equal(elements["group-quote-inherit"].textContent, "全局(引用)");
assert.equal(elements["group-model-inherit"].textContent, "全局(default-model)");
assert.equal(elements["group-strategy-inherit"].textContent, "全局(按规则优先级)");
assert.equal(elements["group-reason-language-inherit"].textContent, "全局(中文)");
assert.equal(elements["group-reason-language-mode"].value, "");
assert.equal(elements["group-reason-language-label"].hidden, true);
assert.equal(elements["group-execution-inherit"].textContent, "全局(允许执行)");
assert.match(elements["group-defaults"].textContent, /按规则优先级/);
assert.equal(elements["group-template-label"].hidden, true);
assert.equal(elements["group-template-preview"].textContent, "global rule-1: member-1 mute");
assert.match(elements["group-protected-state"].textContent, /选择只记录时不会发送群提示/);
assert.equal(elements["group-select"].children[1].title, "技术交流群");
elements["group-alias"].value = "新的备注";
await elements["get-group-name"].trigger("click");
assert.equal(elements["group-alias"].value, "新的备注");
assert.equal(elements["group-select"].children[1].textContent, "技术交流群");
await elements["save-group-alias"].trigger("click");
assert.equal(elements["group-select"].children[1].textContent, "新的备注");
const ruleCard = elements["current-rules"].children[0];
assert.deepEqual(ruleCard.children.slice(1, 3).map((button) => button.textContent), ["历史版本", "编辑"]);
assert.match(ruleCard.children[0].children[1].textContent, /<script>/);
assert.equal(ruleCard.children[0].children[0].children[2].textContent, "当前版本: 第 3 版");
const currentConfig = ruleCard.children[0].children[2];
assert.equal(currentConfig.children[0].children[0].textContent, "当前配置");
assert.equal(currentConfig.children[0].children[1].textContent, "优先级: 8");
assert.deepEqual(currentConfig.children[1].children.map((item) => [item.children[0].textContent, item.children[1].textContent]), [
  ["1", "禁言 / 自动 (low: 1分钟, custom level: 1分钟 30秒, __proto__: 2分钟)"],
  ["2", "发言 / 人工确认 (<img src=x>)"], ["3", "撤回 / 自动"]]);
await ruleCard.children[1].trigger("click");
assert.deepEqual(requests.at(-1), {endpoint: "rules/history", params: {platform_id: "p1", group_id: "long-group-12345678", rule_id: "r"}});
const history = ruleCard.children[3];
assert.equal(ruleCard.children[1].attributes["aria-expanded"], "true");
assert.match(history.children[0].textContent, /历史版本仅供查看/);
assert.equal(history.children[1].children[0].textContent, "第 2 版");
assert.match(history.children[1].children[1].textContent, /保存时: 启用/);
assert.equal(history.children[2].children[0].textContent, "第 1 版");
assert.match(history.children[2].children[1].textContent, /保存时: 停用/);
assert.equal(history.children[2].children[2].textContent, "<img src=x>");
await ruleCard.children[1].trigger("click");
assert.equal(ruleCard.children[1].attributes["aria-expanded"], "false");
historyMode = "current";
await ruleCard.children[1].trigger("click");
assert.equal(history.textContent, "暂无历史版本.");
await ruleCard.children[1].trigger("click");
historyMode = "newer";
await ruleCard.children[1].trigger("click");
assert.match(history.textContent, /规则已更新/);
historyMode = "all";
await ruleCard.children[2].trigger("click");
assert.equal(elements["rule-form"].parentNode, ruleCard);
assert.equal(ruleCard.children[2].attributes["aria-expanded"], "true");
assert.equal(ruleCard.children[2].textContent, "收起");
assert.equal(ruleCard.children[2].title, "收起并放弃未保存的修改");
assert.equal(elements["new-rule"].textContent, "新建规则");
elements.text.value = "unsaved rule draft";
await elements.text.trigger("input");
elements.priority.value = "19";
await elements.priority.trigger("input");
const beforeRuleToggle = requests.length;
await ruleCard.children[2].trigger("click");
assert.equal(ruleCard.children[2].textContent, "编辑");
assert.equal(ruleCard.children[2].attributes["aria-expanded"], "false");
assert.equal(elements["rule-form"].hidden, true);
await ruleCard.children[2].trigger("click");
assert.equal(ruleCard.children[2].textContent, "收起");
assert.equal(ruleCard.children[2].attributes["aria-expanded"], "true");
assert.equal(elements["rule-form"].hidden, false);
assert.equal(elements.text.value, "<script>");
assert.equal(elements.priority.value, "8");
assert.equal(requests.length, beforeRuleToggle);
assert.equal(elements.rule.value, "r");
assert.equal(elements.version.value, "3");
assert.equal(elements.priority.value, "8");
assert.equal(elements.text.value, "<script>");
assert.equal(elements["action-list"].children[0].children[1].value, "mute");
const severityRow = elements["action-list"].children[0].children[5].children[1].children[0];
assert.equal(severityRow.children[0].children[0].value, "low");
assert.equal(severityRow.children[1].children[0].value, "1");
assert.equal(severityRow.children[2].children[0].value, "60");
let releaseRulePreview;
heldSave = {endpoint: "rules/preview", promise: new Promise((resolve) => { releaseRulePreview = resolve; })};
const beforeCancelledPreview = requests.filter((request) => request.endpoint === "rules/confirm").length;
const cancelledPreview = elements.preview.trigger("click");
assert.deepEqual(JSON.parse(JSON.stringify(requests.at(-1).body.actions[0].severity_seconds)), JSON.parse('{"low":60,"custom level":90,"__proto__":120}'));
assert.equal(ruleCard.children[2].disabled, true);
elements.text.value = "changed while checking";
await elements.text.trigger("input");
releaseRulePreview();
await cancelledPreview;
heldSave = null;
assert.equal(requests.filter((request) => request.endpoint === "rules/confirm").length, beforeCancelledPreview);
assert.equal(ruleCard.children[2].disabled, false);
await ruleCard.children[2].trigger("click");
await ruleCard.children[2].trigger("click");
const secondLevel = elements["action-list"].children[0].children[5].children[1].children[1].children[0].children[0];
secondLevel.value = "low";
await secondLevel.trigger("input");
assert.equal(elements.preview.disabled, false);
const beforeDuplicate = requests.length;
await elements.preview.trigger("click");
assert.equal(requests.length, beforeDuplicate);
assert.match(latestFeedback().children[0].textContent, /级别名称不能重复/);
assert.equal(elements.preview.disabled, false);
secondLevel.value = "custom level";
await secondLevel.trigger("input");
const severityEditor = elements["action-list"].children[0].children[5];
await severityEditor.children[2].trigger("click");
assert.equal(elements.preview.disabled, false);
const newLevel = severityEditor.children[1].children[3];
newLevel.children[0].children[0].value = "medium";
newLevel.children[1].children[0].value = "5";
newLevel.children[2].children[0].value = "60";
const confirmations = requests.filter((request) => request.endpoint === "rules/confirm").length;
await elements.preview.trigger("click");
assert.equal(requests.findLast((request) => request.endpoint === "rules/preview").body.actions[0].severity_seconds.medium, 300);
assert.equal(requests.filter((request) => request.endpoint === "rules/confirm").length, confirmations + 1);
assert.match(latestFeedback().children[0].textContent, /规则已保存/);
assert.equal(latestFeedback().attributes["data-tone"], "success");
assert.equal(elements["save-status"].textContent, "");
assert.equal(elements.preview.disabled, false);
assert.equal(elements.enabled.checked, true);
assert.equal(ruleCard.scrolled, true);
assert.equal(elements["action-list"].children[0].children[9].textContent, "动作类型");
assert.equal(elements["action-list"].children[0].children[10].textContent, "执行方式");
const cells = elements["records-body"].children[0].children;
assert.equal(cells.length, 5);
assert.equal(elements["records-body"].children[0].attributes["aria-label"], "查看消息 ROBOT1.0_12345678901234567890abcdef 的详情");
assert.equal(elements["records-body"].children[1].children[0].children[0].children[0].children[0].children[1].textContent, "ROBOT1.0_12345678901234567890abcdef");
const time = new Date("2026-11-01T12:34:56.000Z");
const pad = (value) => String(value).padStart(2, "0");
assert.equal(cells[0].textContent, `${pad(time.getMonth()+1)}-${pad(time.getDate())} ${pad(time.getHours())}:${pad(time.getMinutes())}`);
assert.equal(cells[0].title, "2026-11-01 20:34:56");
assert.equal(cells[0].textContent, "11-01 20:34");
elements["group-select"].value = JSON.stringify(["p1", "other-group-87654321"]);
await elements["group-select"].trigger("change");
assert.equal(elements["rule-form"].parentNode, elements["panel-rules"]);
assert.equal(ruleCard.children[2].textContent, "编辑");
assert.equal(elements["rule-form"].hidden, false);
assert.equal(elements["new-rule"].textContent, "取消");
assert.equal(elements["new-rule"].attributes["aria-expanded"], "true");
assert.match(elements["save-status"].textContent, /返回原群/);
assert.doesNotMatch(elements["group-detail-name"].textContent, /other-group-87654321/);
const beforeWrongScope = requests.filter((request) => request.endpoint === "rules/preview").length;
await elements.preview.trigger("click");
assert.equal(requests.filter((request) => request.endpoint === "rules/preview").length, beforeWrongScope);
assert.match(latestFeedback().children[0].textContent, /返回原群/);
assert.deepEqual(requests.filter((request) => request.endpoint === "rules" || request.endpoint === "messages").slice(-2).map((request) => [request.endpoint, request.params.group_id]),
  [["rules", "other-group-87654321"], ["messages", "other-group-87654321"]]);
assert.deepEqual(requests.filter((request) => request.endpoint === "ui-state" && Object.hasOwn(request.body || {}, "platform_id")).at(-1).body,
  {platform_id: "p1", group_id: "other-group-87654321"});
assert.equal(elements["group-select"].children.length, 4);
assert.equal(elements["group-select"].children[3].value, JSON.stringify(["p2", "separate-group"]));
elements["group-select"].value = JSON.stringify(["p2", "separate-group"]);
await elements["group-select"].trigger("change");
assert.deepEqual(requests.filter((request) => request.endpoint === "rules" || request.endpoint === "messages").slice(-2).map((request) => [request.endpoint, request.params.platform_id, request.params.group_id]),
  [["rules", "p2", "separate-group"], ["messages", "p2", "separate-group"]]);
assert.deepEqual(requests.filter((request) => request.endpoint === "ui-state" && Object.hasOwn(request.body || {}, "platform_id")).at(-1).body,
  {platform_id: "p2", group_id: "separate-group"});
failSave = true;
elements["group-select"].value = JSON.stringify(["p2", "separate-group"]);
await elements["group-select"].trigger("change");
assert.equal(latestFeedback().children[0].textContent, "无法保存上次选择.");
failSave = false;
assert.equal(elements["group-model-backend"].value, "");
elements["group-model-backend"].value = "llm";
await elements["group-model-backend"].trigger("change");
elements["group-model"].value = "model-override";
elements["group-strategy"].value = "priority";
elements["group-execution"].value = "record";
await elements["group-settings-save"].trigger("click");
assert.deepEqual(requests.findLast((request) => request.endpoint === "group-settings").body, {
  platform_id: "p2", group_id: "separate-group", overrides: {
    model: {backend: "llm", model: "model-override"}, strategy: "priority", execution_mode: "record"}});
elements["group-select"].value = JSON.stringify(["p1", "long-group-12345678"]);
await elements["group-select"].trigger("change");
assert.match(elements["save-status"].textContent, /草稿仍未保存/);
assert.equal(elements["rule-form"].parentNode, elements["current-rules"].children[0]);
assert.equal(elements["current-rules"].children[0].children[2].textContent, "收起");
assert.equal(elements["new-rule"].textContent, "新建规则");
assert.equal(elements["group-model-backend"].value, "");
assert.equal(elements["group-strategy"].value, "");
assert.equal(elements["monitor-group"].checked, true);
elements["monitor-group"].checked = false;
await elements["monitor-group"].trigger("change");
assert.deepEqual(settings.monitoring_groups, []);
failSummary = true;
await elements["tab-status"].trigger("click");
await new Promise((resolve) => setImmediate(resolve));
assert.equal(elements["status-groups-total"].textContent, "1");
assert.equal(elements["groups-status-body"].children.length, 1);
elements["group-select"].value = "";
await elements["group-select"].trigger("change");
assert.equal(elements["status-groups-total"].textContent, "3");
assert.equal(elements["status-groups-enabled"].textContent, "0");
assert.equal(elements["groups-status-body"].children.length, 3);
const disabledStatus = elements["groups-status-body"].children[0].children[1];
assert.equal(disabledStatus.attributes["data-label"], "监控状态");
assert.equal(disabledStatus.children[0].className, "badge");
assert.equal(disabledStatus.children[0].textContent, "已停用");
assert.equal(disabledStatus.children[0].attributes["data-tone"], "muted");
assert.equal(elements["groups-status-body"].children[0].children[0].textContent, "新的备注");
assert.equal(elements["groups-status-body"].children[0].children[0].children.length, 0);
assert.equal(elements["groups-status-body"].children[1].children[2].textContent, "1+");
assert.equal(elements["groups-status-body"].children[2].children[2].textContent, "-");
assert.match(elements["all-groups-status-message"].textContent, /部分群统计不可用/);
elements["group-select"].value = JSON.stringify(["p1", "long-group-12345678"]);
await elements["group-select"].trigger("change");

elements["group-execution"].value = "execute";
await elements["group-execution"].trigger("change");
assert.match(elements["group-execution-state"].textContent, /本群监控未开启/);
elements["group-protected-policy"].value = "group_message";
await elements["group-protected-policy"].trigger("change");
assert.match(elements["group-protected-state"].textContent, /满足上方条件时/);
assert.match(html, /id="group-notice-controls" class="notice-settings">/);
elements["group-protected-quote"].value = "false";
await elements["group-protected-quote"].trigger("change");
elements["group-template-override"].checked = true;
await elements["group-template-override"].trigger("change");
assert.equal(elements["group-template-label"].hidden, false);
assert.equal(elements["group-protected-template"].value, settings.protected_target_template);
const safeTemplate = "<img src=x> {rule_id} {{safe}} {action:}";
elements["group-protected-template"].value = safeTemplate;
await elements["group-protected-template"].trigger("input");
assert.equal(elements["group-template-preview"].textContent, "<img src=x> rule-1 {safe} mute");
elements["group-template-override"].checked = false;
await elements["group-template-override"].trigger("change");
assert.equal(elements["group-template-label"].hidden, true);
assert.equal(elements["group-template-preview"].textContent, "global rule-1: member-1 mute");
elements["group-template-override"].checked = true;
await elements["group-template-override"].trigger("change");
assert.equal(elements["group-protected-template"].value, safeTemplate);
for (const template of ["bad {action", "bad }", "{action!s}", "{action:>10}", "{unknown}", "{{{action"]) {
  elements["group-protected-template"].value = template;
  await elements["group-protected-template"].trigger("input");
  assert.match(elements["group-template-preview"].textContent, /无法生成示例/);
  const before = requests.filter((request) => request.endpoint === "group-settings").length;
  await elements["group-settings-save"].trigger("click");
  assert.equal(requests.filter((request) => request.endpoint === "group-settings").length, before);
  assert.match(latestFeedback().children[0].textContent, /花括号或变量名称无效/);
}
elements["group-protected-template"].value = " ";
await elements["group-protected-template"].trigger("input");
await elements["group-settings-save"].trigger("click");
assert.match(latestFeedback().children[0].textContent, /请填写本群提示内容/);
elements["group-protected-template"].value = safeTemplate;
await elements["group-protected-template"].trigger("input");
await elements["group-settings-save"].trigger("click");
assert.deepEqual(settings.group_overrides["p1/long-group-12345678"], {execution_mode: "execute",
  protected_policy: "group_message", protected_quote: false, protected_template: safeTemplate});

elements["group-protected-template"].value = "unsaved {sender_id}";
await elements["group-protected-template"].trigger("input");
elements["group-select"].value = JSON.stringify(["p2", "separate-group"]);
await elements["group-select"].trigger("change");
assert.equal(elements["group-template-override"].checked, false);
elements["group-select"].value = JSON.stringify(["p1", "long-group-12345678"]);
await elements["group-select"].trigger("change");
assert.equal(elements["group-protected-template"].value, "unsaved {sender_id}");
assert.equal(elements["group-protected-quote"].value, "false");
assert.match(elements["group-settings-status"].textContent, /尚未保存/);

let release;
heldSave = {endpoint: "settings", promise: new Promise((resolve) => { release = resolve; })};
elements["setting-protected_target_template"].value = "new global {rule_id}";
await elements["setting-protected_target_template"].trigger("input");
const savingGlobal = elements["settings-save"].trigger("click");
await new Promise((resolve) => setImmediate(resolve));
elements["setting-protected_target_template"].value = "later global {sender_id}";
await elements["setting-protected_target_template"].trigger("input");
elements["group-protected-template"].value = "draft during global save {action}";
await elements["group-protected-template"].trigger("input");
release();
await savingGlobal;
heldSave = null;
assert.equal(settings.protected_target_template, "new global {rule_id}");
assert.equal(elements["setting-protected_target_template"].value, "later global {sender_id}");
assert.equal(elements["setting-template-preview"].textContent, "later global member-1");
assert.match(latestFeedback().children[0].textContent, /新修改尚未保存/);
assert.equal(latestFeedback().attributes["data-tone"], "warning");
assert.equal(elements["group-protected-template"].value, "draft during global save {action}");
assert.equal(elements["group-protected-quote"].value, "false");

heldSave = {endpoint: "group-settings", promise: new Promise((resolve) => { release = resolve; })};
const savingGroup = elements["group-settings-save"].trigger("click");
await new Promise((resolve) => setImmediate(resolve));
elements["group-select"].value = JSON.stringify(["p2", "separate-group"]);
await elements["group-select"].trigger("change");
elements["group-template-override"].checked = true;
await elements["group-template-override"].trigger("change");
assert.equal(elements["group-protected-template"].value, "new global {rule_id}");
elements["group-protected-template"].value = "other group draft";
await elements["group-protected-template"].trigger("input");
release();
await savingGroup;
heldSave = null;
assert.equal(settings.group_overrides["p1/long-group-12345678"].protected_template, "draft during global save {action}");
assert.equal(elements["group-protected-template"].value, "other group draft");
elements["group-select"].value = JSON.stringify(["p1", "long-group-12345678"]);
await elements["group-select"].trigger("change");
assert.equal(elements["group-protected-template"].value, "draft during global save {action}");
assert.equal(elements["group-protected-quote"].value, "false");
elements["group-select"].value = JSON.stringify(["p2", "separate-group"]);
await elements["group-select"].trigger("change");
assert.equal(elements["group-protected-template"].value, "other group draft");

elements["group-template-override"].checked = false;
await elements["group-template-override"].trigger("change");
elements["group-protected-policy"].value = "group_message";
await elements["group-protected-policy"].trigger("change");
await elements["group-settings-save"].trigger("click");
assert.equal(settings.group_overrides["p2/separate-group"].protected_policy, "group_message");
assert.equal(Object.hasOwn(settings.group_overrides["p2/separate-group"], "protected_template"), false);
elements["setting-protected_target_template"].value = "";
await elements["setting-protected_target_template"].trigger("input");
const previousGlobalPosts = requests.filter((request) => request.endpoint === "settings" && request.body).length;
await elements["settings-save"].trigger("click");
assert.equal(requests.filter((request) => request.endpoint === "settings" && request.body).length, previousGlobalPosts);
assert.match(latestFeedback().children[0].textContent, /有群选择发送提示并使用全局内容/);
elements["group-protected-policy"].value = "exempt";
await elements["group-protected-policy"].trigger("change");
await elements["group-settings-save"].trigger("click");
await elements["settings-save"].trigger("click");
assert.equal(settings.protected_target_template, "");
assert.equal(elements["setting-template-preview"].textContent, "尚未填写提示内容.");
elements["group-protected-policy"].value = "group_message";
await elements["group-protected-policy"].trigger("change");
await elements["group-settings-save"].trigger("click");
assert.match(latestFeedback().children[0].textContent, /全局提示内容为空/);
elements["group-template-override"].checked = true;
await elements["group-template-override"].trigger("change");
assert.match(elements["group-protected-template"].value, /\{rule_id\}/);
await elements["group-settings-save"].trigger("click");
assert.equal(settings.group_overrides["p2/separate-group"].protected_policy, "group_message");

elements["group-protected-template"].value = "queued group {rule_id}";
await elements["group-protected-template"].trigger("input");
heldSave = {endpoint: "group-settings", promise: new Promise((resolve) => { release = resolve; })};
const queuedGroup = elements["group-settings-save"].trigger("click");
await new Promise((resolve) => setImmediate(resolve));
const beforeQueuedGlobal = requests.filter((request) => request.endpoint === "settings" && request.body).length;
const queuedGlobal = elements["settings-save"].trigger("click");
await new Promise((resolve) => setImmediate(resolve));
assert.equal(requests.filter((request) => request.endpoint === "settings" && request.body).length, beforeQueuedGlobal);
release();
await Promise.all([queuedGroup, queuedGlobal]);
heldSave = null;
assert.equal(requests.filter((request) => request.endpoint === "settings" && request.body).length, beforeQueuedGlobal + 1);
assert.equal(settings.group_overrides["p2/separate-group"].protected_template, "queued group {rule_id}");
assert.equal(elements["group-protected-template"].value, "queued group {rule_id}");

for (const template of ["{action!s}", "{rule_id", "{unknown}"]) {
  elements["setting-protected_target_template"].value = template;
  await elements["setting-protected_target_template"].trigger("input");
  const before = requests.filter((request) => request.endpoint === "settings" && request.body).length;
  await elements["settings-save"].trigger("click");
  assert.equal(requests.filter((request) => request.endpoint === "settings" && request.body).length, before);
  assert.match(latestFeedback().children[0].textContent, /变量只支持/);
  assert.equal(latestFeedback().attributes["data-tone"], "error");
}
elements["setting-protected_target_template"].value = "global {sender_id}";
elements["group-execution"].value = "execute";
await elements["group-execution"].trigger("change");
elements["setting-action_execution_enabled"].checked = false;
await elements["settings-save"].trigger("click");
assert.match(elements["group-execution-state"].textContent, /全局真实执行未开启/);
assert.equal(elements["group-execution"].value, "execute");
elements["setting-action_execution_enabled"].checked = true;
await elements["settings-save"].trigger("click");
assert.ok(!Object.hasOwn(requests.findLast((request) => request.endpoint === "settings" && request.body).body, "monitoring_enabled"));
assert.doesNotMatch(elements["group-execution-state"].textContent, /全局监控/);
elements["group-protected-policy"].value = "";
await elements["group-protected-policy"].trigger("change");
elements["group-protected-template"].value = " ";
await elements["group-protected-template"].trigger("input");
await elements["group-settings-save"].trigger("click");
assert.equal(settings.group_overrides["p2/separate-group"].protected_template, " ");
elements["setting-protected_target_policy"].value = "group_message";
const beforeInheritedEmpty = requests.filter((request) => request.endpoint === "settings" && request.body).length;
await elements["settings-save"].trigger("click");
assert.equal(requests.filter((request) => request.endpoint === "settings" && request.body).length, beforeInheritedEmpty);
assert.match(latestFeedback().children[0].textContent, /单独提示内容为空/);

elements["group-select"].value = JSON.stringify(["p1", "long-group-12345678"]);
await elements["group-select"].trigger("change");
elements["group-reason-language-mode"].value = "custom";
await elements["group-reason-language-mode"].trigger("change");
assert.equal(elements["group-reason-language"].value, "中文");
assert.equal(elements["group-reason-language-label"].hidden, false);
elements["group-reason-language"].value = "English";
await elements["group-reason-language"].trigger("input");
elements["group-select"].value = JSON.stringify(["p2", "separate-group"]);
await elements["group-select"].trigger("change");
assert.equal(elements["group-reason-language-mode"].value, "");
elements["group-select"].value = JSON.stringify(["p1", "long-group-12345678"]);
await elements["group-select"].trigger("change");
assert.equal(elements["group-reason-language"].value, "English");
assert.match(elements["group-settings-status"].textContent, /尚未保存/);
await elements["group-settings-save"].trigger("click");
assert.equal(settings.group_overrides["p1/long-group-12345678"].llm_reason_language, "English");
assert.equal(Object.hasOwn(settings.group_overrides["p2/separate-group"], "llm_reason_language"), false);
elements["group-reason-language-mode"].value = "";
await elements["group-reason-language-mode"].trigger("change");
assert.equal(elements["group-reason-language-label"].hidden, true);
await elements["group-settings-save"].trigger("click");
assert.equal(Object.hasOwn(settings.group_overrides["p1/long-group-12345678"], "llm_reason_language"), false);
elements["group-reason-language-mode"].value = "custom";
await elements["group-reason-language-mode"].trigger("change");
elements["group-reason-language"].value = "";
await elements["group-reason-language"].trigger("input");
await elements["group-settings-save"].trigger("click");
assert.equal(settings.group_overrides["p1/long-group-12345678"].llm_reason_language, "");
assert.equal(elements["group-reason-language-mode"].value, "custom");
heldSave = {endpoint: "group-settings", promise: new Promise((resolve) => { release = resolve; })};
elements["group-reason-language"].value = "French";
await elements["group-reason-language"].trigger("input");
const savingLanguage = elements["group-settings-save"].trigger("click");
await new Promise((resolve) => setImmediate(resolve));
elements["group-reason-language"].value = "Japanese";
await elements["group-reason-language"].trigger("input");
release();
await savingLanguage;
heldSave = null;
assert.equal(settings.group_overrides["p1/long-group-12345678"].llm_reason_language, "French");
assert.equal(elements["group-reason-language"].value, "Japanese");
assert.match(elements["group-settings-status"].textContent, /尚未保存/);
elements["setting-protected_target_policy"].value = "exempt";
elements["setting-llm_reason_language"].value = "German";
await elements["settings-save"].trigger("click");
assert.equal(elements["group-reason-language-inherit"].textContent, "全局(German)");
assert.equal(elements["group-reason-language"].value, "Japanese");
assert.equal(elements["group-reason-language-mode"].value, "custom");

groups[0].rule_count = 5;
await elements["tab-groups"].trigger("click");
await new Promise((resolve) => setImmediate(resolve));
assert.equal(elements["groups-list"].children[0].children[0].children[1].textContent, "5 条规则");
const [firstGroup, secondGroup, thirdGroup] = elements["groups-list"].children;
const firstButton = firstGroup.children[3];
const secondButton = secondGroup.children[3];
await firstGroup.trigger("click");
assert.equal(elements["group-detail-panel"].parentNode, elements["group-detail-home"]);
assert.equal(firstButton.attributes["aria-expanded"], "false");
const beforeOpen = requests.length;
await firstButton.trigger("click");
assert.equal(requests.length, beforeOpen);
assert.equal(elements["group-detail-panel"].parentNode, firstGroup);
assert.equal(firstGroup.className, "group-row is-editing");
assert.equal(firstButton.attributes["aria-expanded"], "true");
assert.equal(firstButton.textContent, "收起");
assert.equal(elements["group-reason-language"].value, "Japanese");
await firstButton.trigger("click");
assert.equal(elements["group-detail-panel"].parentNode, elements["group-detail-home"]);
assert.equal(firstButton.attributes["aria-expanded"], "false");
assert.equal(firstButton.textContent, "配置");
await firstButton.trigger("click");
assert.equal(elements["group-reason-language"].value, "Japanese");

elements["group-select"].value = JSON.stringify(["p1", "other-group-87654321"]);
await elements["group-select"].trigger("change");
assert.equal(elements["group-detail-panel"].parentNode, elements["group-detail-home"]);
assert.ok([firstGroup, secondGroup, thirdGroup].every((row) => row.children[3].attributes["aria-expanded"] === "false"));
elements["group-select"].value = JSON.stringify(["p1", "long-group-12345678"]);
await elements["group-select"].trigger("change");
assert.equal(elements["group-detail-panel"].parentNode, elements["group-detail-home"]);
assert.equal(elements["group-reason-language"].value, "Japanese");
await secondButton.trigger("click");
assert.equal(elements["group-detail-panel"].parentNode, secondGroup);
assert.equal(elements["group-select"].value, JSON.stringify(["p1", "other-group-87654321"]));
assert.equal(secondButton.attributes["aria-expanded"], "true");
assert.equal(firstButton.attributes["aria-expanded"], "false");
await firstButton.trigger("click");
assert.equal(elements["group-detail-panel"].parentNode, firstGroup);
assert.equal(firstButton.attributes["aria-expanded"], "true");
assert.equal(secondButton.attributes["aria-expanded"], "false");
assert.equal(elements["group-reason-language"].value, "Japanese");

let scopedRule = elements["current-rules"].children[0];
if (scopedRule.children[2].textContent !== "收起") await scopedRule.children[2].trigger("click");
elements.text.value = "preserved across group selection";
await elements.text.trigger("input");
elements["group-select"].value = JSON.stringify(["p2", "separate-group"]);
await elements["group-select"].trigger("change");
assert.equal(elements["rule-form"].hidden, false);
assert.equal(elements["new-rule"].textContent, "取消");
elements["group-select"].value = JSON.stringify(["p1", "long-group-12345678"]);
await elements["group-select"].trigger("change");
scopedRule = elements["current-rules"].children[0];
assert.equal(elements.text.value, "preserved across group selection");
assert.equal(scopedRule.children[2].textContent, "收起");
await scopedRule.children[2].trigger("click");
assert.equal(elements["rule-form"].hidden, true);
elements["group-select"].value = JSON.stringify(["p2", "separate-group"]);
await elements["group-select"].trigger("change");
elements["group-select"].value = JSON.stringify(["p1", "long-group-12345678"]);
await elements["group-select"].trigger("change");
scopedRule = elements["current-rules"].children[0];
assert.equal(scopedRule.children[2].textContent, "编辑");
await scopedRule.children[2].trigger("click");
assert.equal(elements.text.value, "<script>");

elements.text.value = "discard from another group";
await elements.text.trigger("input");
elements["group-select"].value = JSON.stringify(["p2", "separate-group"]);
await elements["group-select"].trigger("change");
assert.equal(elements["new-rule"].textContent, "取消");
await elements["new-rule"].trigger("click");
assert.equal(elements["rule-form"].hidden, true);
assert.equal(elements["new-rule"].textContent, "新建规则");
elements["group-select"].value = JSON.stringify(["p1", "long-group-12345678"]);
await elements["group-select"].trigger("change");
scopedRule = elements["current-rules"].children[0];
await scopedRule.children[2].trigger("click");
assert.equal(elements.text.value, "<script>");
await scopedRule.children[2].trigger("click");

await elements["new-rule"].trigger("click");
assert.equal(elements["new-rule"].textContent, "取消");
assert.equal(elements["new-rule"].attributes["aria-expanded"], "true");
elements.rule.value = "unsaved-new-rule";
elements.text.value = "unsaved new draft";
await elements.text.trigger("input");
await elements["new-rule"].trigger("click");
assert.equal(elements["rule-form"].hidden, true);
assert.equal(elements["new-rule"].textContent, "新建规则");
assert.equal(elements["new-rule"].attributes["aria-expanded"], "false");
await elements["new-rule"].trigger("click");
assert.equal(elements.rule.value, "");
assert.equal(elements.text.value, "");
scopedRule = elements["current-rules"].children[0];
await scopedRule.children[2].trigger("click");
assert.equal(scopedRule.children[2].textContent, "收起");
assert.equal(elements["new-rule"].textContent, "新建规则");
assert.equal(elements.text.value, "<script>");
await scopedRule.children[2].trigger("click");
await elements["new-rule"].trigger("click");
elements["group-select"].value = "";
await elements["group-select"].trigger("change");
assert.equal(elements["new-rule"].textContent, "取消");
assert.equal(elements["new-rule"].disabled, false);
await elements["new-rule"].trigger("click");
assert.equal(elements["new-rule"].disabled, false);
assert.equal(elements["rule-form"].hidden, true);
await elements["new-rule"].trigger("click");
assert.equal(elements["rule-group-picker"].hidden, false);
elements["rule-group"].value = JSON.stringify(["p1", "long-group-12345678"]);
await elements["rule-group"].trigger("change");
await elements["new-rule"].trigger("click");

elements["group-select"].value = JSON.stringify(["p1", "long-group-12345678"]);
await elements["group-select"].trigger("change");
await elements["new-rule"].trigger("click");
elements.rule.value = "created-rule";
elements.text.value = "new rule";
await elements.preview.trigger("click");
const createdCard = elements["current-rules"].children[1];
assert.equal(createdCard.children[2].textContent, "收起");
assert.equal(elements["new-rule"].textContent, "新建规则");
assert.equal(elements["rule-form"].parentNode, createdCard);
await createdCard.children[2].trigger("click");
assert.equal(elements["rule-form"].hidden, true);

let toggleCard = elements["current-rules"].children[0];
let toggle = toggleCard.children[0].children[0].children[1].children[1];
toggle.checked = false;
await toggle.trigger("change");
const togglePreview = requests.findLast((request) => request.endpoint === "rules/preview");
assert.equal(togglePreview.body.enabled, false);
assert.equal(togglePreview.body.text, "<script>");
assert.equal(togglePreview.body.actions[0].severity_seconds.medium, 300);
toggleCard = elements["current-rules"].children[0];
toggle = toggleCard.children[0].children[0].children[1].children[1];
assert.equal(toggle.checked, false);
assert.match(latestFeedback().children[0].textContent, /规则已关闭/);
await toggleCard.children[2].trigger("click");
elements.text.value = "draft retained after toggle";
await elements.text.trigger("input");
const versionBeforeToggle = Number(elements.version.value);
toggle.checked = true;
await toggle.trigger("change");
assert.equal(elements.text.value, "draft retained after toggle");
assert.equal(Number(elements.version.value), versionBeforeToggle + 1);
toggleCard = elements["current-rules"].children[0];
toggle = toggleCard.children[0].children[0].children[1].children[1];
assert.equal(toggle.checked, true);
failRuleConfirm = true;
toggle.checked = false;
await toggle.trigger("change");
failRuleConfirm = false;
toggleCard = elements["current-rules"].children[0];
assert.equal(toggleCard.children[0].children[0].children[1].children[1].checked, true);
assert.match(latestFeedback().children[0].textContent, /version conflict/);
assert.equal(elements.text.value, "draft retained after toggle");
await toggleCard.children[2].trigger("click");

await elements["tab-records"].trigger("click");
elements["group-select"].value = "";
await elements["group-select"].trigger("change");
assert.deepEqual(requests.filter((request) => request.endpoint === "ui-state" && Object.hasOwn(request.body || {}, "platform_id")).at(-1).body,
  {platform_id: "", group_id: ""});
assert.equal(elements["group-select"].children[0].textContent, "所有群");
assert.equal(elements["records-body"].children.length, 4);
assert.equal(elements["stat-latency"].textContent, "200 ms");
assert.equal(elements["records-body"].children[0].children[0].children[0].textContent, "新的备注");
assert.match(elements["records-body"].children[2].children[0].children[0].textContent, /^平台群名 \([12]\)$/);
assert.ok(!elements["records-body"].children[2].children[0].children[0].title);
await elements["records-body"].children[0].trigger("click");
await elements["records-body"].children[2].trigger("click");
await new Promise((resolve) => setImmediate(resolve));
assert.deepEqual(requests.filter((request) => request.endpoint === "message-content").slice(-2).map((request) => request.params), [
  {platform_id: "p1", group_id: "long-group-12345678", message_id: "same"},
  {platform_id: "p2", group_id: "separate-group", message_id: "same"},
]);
liveCallbacks.onMessage({parsed: {id: 6, platform_id: "p2", group_id: "separate-group", message_id: "same", status: "no_action",
  detail: '{"model_latency_ms":500}', created_at: "2026-11-01T12:36:00Z"}});
assert.equal(elements["records-body"].children.length, 4);
assert.equal(elements["stat-latency"].textContent, "300 ms");
await elements["tab-rules"].trigger("click");
assert.equal(elements["current-rules"].children.length, 3);
assert.match(elements["current-rules"].children[0].children[0].textContent, /新的备注/);
await elements["tab-groups"].trigger("click");
assert.equal(elements["group-picker"].hidden, true);
assert.equal(elements["groups-list"].children[0].children[1].textContent, "关闭监控");
assert.equal(elements["groups-list"].children[0].children[1].attributes["data-tone"], "error");
assert.equal(elements["groups-list"].children[0].children[2].textContent, "查看群规则");
await elements["groups-list"].children[1].children[2].trigger("click");
assert.equal(elements["group-select"].value, JSON.stringify(["p1", "other-group-87654321"]));
assert.equal(elements["tab-rules"].attributes["aria-selected"], "true");
await elements["tab-settings"].trigger("click");
assert.equal(elements["group-picker"].hidden, true);
await elements["tab-status"].trigger("click");
assert.equal(elements["group-picker"].hidden, false);

assert.ok(!html.includes('id="platform-select"'));
assert.ok(!html.includes('id="platform"'));
assert.ok(!html.includes('id="group"'));
discovery = {history_available: true, truncated: false, groups: [
  {platform_id: "p1", group_id: "long-group-12345678", name: "已有群"},
  {platform_id: "p1", group_id: "new-group", name: ""},
  {platform_id: "p2", group_id: "no-name", name: ""},
]};
elements["add-group-panel"].open = true;
await elements["add-group-panel"].trigger("toggle");
assert.deepEqual(elements["available-group-select"].children.map((option) => option.textContent),
  ["请选择群", "自动获取的群名", "未命名群"]);
assert.match(elements["add-group-status"].textContent, /部分群名不可用/);
assert.equal(elements["add-group"].disabled, true);
elements["available-group-select"].value = JSON.stringify(["p1", "new-group"]);
await elements["available-group-select"].trigger("change");
assert.equal(elements["add-group"].disabled, false);
await elements["add-group"].trigger("click");
assert.deepEqual(requests.filter((request) => request.endpoint === "group-register").at(-1).body,
  {platform_id: "p1", group_id: "new-group"});
assert.equal(elements["group-select"].value, JSON.stringify(["p1", "new-group"]));
assert.deepEqual(elements["available-group-select"].children.map((option) => option.textContent), ["请选择群", "未命名群"]);
assert.equal(elements["add-group"].disabled, true);
discovery = {groups: [], history_available: true, truncated: false};
await elements["refresh-available-groups"].trigger("click");
assert.match(elements["add-group-status"].textContent, /在目标群向机器人发送消息/);
discovery.platforms_available = false;
await elements["refresh-available-groups"].trigger("click");
assert.match(elements["add-group-status"].textContent, /请先连接机器人/);
discoveryFailed = true;
await elements["refresh-available-groups"].trigger("click");
assert.match(elements["add-group-status"].textContent, /读取失败/);

const {setPanelVisibility, setDetailsOpen, updateVisiblePanel} = globalThis.__disclosureTest;
delete globalThis.__disclosureTest;
let reducedMotion = false;
window.matchMedia = () => ({matches: reducedMotion});
window.getComputedStyle = (element) => element.computed || {
  opacity: "1", paddingTop: "10px", paddingBottom: "10px", marginTop: "8px", marginBottom: "0px",
  borderTopWidth: "1px", borderBottomWidth: "1px"};
function animatedNode() {
  return {isConnected: true, hidden: true, inert: false, height: 200,
    style: {overflow: "visible", minHeight: "5px"}, animations: [],
    getBoundingClientRect() { return {height: this.height}; },
    animate(frames, options) {
      const element = this;
      const animation = {frames, options, cancelled: false, cancel() { this.cancelled = true; element.height = 200; }};
      this.animations.push(animation);
      return animation;
    }};
}
const animatedPanel = animatedNode();
let hiddenCallbacks = 0;
setPanelVisibility(animatedPanel, true);
assert.equal(animatedPanel.hidden, false);
assert.equal(animatedPanel.animations[0].frames[0].height, "0px");
assert.equal(animatedPanel.animations[0].frames[1].height, "200px");
animatedPanel.height = 75;
setPanelVisibility(animatedPanel, false, {onHidden: () => { hiddenCallbacks++; }});
assert.equal(animatedPanel.animations[0].cancelled, true);
assert.equal(animatedPanel.animations[1].frames[0].height, "75px");
assert.equal(animatedPanel.inert, true);
animatedPanel.height = 30;
setPanelVisibility(animatedPanel, true);
assert.equal(animatedPanel.animations[1].cancelled, true);
assert.equal(animatedPanel.animations[2].frames[0].height, "30px");
animatedPanel.animations[1].onfinish();
assert.equal(animatedPanel.hidden, false);
assert.equal(hiddenCallbacks, 0);
animatedPanel.animations[2].onfinish();
assert.equal(animatedPanel.hidden, false);
assert.equal(animatedPanel.inert, false);
assert.equal(hiddenCallbacks, 0);
assert.equal(animatedPanel.style.overflow, "visible");
assert.equal(animatedPanel.style.minHeight, "5px");
setPanelVisibility(animatedPanel, false, {onHidden: () => { hiddenCallbacks++; }});
animatedPanel.animations[3].onfinish();
assert.equal(animatedPanel.hidden, true);
assert.equal(hiddenCallbacks, 1);
reducedMotion = true;
setPanelVisibility(animatedPanel, true);
setPanelVisibility(animatedPanel, false);
assert.equal(animatedPanel.hidden, true);
assert.equal(animatedPanel.animations.length, 4);

reducedMotion = false;
const changingPanel = animatedNode();
changingPanel.hidden = false;
updateVisiblePanel(changingPanel, () => { changingPanel.height = 350; });
assert.equal(changingPanel.animations[0].frames[0].height, "200px");
assert.equal(changingPanel.animations[0].frames[1].height, "350px");
changingPanel.animations[0].onfinish();
const summary = {attributes: {}, inert: false, computed: {marginTop: "0px", marginBottom: "0px"},
  getBoundingClientRect() { return {height: 20}; }, setAttribute(key, value) { this.attributes[key] = value; }};
const detailChild = {inert: false};
const nativeDetails = animatedNode();
nativeDetails.hidden = false;
nativeDetails.open = false;
nativeDetails.children = [summary, detailChild];
nativeDetails.querySelector = () => summary;
nativeDetails.computed = {paddingTop: "0px", paddingBottom: "0px", borderTopWidth: "0px", borderBottomWidth: "0px"};
nativeDetails.classList = {toggle() {}};
nativeDetails.getBoundingClientRect = function() { return {height: this.open ? this.height : 20}; };
setDetailsOpen(nativeDetails, true, true);
assert.equal(nativeDetails.open, true);
assert.equal(summary.attributes["aria-expanded"], "true");
assert.equal(nativeDetails.animations[0].frames[0].height, "20px");
assert.equal(nativeDetails.animations[0].frames[1].height, "200px");
nativeDetails.animations[0].onfinish();
setDetailsOpen(nativeDetails, false, true);
assert.equal(summary.attributes["aria-expanded"], "false");
assert.equal(detailChild.inert, true);
nativeDetails.animations[1].onfinish();
assert.equal(nativeDetails.open, false);
assert.equal(detailChild.inert, false);
setDetailsOpen(nativeDetails, true, true);
setDetailsOpen(nativeDetails, false);
assert.equal(nativeDetails.open, false);
assert.equal(nativeDetails.animations[2].cancelled, true);
nativeDetails.animations[2].onfinish();
assert.equal(nativeDetails.open, false);
