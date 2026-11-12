import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import vm from "node:vm";

let now = 0, next = 0;
const timers = new Map();
Date.now = () => now;
globalThis.setTimeout = (fn, delay) => { const id = ++next; timers.set(id, {fn, time: now + delay}); return id; };
globalThis.clearTimeout = (id) => timers.delete(id);
async function tick(duration) {
  const end = now + duration;
  while (true) {
    const selected = [...timers].filter(([, t]) => t.time <= end).sort((a, b) => a[1].time - b[1].time)[0];
    if (!selected) break;
    timers.delete(selected[0]); now = selected[1].time;
    selected[1].fn();
    for (let i = 0; i < 20; i++) await Promise.resolve();
  }
  now = end;
}
function node() {
  let text = "";
  return {value: "", checked: false, children: [], handlers: {}, attributes: {}, hidden: false,
    set textContent(value) { text = value; this.children = []; }, get textContent() { return text; },
    appendChild(child) { child.parent = this; this.children.push(child); },
    insertBefore(child, anchor) { child.parent = this; const i = anchor ? this.children.indexOf(anchor) : this.children.length; this.children.splice(i, 0, child); },
    replaceWith(child) { const i = this.parent.children.indexOf(this); child.parent = this.parent; this.parent.children[i] = child; },
    remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); },
    setAttribute(k, v) { this.attributes[k] = v; }, addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
  };
}
const elements = {}, listeners = {}, requests = [], subscriptions = [], statusHistory = [];
globalThis.document = {hidden: false, documentElement: node(), createElement: node,
  getElementById(id) {
    if (!elements[id]) {
      elements[id] = node();
      if (id === "live-status") {
        let text = "";
        Object.defineProperty(elements[id], "textContent", {get() { return text; }, set(value) { text = value; statusHistory.push(value); }});
      }
    }
    return elements[id];
  }, addEventListener(k, fn) { (listeners[k] ||= []).push(fn); }};
let messageHandler;
const post = (data) => messageHandler({source: parent, origin: "http://local", data: {channel: "astrbot-plugin-page", ...data}});
const row = {id: 1, message_id: "m", status: "started", detail: "{}", events: []};
const parent = {postMessage(message) {
  if (message.kind !== "request") return;
  requests.push(message);
  queueMicrotask(() => {
    if (message.action === "sse:subscribe") {
      subscriptions.push(message);
      post({kind: "response", requestId: message.requestId, ok: true, data: null});
      post({kind: "sse_state", subscriptionId: message.subscriptionId, state: "open"});
      post({kind: "sse_message", subscriptionId: message.subscriptionId, eventType: "ping", data: "{}"});
    } else if (message.action === "sse:unsubscribe") {
      post({kind: "response", requestId: message.requestId, ok: true});
    } else if (message.action === "api:get") {
      const data = {status: {monitoring: "disabled"}, "ui-state": {platform_id: "p", group_id: "g", active_tab: "records"},
        platforms: [], groups: [], rules: [], messages: [row], providers: [],
        settings: {monitoring_groups: [], monitoring_mode: "allowlist"},
        summary: {total_messages: 1, status_counts: {}, recent_24h: {messages: 1, proposed: 0, no_action: 0}}}[message.endpoint];
      post({kind: "response", requestId: message.requestId, ok: true, data});
    } else post({kind: "response", requestId: message.requestId, ok: true, data: {}});
  });
}};
globalThis.window = {location: {origin: "http://local"}, parent,
  addEventListener(kind, fn) { if (kind === "message") messageHandler = fn; else (listeners[kind] ||= []).push(fn); }};
function dispatch(kind) { for (const listener of listeners[kind] || []) listener(); }
vm.runInThisContext(await readFile(process.argv[3], "utf8"));
post({kind: "context", context: {}});
const source = await readFile(process.argv[2]);
await import("data:text/javascript;base64," + source.toString("base64"));
for (let i = 0; i < 20; i++) await Promise.resolve();
assert.deepEqual(subscriptions[0].params, {platform_id: "p", group_id: "g"});
assert.equal(elements["live-status"].textContent, "实时 · 已连接");
assert.equal(elements["live-status"].attributes["data-tone"], "success");
const before = elements["records-body"].children[0];
const messageRequests = requests.filter((r) => r.endpoint === "messages").length;
post({kind: "sse_message", subscriptionId: subscriptions[0].subscriptionId, eventType: "message",
  data: JSON.stringify({...row, id: 2, status: "failed"})});
assert.notEqual(elements["records-body"].children[0], before);
assert.equal(elements["records-body"].children.length, 2);
assert.equal(elements["records-body"].children[0].children[1].children[0].textContent, "失败");
assert.equal(requests.filter((r) => r.endpoint === "messages").length, messageRequests);
post({kind: "sse_message", subscriptionId: subscriptions[0].subscriptionId, eventType: "message",
  data: JSON.stringify({...row, id: 3, message_id: "new", status: "started"})});
assert.equal(elements["records-body"].children.length, 4);
assert.equal(elements["records-body"].children[0].attributes["aria-label"], "查看消息 new 的详情");
assert.equal(elements["records-body"].children[2].attributes["aria-label"], "查看消息 m 的详情");
const unchanged = elements["records-body"].children[0];
elements["records-body"].children[2].handlers.click[0]();
assert.equal(elements["records-body"].children[3].hidden, false);
post({kind: "sse_message", subscriptionId: subscriptions[0].subscriptionId, eventType: "message",
  data: JSON.stringify({...row, id: 4, status: "no_action"})});
assert.equal(elements["records-body"].children[0].attributes["aria-label"], "查看消息 m 的详情");
assert.equal(elements["records-body"].children[1].hidden, false);
assert.equal(elements["records-body"].children[2], unchanged);
post({kind: "sse_state", subscriptionId: subscriptions[0].subscriptionId, state: "closed"});
await tick(50000);
assert.equal(subscriptions.length, 2);
assert.ok(statusHistory.slice(1).includes("实时 · 重连中"));
assert.equal(elements["live-status"].textContent, "实时 · 已连接");
post({kind: "sse_state", subscriptionId: subscriptions[1].subscriptionId, state: "closed"});
await tick(50000);
assert.equal(elements["live-status"].textContent, "已断开, 使用轮询");
assert.equal(elements["live-status"].attributes["data-tone"], "warning");
const prior = requests.filter((r) => r.endpoint === "messages").length;
await tick(5000);
assert.ok(requests.filter((r) => r.endpoint === "messages").length > prior);
document.hidden = true;
dispatch("visibilitychange");
const hiddenRequests = requests.filter((r) => r.endpoint === "messages").length;
await tick(10000);
assert.equal(requests.filter((r) => r.endpoint === "messages").length, hiddenRequests);
document.hidden = false;
dispatch("visibilitychange");
for (let i = 0; i < 20; i++) await Promise.resolve();
assert.equal(subscriptions.length, 3);
dispatch("pagehide");
