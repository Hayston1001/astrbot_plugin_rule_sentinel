import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";

function node() {
  let text = "";
  const classes = new Set();
  return {value: "", checked: false, disabled: false, children: [], handlers: {}, attributes: {}, hidden: false,
    classList: {add(value) { classes.add(value); }, remove(value) { classes.delete(value); }, contains(value) { return classes.has(value); }},
    set textContent(value) { text = value; this.children = []; }, get textContent() { return text; },
    get parentNode() { return this.parent || null; },
    appendChild(child) { this.insertBefore(child, null); },
    insertBefore(child, anchor) { if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1); child.parent = this; const index = anchor ? this.children.indexOf(anchor) : this.children.length; this.children.splice(index, 0, child); },
    remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); },
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(type, handler) { (this.handlers[type] ||= []).push(handler); },
    async trigger(type) { await Promise.all((this.handlers[type] || []).map((handler) => handler({target: this, preventDefault() {}}))); },
    set innerHTML(value) { throw new Error("unexpected HTML rendering"); },
  };
}
const elements = {};
const latestFeedback = () => elements["feedback-region"].children[0];
const documentHandlers = {};
globalThis.document = {hidden: false, addEventListener(type, callback) { documentHandlers[type] = callback; }, getElementById(id) { return elements[id] ||= node(); }, createElement: node};
let timer = null;
globalThis.setTimeout = (callback) => { timer = callback; return 1; };
globalThis.clearTimeout = () => { timer = null; };
const requests = [];
let noRecords = false;
let modelFetchFails = false;
let statusResult = {monitoring: "dry_run", backend: "llm"};
let statusFails = false;
const firstPage = Array.from({length: 50}, (_, index) => ({id: 100 - index, message_id: `m${index}`,
  status: index % 2 ? "no_action" : "proposed", detail: index === 0 ? '{"rules":[{"rule_id":"r1"},{"rule_id":"r2"}]}' : "{}", created_at: "2026-01-01T00:00:00Z",
  events: [{id: index * 2 + 100, status: "started", created_at: "2026-01-01T00:00:00Z"},
    {id: index * 2 + 101, status: index % 2 ? "no_action" : "proposed", created_at: "2026-01-01T00:00:01Z"}]}));
for (const [index, row] of firstPage.entries()) row.created_at = new Date(Date.UTC(2026, 0, 1, 0, 0, 50 - index)).toISOString();
firstPage[0].detail = JSON.stringify({proposal: {rule_id: "no-ads", rule_version: 1, action: "mute"}, model_latency_ms: 5});
firstPage[2].detail = JSON.stringify({proposal: {rule_id: "ads", rule_version: 1, action: "mute", reason: "unique reason for searching"}, model_latency_ms: 900});
firstPage[4].detail = JSON.stringify({model_latency_ms: 10});
firstPage[1].detail = JSON.stringify({rules: [{rule_id: "no-ads", version: 1}]});
firstPage[3].detail = JSON.stringify({no_action_reason_enabled: false, rules: Array.from({length: 5}, (_, i) => ({rule_id: `rule-${i}`, version: i + 1}))});
firstPage[5].detail = JSON.stringify({reason: "未违反规则, 模型未提供具体理由."});
firstPage[7].detail = JSON.stringify({reason: "消息内容与群规则相符."});
firstPage[9].detail = JSON.stringify({reason: "non_text_or_unknown"});
firstPage[10].detail = JSON.stringify({model_latency_ms: 8});
firstPage[11].detail = JSON.stringify({reason: "消息包含非文本内容或类型无法识别."});
const longRuleId = "this-rule-identifier-is-extremely-long-and-should-be-truncated";
firstPage[26].detail = JSON.stringify({proposal: {rule_id: longRuleId, rule_version: 3, action: "mute"}});
firstPage[26].execution_summary = "partial_failed";
firstPage[26].executions = [{action: "recall", status: "succeeded"}, {action: "mute", status: "inactive"}];
firstPage[28].detail = JSON.stringify({proposal: {rule_id: "plan", rule_version: 1, action: "mute", duration_seconds: 60}});
firstPage[28].execution_summary = "inactive";
firstPage[28].executions = [{action: "mute", status: "inactive", duration_seconds: 60}];
firstPage[20].execution_summary = "succeeded";
firstPage[20].executions = [{action: "recall", status: "succeeded"}, {action: "mute", status: "succeeded", duration_seconds: 60}];
firstPage[0].execution = {status: "succeeded", attempts: 1, platform_code: "200", trace_id: "trace-1", expires_at: "2026-01-01T00:01:00Z"};
firstPage[2].execution = {action: "mute", status: "blocked", attempts: 1, platform_code: "40103004", reason_code: "protected_target",
  notice: {status: "succeeded", attempts: 1, platform_code: "200", trace_id: "notice-trace"}};
