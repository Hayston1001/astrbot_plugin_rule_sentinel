import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";

function node() {
  const classes = new Set();
  return {
    children: [], attributes: {}, handlers: {}, style: {}, animations: [], textContent: "", parent: null, hovered: false, focused: false,
    classList: {add(value) { classes.add(value); }, remove(value) { classes.delete(value); }, contains(value) { return classes.has(value); }},
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(name, handler) { this.handlers[name] = handler; },
    animate(frames, options) {
      const animation = {frames, options, state: "running", pause() { this.state = "paused"; }, cancel() { this.state = "cancelled"; }};
      this.animations.push(animation);
      return animation;
    },
    appendChild(child) { child.parent = this; this.children.push(child); },
    insertBefore(child, before) {
      child.parent = this;
      const index = before ? this.children.indexOf(before) : this.children.length;
      this.children.splice(index, 0, child);
    },
    remove() { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; },
    matches(selector) { assert.equal(selector, ":hover, :focus-within"); return this.hovered || this.focused; },
  };
}

let now = 0;
let nextTimer = 0;
const timers = new Map();
function setTimer(callback, delay) {
  const id = ++nextTimer;
  timers.set(id, {callback, at: now + delay});
  return id;
}
function clearTimer(id) { timers.delete(id); }
function advance(duration) {
  const end = now + duration;
  for (;;) {
    const pending = [...timers].filter(([, item]) => item.at <= end).sort((a, b) => a[1].at - b[1].at);
    if (!pending.length) break;
    const [id, item] = pending[0];
    timers.delete(id);
    now = item.at;
    item.callback();
  }
  now = end;
}

const region = node();
const document = {getElementById: () => region, createElement: node, createElementNS: node};
const source = await readFile(process.argv[2], "utf8");
const start = source.indexOf("function showFeedback(");
const end = source.indexOf("\nfunction feedbackError(", start);
assert.ok(start >= 0 && end > start);
let reducedMotion = false;
const makeFeedback = new Function("document", "setTimeout", "clearTimeout", "performance", "prefersReducedMotion",
  `const el = id => document.getElementById(id);\n${source.slice(start, end)}\nreturn showFeedback;`);
const showFeedback = makeFeedback(document, setTimer, clearTimer, {now: () => now}, () => reducedMotion);

showFeedback("first", "success");
const first = region.children[0];
const firstTimer = first.children[1];
const firstSvg = firstTimer.children[0];
const firstTrack = firstSvg.children[0];
const firstArc = firstSvg.children[1];
assert.equal(first.children[0].textContent, "first");
assert.equal(firstSvg.attributes["aria-hidden"], "true");
assert.equal(firstTimer.children[1].children[0].attributes["class"], "feedback-close-icon");
assert.equal(firstTimer.children[1].children[0].attributes["aria-hidden"], "true");
assert.equal(firstSvg.attributes["viewBox"], "0 0 26 26");
assert.equal(firstTrack.attributes["class"], "feedback-countdown-track");
assert.equal(firstArc.attributes["class"], "feedback-countdown-arc");
assert.equal(firstArc.attributes.pathLength, "100");
assert.equal(firstArc.attributes.r, "11.5");
assert.equal(firstArc.animations[0].options.duration, 3000);
assert.deepEqual(firstArc.animations[0].frames, [{strokeDashoffset: "0"}, {strokeDashoffset: "-100"}]);
advance(1000);
showFeedback("second", "warning");
const second = region.children[0];
assert.equal(second.children[1].children[0].children[1].animations[0].options.duration, 6000);
advance(1000);
showFeedback("third", "error");
const third = region.children[0];
assert.deepEqual(region.children.map((item) => item.children[0].textContent), ["third", "second", "first"]);
assert.deepEqual(region.children.map((item) => item.attributes["data-tone"]), ["error", "warning", "success"]);
assert.equal(third.children[1].children[1].attributes["aria-label"], "关闭提示");
advance(999);
assert.ok(!first.classList.contains("is-leaving"));
advance(1);
assert.ok(first.classList.contains("is-leaving"));
advance(179);
assert.ok(region.children.includes(first));
advance(1);
assert.ok(!region.children.includes(first));
assert.deepEqual(region.children, [third, second]);
second.children[1].children[1].handlers.click();
second.children[1].children[1].handlers.click();
assert.ok(second.classList.contains("is-leaving"));
assert.deepEqual(region.children, [third, second]);
advance(180);
assert.deepEqual(region.children, [third]);
advance(4639);
assert.ok(!third.classList.contains("is-leaving"));
advance(1);
assert.ok(third.classList.contains("is-leaving"));
advance(180);
assert.deepEqual(region.children, []);
assert.equal(timers.size, 0);

