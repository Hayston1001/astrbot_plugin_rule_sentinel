import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import vm from "node:vm";

function element() {
  const classes = new Set();
  return {
    hidden: false, disabled: false, clientWidth: 800, clientHeight: 600,
    offsetWidth: 700, offsetHeight: 450, style: {}, handlers: {},
    classList: {
      add(name) { classes.add(name); },
      remove(name) { classes.delete(name); },
      contains(name) { return classes.has(name); },
      toggle(name, value) { if (value) classes.add(name); else classes.delete(name); },
    },
    addEventListener(type, fn) { this.handlers[type] = fn; },
    focus() { document.activeElement = this; },
    removeAttribute(name) { delete this[name]; },
    setPointerCapture() {},
  };
}
const elements = Object.fromEntries(["image-viewer", "image-zoom-in", "image-zoom-out",
  "image-zoom-reset", "image-viewer-close"].map((id) => [id, element()]));
const viewer = elements["image-viewer"];
const image = element();
image.tagName = "IMG";
viewer.hidden = true;
viewer.querySelector = () => image;
globalThis.el = (id) => elements[id];
globalThis.window = {addEventListener() {}, matchMedia() { return {matches: true}; }};
globalThis.document = {activeElement: null, handlers: {},
  addEventListener(type, fn) { this.handlers[type] = fn; }};
const source = await readFile(process.argv[2], "utf8");
const code = source.slice(source.indexOf("let imageViewerScale = 1;"),
  source.indexOf('el("group-reason-language-mode").addEventListener("change"'));
vm.runInThisContext(code);
const opener = element();
openImageViewer("data:image/png;base64,abc", "消息图片 1", opener);
assert.equal(viewer.hidden, false);
assert.equal(image.alt, "消息图片 1");
assert.equal(elements["image-zoom-out"].disabled, true);
elements["image-zoom-in"].handlers.click();
assert.equal(elements["image-zoom-reset"].textContent, "125%");
viewer.handlers.pointerdown({target: image, button: 0, pointerId: 1, clientX: 0, clientY: 0, preventDefault() {}});
viewer.handlers.pointermove({pointerId: 1, clientX: 50, clientY: 20});
assert.match(image.style.transform, /translate\((?!0px, 0px)/);
viewer.handlers.pointerup({pointerId: 1});
elements["image-zoom-reset"].handlers.click();
assert.equal(image.style.transform, "translate(0px, 0px) scale(1)");
let prevented = false;
viewer.handlers.wheel({deltaY: -1, preventDefault() { prevented = true; }});
assert.ok(prevented);
assert.equal(elements["image-zoom-reset"].textContent, "120%");
viewer.handlers.click({target: image, currentTarget: viewer});
assert.equal(viewer.hidden, false);
document.handlers.keydown({key: "Escape"});
assert.equal(viewer.hidden, true);
assert.equal(document.activeElement, opener);
assert.equal(image.src, undefined);
