import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";

const ids = ["platform", "group", "rule", "version", "priority", "text", "mode", "mapping", "enabled",
  "confirm", "preview-output", "save-status", "status", "platforms", "load", "history", "output", "audit", "audit-next",
  "overflow", "notifications", "preview", "records-next", "records-status", "records-body", "current-rules",
  "platform-select", "group-select", "selection-status", "refresh-group"];
const elements = Object.fromEntries(ids.map((id) => [id, {
  value: "", checked: false, disabled: false, textContent: "", handlers: {},
  addEventListener(type, handler) { this.handlers[type] = handler; }, setAttribute() {},
  set innerHTML(value) { throw new Error("untrusted HTML must not be rendered"); },
}]));
const latestFeedback = () => elements["feedback-region"].children[0];
function makeNode(tag) {
  const classes = new Set();
  return {tag, children: [], handlers: {}, attributes: {}, hidden: false, textContent: "",
    style: {setProperty(name, value) { this[name] = value; }, getPropertyValue(name) { return this[name] || ""; }},
    classList: {add(value) { classes.add(value); }, remove(value) { classes.delete(value); }, contains(value) { return classes.has(value); }},
    get parentNode() { return this.parent || null; },
    get nextElementSibling() { return this.parent?.children[this.parent.children.indexOf(this) + 1] || null; },
    appendChild(child) {
      if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
      child.parent = this; this.children.push(child);
    },
    insertBefore(child, anchor) {
      if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
      child.parent = this;
      const index = anchor ? this.children.indexOf(anchor) : this.children.length;
      this.children.splice(index, 0, child);
    },
    remove() { if (this.parent) { this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; } },
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(type, handler) { this.handlers[type] = handler; },
    focus(options) { this.focusOptions = options; },
    scrollIntoView(options) { this.scrollOptions = options; },
    getBoundingClientRect() { const top = (this.parent?.children.indexOf(this) || 0) * 150; return {top, bottom: top + 120, height: 120}; },
    animate(frames, options) {
      const animation = {frames, options, currentTime: 0, paused: false, cancelled: false,
        pause() { this.paused = true; }, cancel() { this.cancelled = true; }};
      (this.animations ||= []).push(animation);
      return animation;
    },
    setPointerCapture(pointerId) { this.capturedPointer = pointerId; },
    hasPointerCapture(pointerId) { return this.capturedPointer === pointerId; },
    releasePointerCapture() { this.capturedPointer = null; },
    set innerHTML(value) { throw new Error("untrusted HTML must not be rendered"); },
  };
}
elements["records-body"].children = [];
elements["records-body"].appendChild = function(child) { child.parent = this; this.children.push(child); };
elements["records-body"].insertBefore = function(child, anchor) {
  child.parent = this;
  const index = anchor ? this.children.indexOf(anchor) : this.children.length;
  this.children.splice(index, 0, child);
};
for (const id of ["platform-select", "group-select"]) elements[id].appendChild = function() {};
const documentHandlers = {};
globalThis.document = {hidden: false, body: makeNode("body"),
  addEventListener(type, handler) { (documentHandlers[type] ||= []).push(handler); },
  getElementById(id) { return elements[id] ||= makeNode("div"); }, createElement: makeNode};
