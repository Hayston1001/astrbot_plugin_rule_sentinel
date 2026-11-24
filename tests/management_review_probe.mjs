import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";

process.env.TZ = "Asia/Shanghai";
function node(tag = "div") {
  let text = "";
  const classes = new Set();
  const attributes = {};
  const item = {tag, value: "", checked: false, disabled: false, hidden: false, title: "", open: false, className: "",
    children: [], handlers: {}, dataset: {}, parent: null, isConnected: true,
    classList: {add(value) { classes.add(value); }, remove(value) { classes.delete(value); }, contains(value) { return classes.has(value); },
      toggle(value, force) { if (force ?? !classes.has(value)) classes.add(value); else classes.delete(value); }},
    set textContent(value) { text = String(value); for (const child of this.children) child.parent = null; this.children = []; },
    get textContent() { return text + this.children.map((child) => child.textContent).join(""); },
    get parentNode() { return this.parent; },
    appendChild(child) { this.insertBefore(child, null); },
    insertBefore(child, anchor) {
      if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
      child.parent = this;
      this.children.splice(anchor ? this.children.indexOf(anchor) : this.children.length, 0, child);
    },
    remove() { if (this.parent) { this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; } },
    setAttribute(name, value) { attributes[name] = String(value); },
    getAttribute(name) { return attributes[name] ?? null; },
    removeAttribute(name) { delete attributes[name]; },
    attributes,
    addEventListener(type, handler) { (this.handlers[type] ||= []).push(handler); },
    async trigger(type, event = {preventDefault() {}, stopPropagation() {}}) {
      await Promise.all((this.handlers[type] || []).map((handler) => handler(event)));
    },
    focus() { document.activeElement = this; },
    querySelectorAll(selector) {
      const found = [];
      const visit = (current) => {
        for (const child of current.children) {
          if ((selector === "details[open]" && child.tag === "details" && child.open)
              || (selector === "button[data-focus-key]" && child.tag === "button" && child.dataset.focusKey !== undefined)) found.push(child);
          visit(child);
        }
      };
      visit(this);
      return found;
    },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
    set innerHTML(value) { throw new Error("untrusted HTML must not be rendered"); },
  };
  return item;
}
const elements = new Proxy({}, {get(target, id) { return target[id] ||= node(); }});
globalThis.document = {hidden: false, activeElement: null, addEventListener() {}, getElementById(id) { return elements[id] ||= node(); }, createElement: node};
globalThis.window = {addEventListener() {}, requestAnimationFrame: undefined};
elements["overview-grid"] = node();
const requests = [];
const posts = [];
let store = [];
let failRead = false;
let failClose = false;
let executeResult = {status: "succeeded", current_status: "succeeded", reason_code: null};
let ruleResult = null;
const action = (id, kind, reasonText) => ({id, action: kind, mode: "confirm", mode_reason: reasonText, status: "pending_confirm", rule_id: "no-ads",
  rule_version: 2, duration_seconds: kind === "mute" ? 600 : null, expires_at: "2099-01-01T00:00:00Z", message_time: null,
  action_index: 0, reason_code: null, created_at: "2025-01-01T00:00:00Z", detail: JSON.stringify({proposal: {reason: "重复推广"}})});