firstPage[2].execution_summary = "blocked";
firstPage[2].executions = [firstPage[2].execution];
firstPage[4].execution = {status: "permission_denied", attempts: 1};
firstPage[6].execution = {status: "unknown", attempts: 1};
firstPage[8].execution = {status: "failed", platform_code: "400"};
firstPage[10].execution = {status: "inactive"};
firstPage[12].execution = {action: "mute", status: "blocked", notice: {status: "succeeded", attempts: 2, platform_code: "200", trace_id: "notice-sent", sent_quoted: 1, downgraded: 1, downgrade_reason: "passive_expired"}};
firstPage[14].execution = {action: "mute", status: "blocked", notice: {status: "failed", attempts: 2, platform_code: "400", send_method: "text", downgrade_reason: "markdown_rejected"}};
firstPage[14].execution_summary = "blocked";
firstPage[14].executions = [firstPage[14].execution];
firstPage[16].execution = {action: "mute", status: "failed", platform_code: "40103004", attempts: 3};
firstPage[16].execution_summary = "failed";
firstPage[16].executions = [firstPage[16].execution];
firstPage[18].execution_summary = "pending_confirm";
firstPage[18].executions = [{action: "recall", status: "succeeded", mode: "auto"},
  {action: "mute", status: "pending_confirm", mode: "confirm", duration_seconds: 60}];
firstPage[18].execution = firstPage[18].executions[0];
firstPage[22].execution = {status: "executing", attempts: 1};
firstPage[24].execution = {status: "review"};
firstPage[30].execution = {action: "recall", status: "succeeded", attempts: 1};
firstPage[32].execution = {action: "recall", status: "blocked", reason_code: "protected_target"};
firstPage[34].execution = {action: "recall", status: "blocked", reason_code: "recall_expired"};
firstPage[36].execution = {action: "recall", status: "failed", reason_code: "invalid_message"};
firstPage[38].execution = {action: "mute", status: "unknown", platform_code: "40103004"};
firstPage[40].detail = JSON.stringify({proposal: {rule_id: "plan", rule_version: 3, duration_seconds: 60},
  rules: [{rule_id: "plan", version: 3, actions: [{type: "recall", mode: "auto"},
    {type: "reply", mode: "confirm"}, {type: "mute", mode: "auto"}]}]});
firstPage[42].detail = JSON.stringify({proposal: {rule_id: "plan", rule_version: 3, duration_seconds: 180},
  rules: [{rule_id: "plan", version: 2, actions: [{type: "mute", mode: "auto"}]}]});
firstPage[44].detail = JSON.stringify({proposal: {rule_id: "legacy", rule_version: 1, action: "mute", duration_seconds: 120}});
firstPage[45].status = "timed_out";
firstPage[45].detail = "decision_timeout";
firstPage[46].execution_summary = "partial_failed";
firstPage[46].executions = [{action: "recall", status: "succeeded"},
  {action: "reply", status: "failed", platform_code: "400"},
  {action: "mute", status: "blocked", reason_code: "protected_target", platform_code: "40103004", duration_seconds: 60}];
firstPage[46].execution = firstPage[46].executions[0];
firstPage[48].execution_summary = "partial_failed";
firstPage[48].executions = [{action: "recall", status: "blocked", reason_code: "protected_target"},
  {action: "mute", status: "failed", platform_code: "40103004"}];