const requests = [];
const delayed = {};
let stream;
let malformedPreview = false;
let previewDelay = null;
let failPreview = false;
let initializing = true;
let immediateRuleLoad = false;
let delayConfirm = false;
let actionStatus = "succeeded";
let reduceMotion = false;
const windowHandlers = {};
globalThis.window = {innerWidth: 800, innerHeight: 600, scrollY: 0,
  addEventListener(type, handler) { (windowHandlers[type] ||= []).push(handler); },
  matchMedia(query) { return {matches: query.includes("reduced-motion") && reduceMotion, addEventListener() {}}; },
  requestAnimationFrame() { return 1; }, cancelAnimationFrame() {}, scrollBy() {}, AstrBotPluginView: {
  async subscribeSSE(endpoint, callbacks) { stream = callbacks; return "test"; }, async unsubscribeSSE() {},
  async ready() {},
  async apiGet(endpoint, params) {
    requests.push({endpoint, params});
    if (endpoint === "status") return {monitoring: "disabled_unverified_event_hook"};
    if (endpoint === "settings") return {quick_start_completed: false, monitoring_groups: [], monitoring_mode: "allowlist", default_action_mode: "auto", llm_no_action_reason_enabled: true};
    if (endpoint === "providers") return [];
    if (endpoint === "ui-state") return null;
    if (endpoint === "summary") return {total_messages: 0, status_counts: {}, recent_24h: {messages: 0, proposed: 0, no_action: 0}, latency_p95_ms: null};
    if (endpoint === "platforms") return [];
    if (endpoint === "groups") return [{platform_id: "p", group_id: "g"},
      {platform_id: "p", group_id: "other"}];
    if (endpoint === "rules/history") return [{version: 1, text: "<img src=x onerror=alert(1)>"}];
    if (endpoint === "messages" && params.all_groups === "true") return [];
    if (endpoint === "rules" && (initializing || immediateRuleLoad)) { immediateRuleLoad = false; return []; }
    if (endpoint === "audit" || endpoint === "messages" || endpoint === "rules") return await new Promise((resolve, reject) => {
      delayed[endpoint] = {resolve, reject};
    });
    throw new Error("unexpected request");
  },
  async apiPost(endpoint, body) {
    requests.push({endpoint, body});
    if (endpoint === "rules/preview") {
      if (previewDelay) await previewDelay;
      if (failPreview) throw new Error("preview unavailable");
      return {fields: malformedPreview ? {platform_id: "p"} : body, expires: 1, signature: "synthetic"};
    }
    if (endpoint === "execute-action") return {status: actionStatus};
    if (endpoint === "ui-state") return body;
    if (endpoint === "rules/confirm") {
      if (!Array.isArray(body?.fields?.actions)) throw new Error("invalid preview");
      if (!delayConfirm) { immediateRuleLoad = true; return {version: 1}; }
      return await new Promise((resolve, reject) => { delayed.confirm = {resolve, reject}; });
    }
    throw new Error("unexpected post");
  },
}};
const source = await readFile(process.argv[2]);
await import("data:text/javascript;base64," + source.toString("base64"));
initializing = false;
assert.equal(elements["new-rule"].disabled, false);
assert.equal(elements["records-empty"].hidden, false);
assert.match(elements["records-empty"].textContent, /还没有判定记录/);
elements["group-select"].value = JSON.stringify(["p", "g"]);
const initialSelection = elements["group-select"].handlers.change();
delayed.rules.resolve([]);
delayed.messages.resolve([]);
await initialSelection;
elements.rule.value = " r ";
elements["audit-next"].disabled = false;
await elements.history.handlers.click();
assert.deepEqual(requests.at(-1), {endpoint: "rules/history", params: {platform_id: "p", group_id: "g", rule_id: "r"}});
assert.equal(elements["audit-next"].disabled, true);
assert.equal(JSON.parse(elements.output.textContent).history[0].text, "<img src=x onerror=alert(1)>");
const count = requests.length;
elements.rule.value = " ";
await elements.history.handlers.click();
assert.equal(requests.length, count);
assert.match(elements.output.textContent, /操作失败/);
elements.rule.value = "r";
const oldAudit = elements.audit.handlers.click();
await elements.history.handlers.click();
const historyOutput = elements.output.textContent;
delayed.audit.resolve(Array.from({length: 50}, (_, i) => ({id: i + 1})));
await oldAudit;
assert.equal(elements.output.textContent, historyOutput);
assert.equal(elements["audit-next"].disabled, true);
const oldRules = elements.load.handlers.click();
const staleRules = delayed.rules;
elements["group-select"].value = JSON.stringify(["p", "other"]);
const nextSelection = elements["group-select"].handlers.change();
staleRules.resolve([{rule_id: "stale"}]);
await oldRules;
delayed.rules.resolve([]);
delayed.messages.resolve([]);
await nextSelection;
assert.equal(elements.output.textContent, "");
assert.equal(elements["audit-next"].disabled, true);
const failedRules = elements.load.handlers.click();
await elements.history.handlers.click();
const currentOutput = elements.output.textContent;
delayed.rules.reject(new Error("old request failed"));
await failedRules;
assert.equal(elements.output.textContent, currentOutput);
elements.version.value = "0";
elements.priority.value = "0";
elements.text.value = "first rule";
elements["add-action"].handlers.click();
elements["add-action"].handlers.click();
const reply = elements["action-list"].children[1];
const firstAction = elements["action-list"].children[0];
assert.deepEqual(elements["action-list"].children.map((item) => item.children[0].children[0].children[1].textContent), ["1", "2"]);
assert.equal(reply.children[0].children[0].children[0].textContent, "动作");
assert.equal(reply.style["--action-hue"], firstAction.style["--action-hue"]);
assert.equal(firstAction.children[1].value, "");
assert.equal(firstAction.children[2].value, "inherit");
assert.equal(firstAction.children[2].children[0].textContent, "全局(自动)");
await elements.preview.handlers.click();
assert.match(latestFeedback().children[0].textContent, /请选择动作类型/);
firstAction.children[1].value = "mute";
firstAction.children[1].handlers.change();
assert.equal(firstAction.children[5].children[1].children.length, 0);
firstAction.children[5].children[2].handlers.click();
const level = firstAction.children[5].children[1].children[0].children[0].children[0];
level.value = "low";
level.handlers.change();
firstAction.children[5].children[1].children[0].children[1].children[0].value = "60";
const initialMuteHue = firstAction.style["--action-hue"];
level.value = "high";
level.handlers.input();
assert.equal(firstAction.style["--action-hue"], initialMuteHue);
level.handlers.change();
assert.notEqual(firstAction.style["--action-hue"], initialMuteHue);
level.value = "low";
level.handlers.change();
assert.equal(firstAction.style["--action-hue"], initialMuteHue);
reply.children[1].value = "reply";
reply.children[1].handlers.change();
assert.notEqual(reply.style["--action-hue"], initialMuteHue);
const automaticReplyHue = reply.style["--action-hue"];
reply.children[2].value = "confirm";
reply.children[2].handlers.change();
const initialReplyHue = reply.style["--action-hue"];
assert.notEqual(initialReplyHue, automaticReplyHue);
reply.children[3].children[0].value = "{sender_id} / {rule_id}";
reply.children[3].children[0].handlers.input();
assert.equal(reply.style["--action-hue"], initialReplyHue);
reply.children[3].children[0].handlers.change();
const replyHue = reply.style["--action-hue"];
assert.notEqual(replyHue, initialReplyHue);
  await elements.preview.handlers.click();
  assert.deepEqual(requests.findLast((request) => request.endpoint === "rules/preview").body.actions[1], {type: "reply", mode: "confirm", template: "{sender_id} / {rule_id}"});
  const handle = reply.children[0].children[1];
  const pointer = (clientY) => ({pointerId: 7, pointerType: "mouse", isPrimary: true, button: 0,
    clientX: 100, clientY, preventDefault() {}});
  handle.handlers.pointerdown(pointer(180));
  windowHandlers.pointermove[0](pointer(200));
  assert.equal(elements["action-list"].children.length, 2);
  assert.equal(document.body.children[0].className, "action-drag-preview");
  windowHandlers.pointermove[0](pointer(10));
  assert.equal(elements["action-list"].children[0].className, "action-drop-placeholder");
  assert.equal(elements["action-list"].children[0].style["--action-hue"], replyHue);
  windowHandlers.pointermove[0](pointer(250));
  assert.equal(elements["action-list"].children.length, 2);
  windowHandlers.pointerup[0](pointer(250));
  assert.equal(elements["action-list"].children[1], reply);
  assert.equal(elements.preview.disabled, false);
  handle.handlers.pointerdown(pointer(180));
  windowHandlers.pointermove[0](pointer(10));
  assert.equal(elements["action-list"].children[0].className, "action-drop-placeholder");
  assert.equal(document.body.children[0].className, "action-drag-preview");
  windowHandlers.keydown[0]({key: "Escape", preventDefault() {}});
  assert.equal(elements["action-list"].children[1], reply);
  assert.equal(elements.preview.disabled, false);
  assert.equal(document.body.children.length, 0);
  assert.equal(elements["action-order-status"].textContent, "已取消排序.");
  for (const cancel of [
    () => windowHandlers.pointercancel[0](pointer(10)),
    () => windowHandlers.pagehide.at(-1)(),
    () => { document.hidden = true; documentHandlers.visibilitychange.at(-1)(); document.hidden = false; },
  ]) {
    handle.handlers.pointerdown(pointer(180));
    windowHandlers.pointermove[0](pointer(10));
    cancel();
    assert.equal(elements["action-list"].children[1], reply);
    assert.equal(document.body.children.length, 0);
    assert.equal(elements.preview.disabled, false);
  }
  handle.handlers.pointerdown(pointer(180));
  windowHandlers.pointermove[0](pointer(10));
  windowHandlers.pointerup[0](pointer(10));
  assert.equal(elements["action-list"].children[0], reply);
  assert.equal(reply.style["--action-hue"], replyHue);
  assert.equal(reply.children[0].children[0].children[1].textContent, "1");
  assert.equal(elements.preview.disabled, false);
  assert.deepEqual(handle.focusOptions, {preventScroll: true});
  assert.deepEqual(handle.scrollOptions, {behavior: "smooth", block: "nearest", inline: "nearest"});
  assert.ok(elements["action-list"].children.some((item) => item.animations?.some((animation) => animation.options.duration === 220)));
  let arrowPrevented = false;
  handle.handlers.keydown({key: "ArrowDown", preventDefault() { arrowPrevented = true; }});
  assert.equal(arrowPrevented, true);
  assert.equal(elements["action-list"].children[1], reply);
  assert.equal(reply.children[0].children[0].children[1].textContent, "2");
  await elements.preview.handlers.click();
  const up = reply.children[7];
  up.handlers.click();
  assert.deepEqual(elements["action-list"].children.map((item) => item.children[0].children[0].children[1].textContent), ["1", "2"]);
  assert.equal(reply.children[0].children[0].children[1].textContent, "1");
  assert.equal(up.attributes["aria-disabled"], "true");
  assert.deepEqual(up.focusOptions, {preventScroll: true});
  assert.deepEqual(reply.scrollOptions, {behavior: "smooth", block: "center", inline: "nearest"});
  assert.match(elements["action-order-status"].textContent, /第 1 位/);
  assert.equal(elements.preview.disabled, false);
  await elements.preview.handlers.click();
  assert.deepEqual(requests.findLast((request) => request.endpoint === "rules/preview").body.actions.map((item) => item.type), ["reply", "mute"]);
  reduceMotion = true;
  document.scrollingElement = {scrollHeight: 600};
  document.documentElement = {};
  window.scrollY = 500;
  window.getComputedStyle = () => ({scrollPaddingTop: "0px", scrollPaddingBottom: "0px", scrollMarginTop: "0px", scrollMarginBottom: "0px"});
  reply.children[8].handlers.click();
  assert.equal(reply.children[0].children[0].children[1].textContent, "2");
  assert.deepEqual(reply.scrollOptions, {behavior: "auto", block: "center", inline: "nearest"});
  assert.equal(elements["panel-rules"].style.paddingBottom, "410px");
  delete document.scrollingElement;
  delete window.getComputedStyle;
  window.scrollY = 0;
  reply.getBoundingClientRect = () => ({top: 0, bottom: 700, height: 700});
  up.handlers.click();
  assert.deepEqual(up.scrollOptions, {behavior: "auto", block: "center", inline: "nearest"});
  reply.getBoundingClientRect = () => ({top: (reply.parent?.children.indexOf(reply) || 0) * 150, height: 120});
  reply.children[8].handlers.click();
  reduceMotion = false;
  await elements.preview.handlers.click();
  assert.equal(elements["panel-rules"].style.paddingBottom, "");
  const frames = new Map();
  let nextFrame = 0;
  document.scrollingElement = {scrollHeight: 2000};
  window.scrollY = 0;
  window.getComputedStyle = () => ({scrollPaddingTop: "0px", scrollPaddingBottom: "0px", scrollMarginTop: "0px", scrollMarginBottom: "0px"});
  window.scrollTo = ({top}) => { window.scrollY = top; };
  window.requestAnimationFrame = (callback) => { const id = ++nextFrame; frames.set(id, callback); return id; };
  window.cancelAnimationFrame = (id) => frames.delete(id);
  reply.getBoundingClientRect = () => {
    const top = 900 + reply.parent.children.indexOf(reply) * 150 - window.scrollY;
    return {top, bottom: top + 120, height: 120};
  };
  up.handlers.click();
  assert.equal(window.scrollY, 0);
  const stepFrame = (time) => { const [id, callback] = frames.entries().next().value; frames.delete(id); callback(time); };
  const synchronized = reply.animations.at(-1);
  assert.equal(synchronized.paused, true);
  stepFrame(1000);
  assert.equal(window.scrollY, 0);
  stepFrame(1160);
  assert.equal(window.scrollY, 330);
  assert.equal(synchronized.currentTime, 160);
  stepFrame(1320);
  assert.equal(window.scrollY, 660);
  assert.equal(synchronized.cancelled, true);
  assert.equal(frames.size, 0);
  reply.children[8].handlers.click();
  const interrupted = reply.animations.at(-1);
  assert.equal(frames.size, 1);
  windowHandlers.wheel[0]();
  assert.equal(frames.size, 0);
  assert.equal(interrupted.cancelled, true);
  delete document.scrollingElement;
  delete window.getComputedStyle;
  delete window.scrollTo;
  window.scrollY = 0;
  window.requestAnimationFrame = () => 1;
  window.cancelAnimationFrame = () => {};
  reply.getBoundingClientRect = () => ({top: (reply.parent?.children.indexOf(reply) || 0) * 150, height: 120});