function seed() {
  store = [
    {platform_id: "p", group_id: "g", message_id: "m1", content: "<b>spam</b>", content_truncated: false, cursor: 30, execution_allowed: true,
      actions: [action(11, "mute", "rule"), action(12, "recall", "peer_tie")],
      checks: [{kind: "intent", intent_id: 21, action: "mute", status: "unknown", rule_id: "no-ads", reason_code: null, updated_at: "2025-01-01T00:00:00Z", event_id: null},
        {kind: "event", event_id: 31, status: "review", review_reason: "chat_context_unavailable", created_at: "2025-01-01T00:00:00Z"}]},
    {platform_id: "p", group_id: "other", message_id: "m2", content: null, content_truncated: false, cursor: 20, execution_allowed: false,
      actions: [action(13, "reply", "rule")], checks: []},
  ];
}
function snapshot(params) {
  if (failRead) throw new Error("unavailable");
  const rows = store.filter((item) => params.before_id === undefined || item.cursor < params.before_id);
  const limit = params.limit || 20;
  return structuredClone({messages: rows.slice(0, limit), total_messages: store.length,
    total_actions: store.reduce((sum, item) => sum + item.actions.length, 0),
    total_checks: store.reduce((sum, item) => sum + item.checks.length, 0),
    oldest_at: store.length ? "2025-01-01T00:00:00Z" : null,
    next_before_id: rows.length > limit ? rows[limit - 1].cursor : null});
}
function clean() { store = store.filter((item) => item.actions.length || item.checks.length); }
globalThis.window.AstrBotPluginView = {
  async ready() {},
  async apiGet(endpoint, params) {
    requests.push({endpoint, params});
    if (endpoint === "overview-layout") return {cards: []};
    if (endpoint === "pending-reviews") return snapshot(params);
    throw new Error("unexpected endpoint");
  },
  async apiPost(endpoint, body) {
    posts.push({endpoint, body});
    if (endpoint === "execute-action") {
      for (const item of store) item.actions = item.actions.filter((entry) => entry.id !== body.intent_id);
      clean();
      return executeResult;
    }
    if (endpoint === "execute-rule") {
      const target = store.find((item) => item.platform_id === body.platform_id && item.group_id === body.group_id
        && item.message_id === body.message_id);
      if (!target) throw new Error("missing message");
      const ids = target.actions.filter((entry) => entry.rule_id === body.rule_id).map((entry) => entry.id);
      target.actions = target.actions.filter((entry) => entry.rule_id !== body.rule_id);
      clean();
      if (ruleResult) { const result = ruleResult; ruleResult = null; return result; }
      return {actions: ids.map((id) => ({intent_id: id, status: "succeeded"})), stopped: false, remaining: 0};
    }
    if (endpoint === "review-close") {
      if (failClose) throw new Error("unavailable");
      let closed = 0;
      for (const item of store) {
        const keep = item.actions.filter((entry) => body.target === "rule_actions"
          ? !(entry.rule_id === body.rule_id && item.message_id === body.message_id)
          : !(body.target === "action" && entry.id === body.intent_id));
        closed += item.actions.length - keep.length;
        item.actions = keep;
        item.checks = item.checks.filter((entry) => !((body.target === "intent_check" && entry.intent_id === body.intent_id)
          || (body.target === "event_check" && entry.event_id === body.event_id)));
      }
      clean();
      if (body.target === "rule_actions") return {closed, already_closed: 0, closed_by: "admin", closed_at: "2026-01-01T00:00:00Z"};
      return {closed: true, already_closed: false, closed_by: "admin", closed_at: "2026-01-01T00:00:00Z"};
    }
    throw new Error("unexpected post");
  },
};
const source = await readFile(process.argv[2], "utf8");
await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));
const overview = window.SentinelOverview;
const feedback = [];
overview.setFeedback((message, tone) => feedback.push([message, tone]));
let jumped = null;
overview.setJump((target) => { jumped = target; });
const badge = elements["tab-status-badge"];
const tab = elements["tab-status"];
const pendingRequests = () => requests.filter((request) => request.endpoint === "pending-reviews");
const button = (key) => elements["overview-pending-list"].querySelectorAll("button[data-focus-key]").find((item) => item.dataset.focusKey === key);
const settle = () => new Promise((resolve) => setImmediate(resolve));