firstPage[48].execution = firstPage[48].executions[0];
let stream;
globalThis.window = {addEventListener() {}, AstrBotPluginView: {
  async subscribeSSE(endpoint, callbacks) { stream = callbacks; return "test"; }, async unsubscribeSSE() {},
  async ready() {},
  async apiGet(endpoint, params) {
    requests.push({endpoint, params});
    if (endpoint === "status") {
      if (statusFails) throw new Error("status unavailable");
      return statusResult;
    }
    if (endpoint === "settings") return {monitoring_mode: "allowlist", monitoring_groups: [],
      decision_backend: "", rule_selection_strategy: "model", llm_provider_id: "old-provider", max_duration_seconds: 30,
      model_concurrency: 2, model_timeout_seconds: 30, global_rate_per_minute: 60, group_rate_per_minute: 30,
      model_failure_threshold: 5, model_cooldown_seconds: 60, queue_capacity_per_group: 100,
      ingress_capacity: 256, notification_target: "", notification_interval_seconds: 0, jev_model: "", jev_provider: "typesafe", jev_api_key_set: true,
      default_action_mode: "auto", message_retention_days: 30, llm_reason_language: "", llm_no_action_reason_enabled: true,
      protected_target_policy: "group_message", protected_target_template: "hello {sender_id}", protected_target_quote: true};
    if (endpoint === "providers") return [{id: "example"}];
    if (endpoint === "ui-state") return {platform_id: "p", group_id: "g"};
    if (endpoint === "platforms") return [{id: "p", type: "qq_official"}];
    if (endpoint === "groups") return [{platform_id: "p", group_id: "g", rule_count: 0}];
    if (endpoint === "rules") return [];
    if (endpoint === "message-content") return {content: "original <text>"};
    if (endpoint === "messages") return noRecords ? [] : params.before_id === undefined ? firstPage : [
      {id: 50, message_id: "next", status: "skipped", detail: "non_text_or_unknown", events: []}];
    if (endpoint === "summary") return {total_messages: 51, status_counts: {proposed: 25, no_action: 25, skipped: 1},
      recent_24h: {messages: 9, proposed: 4, no_action: 5}, latency_p95_ms: 125, truncated: false, limit: 5000};
    throw new Error("unexpected endpoint");
  },
  async apiPost(endpoint, body) {
    requests.push({endpoint, body});
    if (endpoint === "jev/models") {
      if (modelFetchFails) throw new Error("private provider message");
      return {models: [body.provider === "openrouter" ? "typesafe/jev-latest" : "jev-latest"]};
    }
    return {settings: {...body, monitoring_groups: [], jev_api_key_set: true}};
  },
}};
const source = await readFile(process.argv[2], "utf8");
const html = await readFile(process.argv[2].replace(/app\.js$/, "index.html"), "utf8");
const headings = [...html.match(/<thead><tr>(.*?)<\/tr><\/thead>/s)[1].matchAll(/<th[^>]*>(.*?)<\/th>/g)].map((match) => match[1]);
assert.deepEqual(headings, ["时间", "结果", "理由摘要", "规则 ID / 版本", "模型延迟"]);
const options = (id) => [...html.match(new RegExp(`<select id="${id}">(.*?)</select>`, "s"))[1].matchAll(/<option[^>]*>(.*?)<\/option>/g)].map((match) => match[1]);
assert.deepEqual(options("setting-decision_backend"), ["普通 LLM", "Jev"]);
assert.deepEqual(options("records-decision-filter"), ["全部", "拟执行", "全部成功", "部分失败", "已撤回", "全部无法执行", "无法撤回", "已超时不可撤回", "全部失败", "撤回失败", "结果不明", "待确认", "未违反规则", "跳过", "其他"]);
assert.deepEqual(options("records-execution-filter"), ["全部", "全部成功", "部分失败", "已撤回", "无法执行", "无法撤回", "已超时不可撤回", "执行失败", "撤回失败", "结果不明", "待确认", "无执行"]);
assert.ok(!html.includes('class="chip'));
assert.ok(html.indexOf('id="records-search"') < html.indexOf('id="records-decision-filter"'));
assert.ok(html.indexOf('id="records-decision-filter"') < html.indexOf('id="records-execution-filter"'));
assert.ok(html.indexOf('id="records-execution-filter"') < html.indexOf('id="records-sort"'));
await import("data:text/javascript;base64," + Buffer.from(source + "\nglobalThis.__refreshStatusForTest = refreshStatus;\n").toString("base64"));
const refreshStatusForTest = globalThis.__refreshStatusForTest;
delete globalThis.__refreshStatusForTest;
assert.equal(elements.status.attributes["data-tone"], "success");
assert.equal(elements.status.textContent, "运行中");
for (const [value, label, tone] of [
  [{monitoring: "dry_run", backend: "llm", action_execution_available: true}, "运行中", "success"],
  [{monitoring: "dry_run", backend: "llm", action_execution_available: false}, "运行中", "success"],
  [{monitoring: "dry_run", backend: "jev", action_execution_available: false}, "运行中", "success"],
  [{monitoring: "disabled", backend: "llm"}, "未运行", "warning"],
  [{monitoring: "invalid_configuration", backend: "llm", start_error: "invalid_configuration"}, "未运行", "warning"],
  [{monitoring: "disabled", backend: "jev", start_error: "start_failed"}, "未运行", "warning"],
]) {
  statusResult = value;
  await refreshStatusForTest();
  assert.equal(elements.status.textContent, label);
  assert.equal(elements.status.attributes["data-tone"], tone);
}
assert.equal(elements["status-label"].textContent, "监控未启动");
assert.match(elements["status-detail"].textContent, /启动失败/);
statusResult = {monitoring: "disabled", backend: "llm", start_error: "model_not_configured",
  action_execution_enabled: true, action_execution_available: false};