reply.children[6].handlers.click();
assert.equal(elements["action-list"].children.length, 1);
assert.equal(elements["action-list"].children[0].children[0].children[1].disabled, true);
elements["action-list"].children[0].children[1].value = "recall";
elements["action-list"].children[0].children[1].handlers.change();
assert.equal(elements["action-list"].children[0].children[3].hidden, true);
assert.equal(elements["action-list"].children[0].children[4].hidden, true);
assert.equal(elements["action-list"].children[0].children[5].hidden, true);
await elements.preview.handlers.click();
assert.deepEqual(requests.findLast((request) => request.endpoint === "rules/preview").body.actions, [{type: "recall", mode: "inherit"}]);
elements["action-list"].children[0].children[1].value = "mute";
elements["action-list"].children[0].children[1].handlers.change();
await elements.preview.handlers.click();
assert.equal(requests.findLast((request) => request.endpoint === "rules/preview").body.actions.length, 1);
assert.match(latestFeedback().children[0].textContent, /规则已保存/);
const severityEditor = elements["action-list"].children[0].children[5];
severityEditor.children[2].handlers.click();
assert.equal(elements.preview.disabled, false);
const beforeEmptyLevel = requests.length;
await elements.preview.handlers.click();
assert.equal(requests.length, beforeEmptyLevel);
assert.match(latestFeedback().children[0].textContent, /请填写级别名称/);
assert.equal(elements.preview.disabled, false);
const newLevel = severityEditor.children[1].children[1];
newLevel.children[0].children[0].value = "high";
newLevel.children[0].children[0].handlers.input();
newLevel.children[1].children[0].value = "5";
newLevel.children[1].children[0].handlers.input();
newLevel.children[2].children[0].value = "60";
newLevel.children[2].children[0].handlers.change();
await elements.preview.handlers.click();
assert.equal(requests.findLast((request) => request.endpoint === "rules/preview").body.actions[0].severity_seconds.high, 300);
assert.match(latestFeedback().children[0].textContent, /规则已保存/);
await elements.preview.handlers.click();
const recordRequest = elements["refresh-group"].handlers.click();
assert.deepEqual(requests.filter((request) => request.endpoint === "messages").at(-1), {endpoint: "messages", params: {platform_id: "p", group_id: "other", limit: 50}});
delayed.rules.resolve([]);
delayed.messages.resolve([{id: 1, created_at: "2026-11-01", status: "proposed", message_id: "m",
  execution: {id: 8, action: "mute", mode: "confirm", status: "pending_confirm", history: []},
  detail: JSON.stringify({proposal: {rule_id: "r", rule_version: 2, action: "mute", duration_seconds: 60,
    reason: "<img src=x>"}, model_latency_ms: 12})}]);