// Nav badge: states while the overview tab is not active.
store = [];
overview.setActive(false);
await settle();
assert.deepEqual(pendingRequests().at(-1).params, {all_groups: "true", limit: 1});
assert.equal(badge.hidden, true);
assert.equal(tab.getAttribute("aria-label"), null);
const fakeStore = store;
for (const [total, label, aria] of [[3, "3", "总览, 3 项待复核"], [99, "99", "总览, 99 项待复核"], [100, "99+", "总览, 100 项待复核"], [0, "", null]]) {
  store = Array.from({length: total}, (_, index) => ({platform_id: "p", group_id: "g", message_id: `x${index}`, content: null,
    content_truncated: false, cursor: 1000 - index, execution_allowed: true, actions: [action(1000 + index, "mute", "rule")], checks: []}));
  await overview.refresh();
  assert.equal(badge.hidden, total === 0);
  assert.equal(badge.textContent, label);
  assert.equal(tab.getAttribute("aria-label"), aria);
  assert.deepEqual(pendingRequests().at(-1).params, {all_groups: "true", limit: 1});
}
assert.equal(elements["overview-pending-list"].children.length, 0);
store = fakeStore;
seed();
await overview.refresh();
assert.equal(badge.textContent, "2");
failRead = true;
await overview.refresh();
assert.equal(badge.hidden, true);
assert.equal(badge.textContent, "");
assert.equal(tab.getAttribute("aria-label"), "总览, 待复核数量不可用");
failRead = false;
await overview.refresh();
assert.equal(badge.textContent, "2");
assert.equal(tab.getAttribute("aria-label"), "总览, 2 项待复核");

// Debounced events refresh the count without a list.
const beforeEvents = pendingRequests().length;
overview.onEvent();
overview.onEvent();
await new Promise((resolve) => setTimeout(resolve, 450));
assert.equal(pendingRequests().length, beforeEvents + 1);

// Review card.
overview.setGroups([{platform_id: "p", group_id: "g", name: "测试群"}, {platform_id: "p", group_id: "other", name: "另一个群"}]);
overview.setActive(true);
await settle();
await settle();
assert.deepEqual(pendingRequests().at(-1).params, {all_groups: "true", limit: 5});
assert.equal(elements["overview-pending-list"].children.length, 2);
assert.equal(elements["overview-pending-count"].textContent, "2 条消息待复核 (3 个可确认动作, 2 项需核查)");
assert.match(elements["overview-pending-oldest"].textContent, /最早的未处理事项已等待/);
assert.equal(elements["overview-pending-oldest"].hidden, false);
assert.equal(badge.textContent, "2");
const [first, second] = elements["overview-pending-list"].children;
assert.match(first.textContent, /测试群/);
assert.match(first.textContent, /2 个动作, 2 项核查/);
assert.match(first.textContent, /确认原因: 规则设置为人工确认/);
assert.match(first.textContent, /确认原因: 同级规则命中, 统一人工确认/);
assert.match(first.textContent, /判定需人工复核/);
assert.match(first.textContent, /上下文快照不可用/);
assert.ok(!first.textContent.includes("chat_context_unavailable"));
assert.match(first.textContent, /消息原文: <b>spam<\/b>/);
assert.doesNotMatch(first.textContent, /110|倒计时|可撤回时间/);
assert.equal(button("a:11:execute").disabled, false);
assert.equal(button("a:12:execute").disabled, false);
assert.equal(button("a:13:execute").disabled, true);
assert.equal(button("a:13:discard").disabled, false);
assert.match(second.textContent, /当前不允许真实执行/);
assert.equal(button("i:21:mark").disabled, false);
assert.equal(button("e:31:mark").disabled, false);
assert.ok(!button("i:21:execute"));
await button(`jump:${JSON.stringify(["p", "g", "m1"])}`).trigger("click");
assert.deepEqual(jumped, {platform_id: "p", group_id: "g", message_id: "m1"});

// Confirming one action only posts that action.
await button("a:11:execute").trigger("click");
assert.deepEqual(posts, [{endpoint: "execute-action", body: {intent_id: 11}}]);
assert.deepEqual(feedback.at(-1), ["禁言已执行.", "success"]);
assert.equal(button("a:11:execute"), undefined);
assert.ok(button("a:12:execute"));

// Unknown results never offer a success or retry shortcut.
executeResult = {status: "unknown", current_status: "unknown", reason_code: null};
await button("a:12:execute").trigger("click");
assert.deepEqual(feedback.at(-1), ["执行结果不明, 请人工核查.", "warning"]);
assert.deepEqual(posts.at(-1), {endpoint: "execute-action", body: {intent_id: 12}});
executeResult = {status: "succeeded", current_status: "succeeded", reason_code: null};
assert.equal(elements["overview-pending-count"].textContent, "2 条消息待复核 (1 个可确认动作, 2 项需核查)");