await refreshStatusForTest();
assert.equal(elements["status-label"].textContent, "监控未启动");
assert.match(elements["status-detail"].textContent, /请先选择默认判定模型/);
assert.equal(elements["status-backend"].textContent, "普通 LLM");
assert.equal(elements["status-execution"].textContent, "不可用");
for (const [enabled, available, detail] of [
  [true, true, "消息判定和真实执行已开启. 各群按设置处理规则动作."],
  [true, false, "消息判定已开启, 真实执行不可用."],
  [false, false, "消息判定已开启, 仅记录结果."],
]) {
  statusResult = {monitoring: "dry_run", backend: "llm", action_execution_enabled: enabled, action_execution_available: available};
  await refreshStatusForTest();
  assert.equal(elements["status-detail"].textContent, detail);
}
statusResult = {monitoring: "disabled", backend: "llm"};
await refreshStatusForTest();
assert.match(elements["status-detail"].textContent, /监控设置/);
statusFails = true;
await refreshStatusForTest();
assert.equal(elements.status.textContent, "状态不可用");
assert.equal(elements.status.attributes["data-tone"], "warning");
statusFails = false;
statusResult = {monitoring: "dry_run", backend: "llm"};
await refreshStatusForTest();
assert.equal(elements["stat-messages"].textContent, "9");
assert.equal(elements["stat-proposed"].textContent, "4");
assert.equal(elements["stat-no-action"].textContent, "5");
assert.equal(elements["stat-latency"].textContent, "305 ms");
assert.equal(elements["decision-option-all"].textContent, "全部 (51)");
assert.equal(elements["execution-option-all"].textContent, "全部 (51)");
assert.equal(elements["decision-option-no_action"].textContent, "未违反规则 (25)");
assert.equal(elements["decision-option-skipped"].textContent, "跳过 (1)");
assert.equal(elements["records-body"].children.length, 100);
const recordId = (row) => row.attributes["aria-label"].slice("查看消息 ".length, -" 的详情".length);
assert.equal(elements["records-body"].children[0].children.length, 5);
assert.equal(recordId(elements["records-body"].children[0]), "m0");
assert.equal(recordId(elements["records-body"].children[98]), "m49");
assert.equal(elements["records-body"].children[0].children[3].textContent, "no-ads / 1");
assert.equal(elements["records-body"].children[2].children[3].textContent, "-");
assert.equal(elements["records-body"].children[6].children[3].textContent, "-");
assert.equal(elements["records-body"].children[6].children[3].title, "");
assert.equal(elements["records-body"].children[10].children[3].textContent, "-");
assert.equal(elements["records-body"].children[52].children[3].title, `${longRuleId} / 3`);
assert.ok(elements["records-body"].children[52].children[3].textContent.length < longRuleId.length);
const resultBadge = (index) => elements["records-body"].children[index * 2].children[1].children[0];
const badge = (index) => resultBadge(index).textContent;
const detailAt = (index) => elements["records-body"].children[index * 2 + 1].children[0].children[0];
const section = (detail, title) => detail.children.flatMap((item) => item.children).find((item) => item.children[0]?.textContent === title);
const rawAt = (index) => detailAt(index).children.find((item) => item.className === "record-raw");
assert.equal(badge(0), "已禁言");
assert.equal(badge(1), "未违反规则");
assert.equal(resultBadge(1).attributes["data-tone"], "muted");
assert.equal(badge(2), "全部失败");
assert.equal(badge(4), "无法执行");
assert.equal(badge(6), "结果不明");
assert.equal(badge(8), "执行失败");
assert.equal(badge(10), "拟执行");
assert.equal(badge(14), "全部无法执行");
assert.equal(badge(16), "全部失败");
assert.equal(badge(18), "待确认");
assert.equal(badge(20), "全部成功");
assert.equal(badge(22), "执行中");
assert.equal(badge(24), "需复核");
assert.equal(badge(30), "已撤回");
assert.equal(badge(32), "全部失败");
assert.match(resultBadge(32).title, /目标是管理员或机器人/);
assert.equal(badge(34), "已超时不可撤回");
assert.equal(badge(36), "撤回失败");
assert.equal(badge(38), "结果不明");
assert.equal(badge(46), "部分失败");
assert.equal(badge(48), "全部失败");
assert.equal(resultBadge(2).title, resultBadge(16).title);
assert.equal(resultBadge(2).attributes["data-tone"], "error");
assert.equal(resultBadge(16).attributes["data-tone"], "error");
assert.equal(resultBadge(20).className, "badge record-executed");
assert.equal(resultBadge(20).attributes["data-tone"], "success");
assert.equal(resultBadge(30).className, "badge record-executed");
assert.equal(resultBadge(1).className, "badge");
assert.equal(resultBadge(28).className, "badge record-planned");
assert.equal(resultBadge(28).attributes["data-tone"], "warning");
elements["records-decision-filter"].value = "recalled";
await elements["records-decision-filter"].trigger("change");
assert.equal(recordId(elements["records-body"].children[0]), "m30");
assert.equal(elements["records-body"].children.length, 2);
elements["records-decision-filter"].value = "all";
await elements["records-decision-filter"].trigger("change");
assert.equal(resultBadge(0).title, undefined);
assert.equal(resultBadge(1).title, undefined);
assert.match(resultBadge(2).title, /目标是管理员或机器人/);
assert.match(resultBadge(4).title, /机器人权限不足/);
assert.match(resultBadge(6).title, /执行结果不明/);
assert.match(resultBadge(8).title, /未提供具体原因/);
assert.match(resultBadge(34).title, /超过可撤回时间/);
assert.match(resultBadge(36).title, /消息标识无效/);
assert.equal(resultBadge(45).title, "模型判定超时");
assert.match(resultBadge(46).title, /发言: 未提供具体原因/);
assert.match(resultBadge(46).title, /禁言 1分钟: 目标是管理员或机器人/);
const notice12 = section(detailAt(12), "处理动作").children[1].children[0].children.find((item) => item.textContent.startsWith("群提示:"));
assert.match(notice12.textContent, /已引用触发消息.*被动回复已过期/);
const notice14 = section(detailAt(14), "处理动作").children[1].children[0].children.find((item) => item.textContent.startsWith("群提示:"));
assert.match(notice14.textContent, /提示发送失败.*已降级为纯文本/);
assert.match(rawAt(2).children[1].textContent, /notice-trace/);
assert.equal(elements["records-body"].children[1].hidden, true);
await elements["records-body"].children[0].trigger("click");
await Promise.resolve();
assert.equal(elements["records-body"].children[1].hidden, false);
const detail = detailAt(0);
assert.equal(section(detail, "消息标识").children[1].textContent, "m0");
assert.equal(section(detail, "消息原文").children[1].textContent, "original <text>");
assert.deepEqual(requests.findLast((request) => request.endpoint === "message-content"),
  {endpoint: "message-content", params: {platform_id: "p", group_id: "g", message_id: "m0"}});