await recordRequest;
const [record, detailRow] = elements["records-body"].children;
const section = (row, title) => row.children[0].children[0].children.flatMap((item) => item.children).find((item) => item.children[0]?.textContent === title);
const actionItems = (row) => section(row, "处理动作").children[1].children;
const actionButton = (item) => item.children.find((child) => child.tag === "button");
assert.equal(record.children.length, 5);
assert.equal(record.children[3].textContent, "r / 2");
assert.equal(record.children[1].children[0].textContent, "待确认");
assert.equal(record.children[2].textContent, "<img src=x>");
assert.equal(record.children[4].textContent, "12 ms");
assert.equal(detailRow.hidden, true);
record.handlers.click();
assert.equal(detailRow.hidden, false);
assert.equal(record.attributes["aria-expanded"], "true");
assert.equal(section(detailRow, "判定理由").children[1].textContent, "<img src=x>");
assert.equal(section(detailRow, "消息标识").children[1].textContent, "m");
assert.equal(actionItems(detailRow)[0].children[0].children[0].textContent, "1. 禁言 1分钟");
await actionButton(actionItems(detailRow)[0]).handlers.click();
assert.deepEqual(requests.at(-1), {endpoint: "execute-action", body: {intent_id: 8}});
assert.equal(latestFeedback().children[0].textContent, "动作已执行.");
assert.equal(latestFeedback().attributes["data-tone"], "success");
actionStatus = "unknown";
await actionButton(actionItems(detailRow)[0]).handlers.click();
assert.equal(latestFeedback().children[0].textContent, "执行结果不明, 请核查后再操作.");
assert.equal(latestFeedback().attributes["data-tone"], "warning");
actionStatus = "blocked";
await actionButton(actionItems(detailRow)[0]).handlers.click();
assert.equal(latestFeedback().attributes["data-tone"], "error");
actionStatus = "succeeded";
stream.onMessage({parsed: {id: 1, created_at: "2026-11-01", status: "proposed", message_id: "m",
  execution: {id: 8, action: "mute", mode: "confirm", status: "blocked", attempts: 1, reason_code: "protected_target", platform_code: "40103004"},
  detail: JSON.stringify({proposal: {rule_id: "r", rule_version: 2, action: "mute", duration_seconds: 60}})}});