// Closing needs a confirmation step that states it is irreversible and never calls the platform.
const postCount = posts.length;
await button("a:13:discard").trigger("click");
assert.equal(posts.length, postCount);
assert.match(elements["overview-pending-list"].textContent, /无法恢复/);
assert.match(elements["overview-pending-list"].textContent, /不会调用平台/);
assert.ok(button("a:13:confirm"));
await button("a:13:cancel").trigger("click");
assert.equal(posts.length, postCount);
assert.ok(button("a:13:discard"));
await button("a:13:discard").trigger("click");
failClose = true;
await button("a:13:confirm").trigger("click");
assert.equal(feedback.at(-1)[1], "error");
assert.equal(store.length, 2);
failClose = false;
await button("a:13:confirm").trigger("click");
assert.deepEqual(posts.at(-1), {endpoint: "review-close", body: {target: "action", intent_id: 13}});
assert.equal(feedback.at(-1)[0], "已放弃执行, 未调用平台.");
assert.equal(elements["overview-pending-list"].children.length, 1);
assert.equal(badge.textContent, "1");

// Checks are marked done without executing; a message disappears only when everything is closed.
await button("i:21:mark").trigger("click");
assert.match(elements["overview-pending-list"].textContent, /无法恢复/);
await button("i:21:confirm").trigger("click");
assert.deepEqual(posts.at(-1), {endpoint: "review-close", body: {target: "intent_check", intent_id: 21}});
assert.equal(elements["overview-pending-list"].children.length, 1);
await button("e:31:mark").trigger("click");
await button("e:31:confirm").trigger("click");
assert.deepEqual(posts.at(-1), {endpoint: "review-close", body: {target: "event_check", event_id: 31}});
assert.equal(elements["overview-pending-list"].children.length, 0);
assert.equal(elements["overview-pending-empty"].hidden, false);
assert.equal(elements["overview-pending-count"].hidden, true);
assert.equal(badge.hidden, true);
assert.equal(posts.filter((entry) => entry.endpoint === "execute-action").length, 2);

// Rule-level confirmation groups actions under their rule and can confirm or give up all of them.
seed();
await overview.refresh();
const bulkKey = `r:${JSON.stringify(["p", "g", "m1"])}|no-ads`;
assert.match(elements["overview-pending-list"].textContent, /规则: no-ads/);
assert.match(elements["overview-pending-list"].textContent, /2 项待确认/);
assert.match(button(`${bulkKey}:execute`).textContent, /确认本规则全部 \(2 项\)/);
assert.equal(button(`${bulkKey}:execute`).disabled, false);
assert.equal(button(`${bulkKey}:discard`).disabled, false);
assert.equal(button(`${bulkKey}:execute`).getAttribute("aria-label"), "确认本规则全部动作, 2 项, 规则 no-ads, 测试群");
assert.equal(button(`${bulkKey}:discard`).getAttribute("aria-label"), "放弃本规则剩余动作, 2 项, 规则 no-ads, 测试群");
// A rule with one pending action keeps the rule-level controls; a blocked message only allows giving up.
const otherKey = `r:${JSON.stringify(["p", "other", "m2"])}|no-ads`;
assert.match(elements["overview-pending-list"].textContent, /1 项待确认/);
assert.match(button(`${otherKey}:execute`).textContent, /确认本规则全部 \(1 项\)/);
assert.equal(button(`${otherKey}:execute`).disabled, true);
assert.equal(button(`${otherKey}:execute`).title, "当前不允许真实执行");
assert.equal(button(`${otherKey}:discard`).disabled, false);
// Giving up a blocked rule still goes through the irreversible confirmation and never calls the platform.
const postsBeforeBlocked = posts.length;
await button(`${otherKey}:discard`).trigger("click");
assert.equal(posts.length, postsBeforeBlocked);
assert.ok(button(`${otherKey}:confirm`));
await button(`${otherKey}:cancel`).trigger("click");
assert.equal(posts.length, postsBeforeBlocked);
const beforeBulk = posts.length;
await button(`${bulkKey}:discard`).trigger("click");
assert.equal(posts.length, beforeBulk);
assert.match(elements["overview-pending-list"].textContent, /放弃后该规则的这些动作不会执行/);
assert.ok(button(`${bulkKey}:confirm`));
await button(`${bulkKey}:cancel`).trigger("click");
assert.equal(posts.length, beforeBulk);
await button(`${bulkKey}:execute`).trigger("click");
assert.deepEqual(posts.at(-1), {endpoint: "execute-rule",
  body: {platform_id: "p", group_id: "g", message_id: "m1", rule_id: "no-ads"}});