showFeedback("paused", "success");
const paused = region.children[0];
const pausedArc = paused.children[1].children[0].children[1];
advance(1000);
paused.hovered = true;
paused.handlers.mouseenter();
assert.equal(pausedArc.animations[0].state, "paused");
advance(10000);
paused.focused = true;
paused.handlers.focusin();
paused.hovered = false;
paused.handlers.mouseleave();
advance(10000);
assert.ok(!paused.classList.contains("is-leaving"));
paused.focused = false;
paused.handlers.focusout();
await Promise.resolve();
assert.equal(pausedArc.animations[0].state, "cancelled");
assert.equal(pausedArc.animations[1].options.duration, 2000);
assert.ok(Math.abs(Number(pausedArc.animations[1].frames[0].strokeDashoffset) + 100 / 3) < 0.001);
advance(1999);
assert.ok(!paused.classList.contains("is-leaving"));
advance(1);
assert.ok(paused.classList.contains("is-leaving"));
advance(180);
assert.deepEqual(region.children, []);
assert.equal(timers.size, 0);

showFeedback("restored", "success");
const restored = region.children[0];
advance(3000);
assert.ok(restored.classList.contains("is-leaving"));
restored.focused = true;
restored.handlers.focusin();
assert.ok(!restored.classList.contains("is-leaving"));
assert.ok(restored.classList.contains("is-restored"));
const restoredArc = restored.children[1].children[0].children[1];
assert.ok(Math.abs(Number(restoredArc.style.strokeDashoffset) + 200 / 3) < 0.001);
advance(10000);
assert.ok(region.children.includes(restored));
restored.focused = false;
restored.handlers.focusout();
await Promise.resolve();
advance(999);
assert.ok(!restored.classList.contains("is-leaving"));
advance(1);
assert.ok(restored.classList.contains("is-leaving"));
restored.children[1].children[1].handlers.click();
restored.hovered = true;
restored.handlers.mouseenter();
advance(180);
assert.deepEqual(region.children, []);
assert.equal(timers.size, 0);

reducedMotion = true;
showFeedback("reduced-auto", "success");
const reducedAuto = region.children[0];
assert.equal(reducedAuto.children.length, 2);
assert.equal(reducedAuto.children[1].children.length, 1);
advance(3000);
assert.ok(!region.children.includes(reducedAuto));
assert.ok(!reducedAuto.classList.contains("is-leaving"));
showFeedback("reduced-close", "error");
const reducedClose = region.children[0];
reducedClose.children[1].children[0].handlers.click();
assert.deepEqual(region.children, []);
assert.ok(!reducedClose.classList.contains("is-leaving"));
assert.equal(timers.size, 0);

showFeedback("saved.", "success");
assert.equal(region.children[0].children[0].textContent, "saved");
showFeedback("wait...", "warning");
assert.equal(region.children[0].children[0].textContent, "wait...");
showFeedback("v4.29.3", "success");
assert.equal(region.children[0].children[0].textContent, "v4.29.3");
for (const toast of [...region.children]) toast.children[1].children[0].handlers.click();
assert.deepEqual(region.children, []);
assert.equal(timers.size, 0);