assert.equal(elements["records-body"].children[0].children[1].children[0].textContent, "全部失败");
assert.match(elements["records-body"].children[0].children[1].children[0].title, /目标是管理员或机器人/);
assert.equal(elements["records-body"].children[1].hidden, false);
stream.onMessage({parsed: {id: 1, created_at: "2026-11-01", status: "proposed", message_id: "m",
  execution: {id: 9, action: "recall", mode: "confirm", status: "pending_confirm"},
  detail: JSON.stringify({proposal: {rule_id: "r", rule_version: 2, action: "recall"}})}});
assert.equal(actionButton(actionItems(elements["records-body"].children[1])[0]).textContent, "执行撤回");
await actionButton(actionItems(elements["records-body"].children[1])[0]).handlers.click();
assert.deepEqual(requests.at(-1), {endpoint: "execute-action", body: {intent_id: 9}});
const multiRow = {id: 1, created_at: "2026-11-01", status: "proposed", message_id: "m",
  execution_summary: "pending_confirm", executions: [
    {id: 10, action_index: 0, action: "recall", status: "blocked", reason_code: "protected_target", platform_code: "40062003", trace_id: "trace-a"},
    {id: 11, action_index: 1, action: "reply", mode: "confirm", status: "pending_confirm"},
    {id: 12, action_index: 2, action: "mute", status: "succeeded", duration_seconds: 60}],
  detail: JSON.stringify({proposal: {rule_id: "r", rule_version: 2}})};