assert.match(elements["records-body"].children[2].children[2].textContent, /模型未提供具体理由/);
assert.match(elements["records-body"].children[6].children[2].textContent, /已关闭理由记录/);
for (const index of [1, 3]) {
  assert.equal(elements["records-body"].children[index * 2].children[2].className, "record-reason-fallback");
  assert.equal(section(detailAt(index), "判定理由").children[1].className, "record-detail-value record-reason-fallback");
}
assert.equal(section(detailAt(1), "相关规则").children[1].textContent, "暂无规则信息.");
for (const index of [5, 7, 9, 11]) {
  assert.notEqual(elements["records-body"].children[index * 2].children[2].className, "record-reason-fallback");
  assert.equal(section(detailAt(index), "判定理由").children[1].className, "record-detail-value");
}
assert.equal(elements["records-body"].children[10].children[2].textContent, "未违反规则, 模型未提供具体理由.");
assert.equal(elements["records-body"].children[18].children[2].textContent, "non_text_or_unknown");
assert.equal(elements["records-body"].children[22].children[2].textContent, "消息包含非文本内容或类型无法识别.");
assert.equal(rawAt(0).children[0].textContent, "查看原始记录");
assert.ok(!rawAt(0).open);
assert.match(rawAt(0).children[1].textContent, /trace-1/);
assert.match(section(detail, "判定过程").children[1].children[0].textContent, /处理中/);
assert.match(section(detail, "判定过程").children[1].children[1].textContent, /拟执行/);
assert.match(section(detail, "处理动作").children[1].children[0].children[1].textContent, /到期时间/);
const plannedActions = section(detailAt(40), "处理动作").children[1].children;
assert.deepEqual(plannedActions.map((item) => item.children[0].children[0].textContent), ["1. 撤回", "2. 发言", "3. 禁言 1分钟"]);
assert.deepEqual(plannedActions.map((item) => item.children[0].children[1].textContent), ["拟执行", "拟执行", "拟执行"]);
for (const item of plannedActions) {
  assert.equal(item.children[0].children[1].className, "badge record-planned");
  assert.equal(item.children[0].children[1].attributes["data-tone"], "warning");
}
assert.equal(plannedActions[1].children[0].children[2].textContent, "人工确认");
const mixedActions = section(detailAt(18), "处理动作").children[1].children;
assert.deepEqual(mixedActions.map((item) => item.children[0].children[1].textContent), ["已执行", "待确认"]);
assert.equal(mixedActions[0].children[0].children[2].textContent, "自动");
assert.equal(mixedActions[1].children[0].children[2].textContent, "人工确认");
const completedActions = section(detailAt(20), "处理动作").children[1].children;
assert.deepEqual(completedActions.map((item) => item.children[0].children[1].textContent), ["已执行", "已执行"]);
for (const item of completedActions) {
  assert.equal(item.children[0].children[1].className, "badge record-executed");
  assert.equal(item.children[0].children[1].attributes["data-tone"], "success");
}
const inactive = section(detailAt(28), "处理动作").children[1].children[0].children[0].children[1];
assert.equal(inactive.textContent, "拟执行");
assert.equal(inactive.className, "badge record-planned");
assert.equal(inactive.attributes["data-tone"], "warning");
const partialInactive = section(detailAt(26), "处理动作").children[1].children[1].children[0].children[1];
assert.equal(partialInactive.textContent, "未执行");
assert.equal(partialInactive.attributes["data-tone"], "muted");
assert.equal(section(detailAt(42), "处理动作").children[1].textContent, "暂无动作信息.");
assert.equal(section(detailAt(44), "处理动作").children[1].children[0].children[0].children[0].textContent, "1. 禁言 2分钟");
assert.equal(section(detailAt(46), "结果说明"), undefined);
assert.equal(section(detailAt(45), "结果说明").children[1].textContent, "模型判定超时");
for (const index of [2, 16]) {
  const action = section(detailAt(index), "处理动作").children[1].children[0];
  assert.equal(action.children[0].children[1].textContent, "执行失败");
  assert.equal(action.children[0].children[1].attributes["data-tone"], "error");
  assert.match(action.children[1].textContent, /目标是管理员或机器人/);
}
assert.equal(JSON.parse(rawAt(2).children[1].textContent).executions[0].status, "blocked");
assert.equal(JSON.parse(rawAt(16).children[1].textContent).executions[0].status, "failed");
const keyboardRow = elements["records-body"].children[92];
const keyboardDetail = elements["records-body"].children[93];
let prevented = false;
keyboardRow.handlers.keydown[0]({key: "Enter", preventDefault() { prevented = true; }});
assert.equal(prevented, true);
assert.equal(keyboardDetail.hidden, false);
assert.equal(keyboardRow.attributes["aria-expanded"], "true");
keyboardRow.handlers.keydown[0]({key: " ", preventDefault() {}});
assert.equal(keyboardDetail.hidden, true);
const visibleIds = () => elements["records-body"].children.filter((_, i) => i % 2 === 0).map(recordId);
elements["records-decision-filter"].value = "failed";
await elements["records-decision-filter"].trigger("change");
for (const id of ["m2", "m16", "m48"]) assert.ok(visibleIds().includes(id));
assert.ok(!visibleIds().includes("m4"));
elements["records-execution-filter"].value = "failed";
await elements["records-execution-filter"].trigger("change");
for (const id of ["m2", "m16", "m48"]) assert.ok(visibleIds().includes(id));
elements["records-decision-filter"].value = "all";
await elements["records-decision-filter"].trigger("change");
elements["records-execution-filter"].value = "blocked";
await elements["records-execution-filter"].trigger("change");
assert.ok(visibleIds().includes("m4"));
for (const id of ["m2", "m16", "m48"]) assert.ok(!visibleIds().includes(id));
elements["records-execution-filter"].value = "all";
await elements["records-execution-filter"].trigger("change");
async function search(value) {
  elements["records-search"].value = value;
  await elements["records-search"].trigger("input");
  assert.ok(timer);
  timer();
}
await search("m49");
assert.deepEqual(visibleIds(), ["m49"]);
assert.equal(elements["stat-latency"].textContent, "—");
assert.equal(elements["records-match-count"].textContent, "匹配 1 条");
await search("no-ads");
assert.deepEqual(visibleIds(), ["m0", "m1"]);
assert.equal(elements["stat-latency"].textContent, "5 ms");
const beforeFilters = requests.filter((request) => request.endpoint !== "message-content").length;
elements["records-decision-filter"].value = "succeeded";
await elements["records-decision-filter"].trigger("change");
assert.deepEqual(visibleIds(), ["m0"]);
elements["records-execution-filter"].value = "none";
await elements["records-execution-filter"].trigger("change");
assert.deepEqual(visibleIds(), []);
assert.equal(elements["records-empty"].textContent, "没有匹配的记录");
assert.equal(requests.filter((request) => request.endpoint !== "message-content").length, beforeFilters);
elements["records-execution-filter"].value = "all";
await elements["records-execution-filter"].trigger("change");
elements["records-decision-filter"].value = "all";
await elements["records-decision-filter"].trigger("change");
await search("unique reason for searching");
assert.deepEqual(visibleIds(), ["m2"]);
await search("");
for (const [sort, first] of [["time_asc", "m49"], ["time_desc", "m0"], ["latency_desc", "m2"], ["latency_asc", "m0"]]) {
  elements["records-sort"].value = sort;
  await elements["records-sort"].trigger("change");
  assert.equal(visibleIds()[0], first);
  assert.equal(elements["stat-latency"].textContent, "305 ms");
}
elements["records-sort"].value = "time_desc";
await elements["records-sort"].trigger("change");
await elements["tab-settings"].trigger("click");
assert.equal(elements["setting-jev_api_key"].value, "");
assert.match(elements["jev-api-key-help"].textContent, /已配置密钥/);
assert.equal(elements["setting-model_concurrency"].value, "2");
assert.equal(elements["setting-protected_target_policy"].value, "group_message");
assert.equal(elements["setting-protected_target_template"].value, "hello {sender_id}");
assert.equal(elements["setting-protected_target_quote"].checked, true);
assert.equal(elements["setting-llm_no_action_reason_enabled"].checked, true);
assert.equal(elements["setting-jev_provider"].value, "typesafe");
assert.equal(elements["setting-decision_backend"].value, "llm");
await elements["settings-save"].trigger("click");
assert.equal(requests.findLast((request) => request.endpoint === "settings" && request.body).body.decision_backend, "");
assert.equal(elements["setting-decision_backend"].value, "llm");
elements["setting-llm_provider_id"].value = "new-provider";
await elements["settings-save"].trigger("click");
assert.equal(requests.findLast((request) => request.endpoint === "settings" && request.body).body.decision_backend, "llm");
elements["setting-decision_backend"].value = "jev";
await elements["setting-decision_backend"].trigger("change");
assert.equal(elements["jev-settings"].open, true);
elements["setting-jev_api_key"].value = "temporary-key";
await elements["jev-models-fetch"].trigger("click");
assert.deepEqual(requests.at(-1), {endpoint: "jev/models", body: {provider: "typesafe", api_key: "temporary-key"}});
assert.equal(elements["setting-jev_model"].value, "jev-latest");
assert.deepEqual(elements["jev-model-options"].children.map((item) => item.value), ["jev-latest"]);
elements["setting-jev_provider"].value = "openrouter";
await elements["setting-jev_provider"].trigger("change");
assert.equal(elements["setting-jev_model"].value, "jev-latest");
assert.deepEqual(elements["jev-model-options"].children, []);
await elements["jev-models-fetch"].trigger("click");
assert.equal(elements["setting-jev_model"].value, "typesafe/jev-latest");
assert.deepEqual(elements["jev-model-options"].children.map((item) => item.value), ["typesafe/jev-latest"]);
elements["setting-jev_model"].value = "manual-model";
modelFetchFails = true;
await elements["jev-models-fetch"].trigger("click");
assert.equal(elements["setting-jev_model"].value, "manual-model");
assert.equal(latestFeedback().children[0].textContent, "获取模型列表失败.");
assert.equal(latestFeedback().attributes["data-tone"], "error");
const dismissedFeedback = latestFeedback();
await dismissedFeedback.children[1].trigger("click");
assert.ok(dismissedFeedback.classList.contains("is-leaving"));
assert.ok(elements["feedback-region"].children.includes(dismissedFeedback));
timer();
assert.ok(!elements["feedback-region"].children.includes(dismissedFeedback));
assert.deepEqual(elements["jev-model-options"].children.map((item) => item.value), ["typesafe/jev-latest"]);
elements["setting-jev_provider"].value = "typesafe";
elements["setting-jev_api_key"].value = "new secret";
elements["setting-llm_no_action_reason_enabled"].checked = false;
await elements["settings-save"].trigger("click");
assert.equal(requests.findLast((request) => request.endpoint === "settings" && request.body).body.jev_api_key, "new secret");
assert.equal(requests.findLast((request) => request.endpoint === "settings" && request.body).body.protected_target_quote, true);
assert.equal(requests.findLast((request) => request.endpoint === "settings" && request.body).body.llm_no_action_reason_enabled, false);
assert.equal(elements["setting-llm_no_action_reason_enabled"].checked, false);
assert.equal(elements["setting-jev_api_key"].value, "");
const timeoutField = elements["setting-model_timeout_seconds"];
const advancedSection = {open: false};
timeoutField.closest = (selector) => selector === "details" ? advancedSection : {firstChild: {textContent: "单次判定超时, 秒 "}};
timeoutField.focus = () => { timeoutField.focused = true; };
timeoutField.value = "";
const beforeInvalidSettings = requests.length;
await elements["settings-save"].trigger("click");
assert.equal(requests.length, beforeInvalidSettings);
assert.equal(advancedSection.open, true);
assert.equal(timeoutField.focused, true);
assert.match(latestFeedback().children[0].textContent, /单次判定超时, 秒: 数值不能为空/);
timeoutField.value = "30";
const count = requests.length;
await elements["tab-rules"].trigger("click");
assert.equal(elements["panel-rules"].hidden, false);
assert.equal(elements["panel-records"].hidden, true);
assert.equal(elements["tab-rules"].attributes["aria-selected"], "true");
assert.equal(elements["current-rules"].textContent, "该群还没有规则.");
await elements["tab-status"].trigger("click");
assert.equal(elements["panel-status"].hidden, false);
await elements["tab-records"].trigger("click");
assert.equal(requests.length, count + 1);
elements["records-decision-filter"].value = "proposed";
await elements["records-decision-filter"].trigger("change");
assert.ok(elements["records-body"].children.length > 0);
elements["records-decision-filter"].value = "other";
await elements["records-decision-filter"].trigger("change");
assert.equal(elements["records-empty"].hidden, true);
elements["records-decision-filter"].value = "all";
await elements["records-decision-filter"].trigger("change");
await elements["records-next"].trigger("click");
assert.equal(elements["records-body"].children.length, 102);
assert.equal(elements["records-status"].textContent, "已加载 51 条消息.");
assert.equal(requests.filter((request) => request.endpoint === "messages").at(-1).params.before_id, 51);
const messageRows = elements["records-body"].children.filter((_, index) => index % 2 === 0);
assert.equal(messageRows.filter((row) => recordId(row) === "m0").length, 1);
assert.equal(recordId(messageRows.at(-1)), "next");
assert.equal(elements["records-body"].children[100].children[2].textContent, "消息包含非文本内容或类型无法识别.");
assert.equal(elements["records-body"].children[100].children[2].className, "record-reason-fallback");
assert.equal(section(detailAt(50), "判定理由").children[1].textContent, "消息包含非文本内容或类型无法识别.");
assert.equal(section(detailAt(50), "判定理由").children[1].className, "record-detail-value record-reason-fallback");
assert.equal(JSON.parse(rawAt(50).children[1].textContent).detail, "non_text_or_unknown");
await search("非文本内容");
assert.deepEqual(visibleIds(), ["m11", "next"]);
await search("");
elements["records-sort"].value = "latency_desc";
await elements["records-sort"].trigger("change");
stream.onMessage({parsed: {id: 200, message_id: "pushed", status: "no_action", created_at: "2026-01-02T00:00:00Z",
  detail: JSON.stringify({model_latency_ms: 700}), events: []}});
assert.deepEqual(visibleIds().slice(0, 3), ["m2", "pushed", "m4"]);
assert.equal(elements["stat-latency"].textContent, "404 ms");
elements["records-sort"].value = "time_desc";
await elements["records-sort"].trigger("change");
assert.equal(visibleIds()[0], "pushed");
noRecords = true;
await elements["refresh-group"].trigger("click");
assert.equal(elements["records-empty"].textContent, "该群还没有判定记录.");
assert.equal(elements["records-empty"].hidden, false);