assert.deepEqual(feedback.at(-1), ["本规则的 2 项动作已执行.", "success"]);
assert.equal(button(`${bulkKey}:execute`), undefined);

seed();
await overview.refresh();
ruleResult = {actions: [{intent_id: 11, status: "review"}], stopped: true, remaining: 1};
await button(`${bulkKey}:execute`).trigger("click");
assert.deepEqual(feedback.at(-1), ["已执行 0 项, 其余需复核, 请在判定记录中查看.", "warning"]);
assert.ok(!posts.some((entry) => entry.endpoint === "execute-rule" && entry.body.intent_id !== undefined));

seed();
await overview.refresh();
await button(`${bulkKey}:discard`).trigger("click");
await button(`${bulkKey}:confirm`).trigger("click");
assert.deepEqual(posts.at(-1), {endpoint: "review-close",
  body: {target: "rule_actions", platform_id: "p", group_id: "g", message_id: "m1", rule_id: "no-ads"}});
assert.equal(feedback.at(-1)[0], "已放弃该规则的剩余动作, 未调用平台.");

// Read failure shows an error state, not stale numbers.
seed();
await overview.refresh();
assert.equal(elements["overview-pending-list"].children.length, 2);
assert.equal(badge.textContent, "2");
failRead = true;
await overview.refresh();
assert.equal(elements["overview-pending-count"].textContent, "待复核列表不可用.");
assert.equal(elements["overview-pending-list"].children.length, 0);
assert.match(elements["overview-pending-status"].textContent, /无法读取待复核事项/);
assert.equal(badge.hidden, true);
assert.equal(elements["overview-pending-empty"].hidden, true);
failRead = false;
await overview.refresh();
assert.equal(elements["overview-pending-list"].children.length, 2);
assert.equal(elements["overview-pending-status"].textContent, "");

// Pagination keeps one item per message.
store = Array.from({length: 12}, (_, index) => ({platform_id: "p", group_id: "g", message_id: `p${index}`, content: "x",
  content_truncated: false, cursor: 500 - index, execution_allowed: true, actions: [action(2000 + index, "mute", "rule"), action(3000 + index, "recall", "rule")], checks: []}));
await overview.refresh();
assert.equal(elements["overview-pending-list"].children.length, 5);
assert.equal(elements["overview-pending-more"].hidden, false);
await elements["overview-pending-more"].trigger("click");
assert.equal(elements["overview-pending-list"].children.length, 10);
assert.equal(badge.textContent, "12");
await elements["overview-pending-more"].trigger("click");
assert.equal(elements["overview-pending-list"].children.length, 12);
assert.equal(elements["overview-pending-more"].hidden, true);
assert.equal(elements["overview-pending-count"].textContent, "12 条消息待复核 (24 个可确认动作, 0 项需核查)");

// Leaving the overview keeps the badge fresh without rendering the list.
overview.setActive(false);
await settle();
const listBefore = elements["overview-pending-list"].children.length;
store = store.slice(0, 3);
await overview.refresh();
assert.equal(badge.textContent, "3");
assert.deepEqual(pendingRequests().at(-1).params, {all_groups: "true", limit: 1});
assert.equal(elements["overview-pending-list"].children.length, listBefore);
// The overview keeps a periodic fallback timer alive, so exit explicitly.
process.exit(0);