stream.onMessage({parsed: multiRow});
assert.equal(elements["records-body"].children[0].children[1].children[0].textContent, "待确认");
const multiDetail = elements["records-body"].children[1];
const multiActions = actionItems(multiDetail);
assert.deepEqual(multiActions.map((item) => item.children[0].children[0].textContent), ["1. 撤回", "2. 发言", "3. 禁言 1分钟"]);
assert.deepEqual(multiActions.map((item) => item.children[0].children[1].textContent), ["执行失败", "待确认", "已执行"]);
assert.match(multiActions[0].children[1].textContent, /目标是管理员或机器人/);
const multiRaw = multiDetail.children[0].children[0].children.find((item) => item.className === "record-raw");
assert.match(multiRaw.children[1].textContent, /trace-a/);
assert.equal(actionButton(multiActions[1]).textContent, "执行发言");
await actionButton(multiActions[1]).handlers.click();
assert.deepEqual(requests.at(-1), {endpoint: "execute-action", body: {intent_id: 11}});
stream.onMessage({parsed: {...multiRow, executions: multiRow.executions.map((item) => item.id === 12
  ? {...item, status: "failed", reason_code: "invalid_message"} : item)}});
const updatedMultiDetail = elements["records-body"].children[1];
assert.equal(updatedMultiDetail.hidden, false);
assert.equal(actionItems(updatedMultiDetail)[2].children[0].children[1].textContent, "执行失败");
assert.match(actionItems(updatedMultiDetail)[2].children[1].textContent, /消息标识无效/);
delayConfirm = true;
const firstSave = elements.preview.handlers.click();
await new Promise((resolve) => setImmediate(resolve));
assert.equal(requests.at(-1).endpoint, "rules/confirm");
assert.equal(requests.at(-1).body.fields.actions[0].severity_seconds.high, 300);
assert.equal(elements.preview.disabled, true);
const beforeDoubleClick = requests.length;
await elements.preview.handlers.click();
assert.equal(requests.length, beforeDoubleClick);
elements.text.value = "second rule";
elements.text.handlers.input();
delayed.confirm.resolve({version: 1, text: "first rule"});
await firstSave;
assert.equal(elements.text.value, "second rule");
assert.equal(elements.preview.disabled, false);
const failedSave = elements.preview.handlers.click();
await new Promise((resolve) => setImmediate(resolve));
delayed.confirm.reject(new Error("synthetic failure"));
await failedSave;
assert.match(latestFeedback().children[0].textContent, /保存失败/);
assert.equal(elements.preview.disabled, false);
delayConfirm = false;
malformedPreview = true;
await elements.preview.handlers.click();
assert.match(latestFeedback().children[0].textContent, /invalid preview/);
malformedPreview = false;
delayConfirm = true;
const versionConflict = elements.preview.handlers.click();
await new Promise((resolve) => setImmediate(resolve));
delayed.confirm.reject(new Error("version conflict"));
await versionConflict;
assert.match(latestFeedback().children[0].textContent, /version conflict/);
assert.equal(elements.preview.disabled, false);
let releasePreview;
previewDelay = new Promise((resolve) => { releasePreview = resolve; });
const confirmsBeforeStale = requests.filter((request) => request.endpoint === "rules/confirm").length;
const stalePreview = elements.preview.handlers.click();
assert.equal(elements.preview.disabled, true);
elements.text.value = "new draft";
elements.text.handlers.input();
releasePreview();
await stalePreview;
previewDelay = null;
assert.equal(elements.preview.disabled, false);
assert.equal(requests.filter((request) => request.endpoint === "rules/confirm").length, confirmsBeforeStale);
failPreview = true;
await elements.preview.handlers.click();
assert.match(latestFeedback().children[0].textContent, /preview unavailable/);
assert.equal(elements.preview.disabled, false);
