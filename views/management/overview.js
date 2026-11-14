const bridge = window.AstrBotPluginView;
const byId = (id) => document.getElementById(id);
const defaults = [
  {id: "pending", width: "full", visible: true},
  {id: "overall", width: "half", visible: true},
  {id: "recent", width: "half", visible: true},
  {id: "groups", width: "full", visible: true},
  {id: "platforms", width: "half", visible: false},
  {id: "diagnostics", width: "half", visible: false},
];
const names = {pending: "待确认禁言", overall: "整体状态", recent: "近期处理", groups: "各群情况",
  platforms: "平台实例", diagnostics: "排查"};
const grid = byId("overview-grid");
const cards = new Map(Array.from(grid.querySelectorAll("[data-card]"), (card) => [card.dataset.card, card]));
const clone = (items) => items.map((item) => ({...item}));
let saved = clone(defaults);
let draft = null;
let ready = false;
let saving = false;
let drag = null;
let cardFollow = null;

function normalize(layout) {
  if (layout === null) return clone(defaults);
  if (!layout || !Array.isArray(layout.cards) || layout.cards.length > 64) throw new Error("invalid layout");
  const seen = new Set();
  const items = layout.cards.map((item) => {
    if (!item || typeof item !== "object" || typeof item.id !== "string" || !/^[a-z][a-z0-9_-]{0,63}$/.test(item.id)
        || seen.has(item.id) || !["half", "full"].includes(item.width) || typeof item.visible !== "boolean") {
      throw new Error("invalid layout");
    }
    seen.add(item.id);
    return {id: item.id, width: item.width, visible: item.visible};
  });
  return [...items, ...clone(defaults.filter((item) => !seen.has(item.id)))];
}
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}
function button(label, action, ariaLabel) {
  const node = element("button", "secondary", label);
  node.type = "button";
  if (ariaLabel) node.setAttribute("aria-label", ariaLabel);
  node.addEventListener("click", action);
  return node;
}
function stopCardFollow() {
  const follow = cardFollow;
  if (!follow) return;
  cardFollow = null;
  window.cancelAnimationFrame?.(follow.frame);
  for (const animation of follow.animations) animation.cancel();
  follow.cleanup?.();
}
function cardPositions() {
  return new Map([...cards.values()].filter((card) => !card.hidden).map((card) => {
    const rect = card.getBoundingClientRect();
    return [card, {left: rect.left + window.scrollX, top: rect.top + window.scrollY,
      width: rect.width, height: rect.height}];
  }));
}
function cardFollowDestination(card, top, bottom, offset = 0) {
  const rect = card.getBoundingClientRect();
  const available = Math.max(0, window.innerHeight - top - bottom);
  const cardTop = rect.top + window.scrollY - offset;
  const desired = rect.height <= available
    ? cardTop + rect.height / 2 - (window.innerHeight + top - bottom) / 2
    : cardTop - top;
  const maxScroll = Math.max(0, document.scrollingElement.scrollHeight - window.innerHeight);
  return Math.max(0, Math.min(desired, maxScroll));
}
function followCard(card, before, previousScroll) {
  const handle = card.querySelector(".overview-drag-handle");
  handle?.focus({preventScroll: true});
  // A reordered grid can cause scroll anchoring or focus to shift the page.
  // Restore the old position before the browser paints the new layout.
  window.scrollTo({top: previousScroll, behavior: "instant"});
  const start = window.scrollY;
  const padding = window.getComputedStyle(document.documentElement);
  const top = parseFloat(padding.scrollPaddingTop) || 0;
  const bottom = parseFloat(padding.scrollPaddingBottom) || 0;
  if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches || !window.requestAnimationFrame) {
    window.scrollTo({top: cardFollowDestination(card, top, bottom), behavior: "instant"});
    return;
  }
  const animations = [];
  let cardOffset = 0;
  for (const [node, previous] of before) {
    if (node.hidden || typeof node.animate !== "function") continue;
    const current = node.getBoundingClientRect();
    const x = previous.left - current.left - window.scrollX;
    const y = previous.top - current.top - window.scrollY;
    if (Math.abs(x) < 1 && Math.abs(y) < 1) continue;
    const animation = node.animate([{transform: `translate(${x}px, ${y}px)`}, {transform: "translate(0, 0)"}],
      {duration: 320, easing: "linear", fill: "both"});
    animation.pause();
    animation.currentTime = 0;
    animations.push(animation);
    if (node === card) cardOffset = y;
  }
  const follow = {frame: null, animations};
  cardFollow = follow;
  let started = null;
  function advance(time) {
    if (cardFollow !== follow) return;
    if (started === null) started = time;
    const progress = Math.min(1, (time - started) / 320);
    const eased = progress * progress * (3 - 2 * progress);
    for (const animation of animations) animation.currentTime = eased * 320;
    const destination = cardFollowDestination(card, top, bottom, cardOffset * (1 - eased));
    window.scrollTo({top: start + (destination - start) * eased, behavior: "instant"});
    if (progress < 1) follow.frame = window.requestAnimationFrame(advance);
    else stopCardFollow();
  }
  follow.frame = window.requestAnimationFrame(advance);
}
function returnCard(card, before) {
  const previous = before.get(card);
  if (!previous || typeof card.animate !== "function" || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
  const rect = card.getBoundingClientRect();
  const x = previous.left - rect.left - window.scrollX;
  const y = previous.top - rect.top - window.scrollY;
  if (Math.abs(x) < 1 && Math.abs(y) < 1) return;
  const animation = card.animate([{transform: `translate(${x}px, ${y}px)`}, {transform: "translate(0, 0)"}],
    {duration: 200, easing: "cubic-bezier(.2, .8, .2, 1)"});
  const follow = {frame: null, animations: [animation]};
  cardFollow = follow;
  animation.onfinish = () => { if (cardFollow === follow) stopCardFollow(); };
}
function resizeCard(item) {
  if (!draft?.includes(item) || saving || !item.visible) return;
  const before = cardPositions();
  const previousScroll = window.scrollY;
  const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches || !window.requestAnimationFrame;
  // Preserve room to animate the scroll when the new layout is shorter.
  const spacer = !reduced && previousScroll > 0 ? element("div", "overview-resize-spacer") : null;
  if (spacer) {
    spacer.style.height = `${Math.ceil(previousScroll)}px`;
    spacer.setAttribute("aria-hidden", "true");
    byId("panel-status").appendChild(spacer);
  }
  stopCardFollow();
  item.width = item.width === "full" ? "half" : "full";
  renderLayout();
  const card = cards.get(item.id);
  const control = card.querySelector(".overview-width");
  control?.focus({preventScroll: true});
  window.scrollTo({top: previousScroll, behavior: "instant"});
  const start = window.scrollY;
  const padding = window.getComputedStyle(document.documentElement);
  const top = parseFloat(padding.scrollPaddingTop) || 0;
  const bottom = parseFloat(padding.scrollPaddingBottom) || 0;
  const rect = card.getBoundingClientRect();
  const old = before.get(card);
  const available = Math.max(0, window.innerHeight - top - bottom);
  const wasVisible = old && old.top - previousScroll >= top &&
    old.top + old.height - previousScroll <= window.innerHeight - bottom;
  const target = wasVisible && rect.height <= available ? rect : control.getBoundingClientRect();
  const delta = target.top < top ? target.top - top :
    target.bottom > window.innerHeight - bottom ? target.bottom - window.innerHeight + bottom : 0;
  const reserved = spacer ? Math.ceil(previousScroll) : 0;
  const maxScroll = Math.max(0, document.scrollingElement.scrollHeight - window.innerHeight - reserved);
  const destination = Math.max(0, Math.min(start + delta, maxScroll));
  if (reduced) {
    spacer?.remove();
    window.scrollTo({top: destination, behavior: "instant"});
    return;
  }
  const after = cardPositions();
  const rows = window.getComputedStyle(grid).gridTemplateRows;
  const previousRows = grid.style.getPropertyValue("grid-template-rows");
  const previousPriority = grid.style.getPropertyPriority("grid-template-rows");
  if (rows && rows !== "none") grid.style.setProperty("grid-template-rows", rows);
  const animations = [];
  const resized = [];
  const growing = after.get(card).width > before.get(card).width;
  card.classList.add("is-width-target");
  for (const [node, previous] of before) {
    const current = after.get(node);
    if (!current || typeof node.animate !== "function") continue;
    const x = previous.left - current.left;
    const y = previous.top - current.top;
    const sizeChanged = Math.abs(previous.width - current.width) >= 1 || Math.abs(previous.height - current.height) >= 1;
    if (Math.abs(x) < 1 && Math.abs(y) < 1 && !sizeChanged) continue;
    const first = {offset: 0, transform: `translate(${x}px, ${y}px)`};
    const middle = {offset: .45, transform: node === card
      ? (growing ? "translate(0, 0)" : `translate(${x}px, ${y}px)`)
      : (growing ? `translate(${x}px, 0)` : `translate(0, ${y}px)`)};
    const last = {offset: 1, transform: "translate(0, 0)"};
    if (sizeChanged) {
      first.width = `${previous.width}px`; first.height = `${previous.height}px`;
      last.width = `${current.width}px`; last.height = `${current.height}px`;
      middle.width = `${node === card ? (growing ? previous.width : current.width) :
        previous.width + (current.width - previous.width) * .45}px`;
      middle.height = `${node === card ? current.height : previous.height + (current.height - previous.height) * .45}px`;
      node.classList.add("is-width-animating");
      resized.push(node);
    }
    const animation = node.animate([first, middle, last], {duration: 320, easing: "linear", fill: "both"});
    animation.pause();
    animation.currentTime = 0;
    animations.push(animation);
  }
  const follow = {frame: null, animations, cleanup() {
    for (const node of resized) node.classList.remove("is-width-animating");
    card.classList.remove("is-width-target");
    if (rows && rows !== "none" && grid.style.getPropertyValue("grid-template-rows") === rows) {
      if (previousRows) grid.style.setProperty("grid-template-rows", previousRows, previousPriority);
      else grid.style.removeProperty("grid-template-rows");
    }
    spacer?.remove();
  }};
  if (!animations.length && Math.abs(destination - start) < 1) {
    follow.cleanup();
    return;
  }
  cardFollow = follow;
  let started = null;
  function advance(time) {
    if (cardFollow !== follow) return;
    if (started === null) started = time;
    const progress = Math.min(1, (time - started) / 320);
    const eased = progress * progress * (3 - 2 * progress);
    for (const animation of animations) animation.currentTime = eased * 320;
    window.scrollTo({top: start + (destination - start) * eased, behavior: "instant"});
    if (progress < 1) follow.frame = window.requestAnimationFrame(advance);
    else stopCardFollow();
  }
  follow.frame = window.requestAnimationFrame(advance);
}
function moveCard(id, target, motion = null) {
  if (!draft || saving) return;
  const visible = draft.filter((item) => item.visible && cards.has(item.id));
  const from = visible.findIndex((item) => item.id === id);
  if (from < 0 || target < 0 || target >= visible.length || target === from) return;
  stopCardFollow();
  const previousScroll = motion?.scroll ?? window.scrollY;
  const before = motion?.positions ?? cardPositions();
  const item = draft.find((entry) => entry.id === id);
  draft.splice(draft.indexOf(item), 1);
  const remaining = visible.filter((entry) => entry !== item);
  const next = remaining[target];
  draft.splice(next ? draft.indexOf(next) : draft.length, 0, item);
  const following = draft.slice(draft.indexOf(item) + 1).find((entry) => cards.has(entry.id));
  grid.insertBefore(cards.get(id), following ? cards.get(following.id) : null);
  renderLayout();
  byId("overview-order-status").textContent = `${names[id]}已移动到第 ${target + 1} 个位置.`;
  followCard(cards.get(id), before, previousScroll);
}
function renderLayout() {
  cancelDrag();
  stopCardFollow();
  grid.classList.toggle("is-editing", Boolean(draft));
  const items = draft || saved;
  const visible = items.filter((item) => item.visible && cards.has(item.id));
  let cursor = grid.firstElementChild;
  for (const item of items) {
    const card = cards.get(item.id);
    if (!card) continue;
    card.dataset.width = item.width;
    card.hidden = !item.visible;
    if (card !== cursor) grid.insertBefore(card, cursor);
    cursor = card.nextElementSibling;
    let tools = card.querySelector(".overview-card-tools");
    if (tools) tools.remove();
    if (!draft) continue;
    tools = element("div", "overview-card-tools");
    const handle = button("拖动排序", () => {}, `调整 ${names[item.id]} 的位置, 可拖动或按方向键`);
    handle.className = "secondary overview-drag-handle";
    handle.addEventListener("pointerdown", (event) => beginDrag(event, item.id, handle));
    handle.addEventListener("keydown", (event) => {
      if (!["ArrowUp", "ArrowLeft", "ArrowDown", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      const index = draft.filter((entry) => entry.visible && cards.has(entry.id)).findIndex((entry) => entry.id === item.id);
      moveCard(item.id, index + (["ArrowUp", "ArrowLeft"].includes(event.key) ? -1 : 1));
    });
    const index = visible.findIndex((entry) => entry.id === item.id);
    const up = button("上移", () => moveCard(item.id, index - 1), `上移 ${names[item.id]}`);
    const down = button("下移", () => moveCard(item.id, index + 1), `下移 ${names[item.id]}`);
    up.disabled = saving || index === 0;
    down.disabled = saving || index === visible.length - 1;
    handle.disabled = saving || visible.length < 2;
    const width = button(item.width === "full" ? "改为半宽" : "改为整行", () => resizeCard(item),
      `更改 ${names[item.id]} 的宽度`);
    width.classList.add("overview-width");
    width.disabled = saving;
    for (const control of [handle, up, down, width]) tools.appendChild(control);
    card.insertBefore(tools, card.firstChild);
  }
  const visibility = byId("overview-visibility");
  visibility.textContent = "";
  if (draft) for (const item of draft.filter((entry) => cards.has(entry.id))) {
    const label = element("label", "");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = item.visible;
    input.disabled = saving;
    input.addEventListener("change", () => { item.visible = input.checked; renderLayout();
      byId("overview-visibility").querySelector(`[data-visibility="${item.id}"]`)?.focus(); });
    input.dataset.visibility = item.id;
    label.appendChild(input);
    label.appendChild(element("span", "", names[item.id]));
    visibility.appendChild(label);
  }
  byId("overview-editor").hidden = !draft;
  byId("overview-customize").setAttribute("aria-expanded", String(Boolean(draft)));
  byId("overview-customize").disabled = !ready || Boolean(draft) || saving;
  for (const id of ["overview-save", "overview-cancel", "overview-reset"]) byId(id).disabled = saving;
}
async function loadLayout() {
  ready = false;
  byId("overview-customize").disabled = true;
  byId("overview-layout-retry").hidden = true;
  byId("overview-layout-status").textContent = "正在读取布局...";
  try {
    await bridge.ready();
    saved = normalize(await bridge.apiGet("overview-layout"));
    ready = true;
    byId("overview-layout-status").textContent = "";
  } catch {
    byId("overview-layout-status").textContent = "无法读取已保存的布局. 请重试, 当前布局不会被覆盖.";
    byId("overview-layout-retry").hidden = false;
  }
  grid.hidden = false;
  renderLayout();
}
byId("overview-layout-retry").addEventListener("click", loadLayout);
byId("overview-customize").addEventListener("click", () => {
  if (!ready || saving) return;
  draft = clone(saved);
  byId("overview-edit-status").textContent = "修改将在保存后生效.";
  renderLayout();
});
byId("overview-cancel").addEventListener("click", () => {
  if (saving) return;
  cancelDrag();
  draft = null;
  renderLayout();
  byId("overview-customize").focus();
});
byId("overview-reset").addEventListener("click", () => {
  if (!draft || saving) return;
  // Keep cards introduced by newer versions when resetting known defaults.
  const unknown = draft.filter((item) => !cards.has(item.id));
  draft = [...clone(defaults), ...clone(unknown)];
  renderLayout();
  byId("overview-edit-status").textContent = "已预览默认布局, 保存后生效.";
});
byId("overview-save").addEventListener("click", async () => {
  if (!draft || saving) return;
  cancelDrag();
  saving = true;
  const candidate = clone(draft);
  renderLayout();
  byId("overview-edit-status").textContent = "正在保存布局...";
  try {
    saved = normalize(await bridge.apiPost("overview-layout", {cards: candidate}));
    draft = null;
    byId("overview-layout-status").textContent = "布局已保存.";
  } catch {
    byId("overview-edit-status").textContent = "布局保存失败, 未保存的修改仍在这里.";
  } finally {
    saving = false;
    renderLayout();
  }
});

function cancelDrag() {
  const current = drag;
  if (!current) return null;
  drag = null;
  if (current.frame !== null) window.cancelAnimationFrame?.(current.frame);
  for (const {animation} of current.animations.values()) animation.cancel();
  current.marker?.remove();
  current.card.classList.remove("is-dragging");
  for (const [name, value, priority] of current.styles || []) {
    if (value) current.card.style.setProperty(name, value, priority);
    else current.card.style.removeProperty(name);
  }
  try { if (current.handle.hasPointerCapture?.(current.pointer)) current.handle.releasePointerCapture(current.pointer); } catch {}
  return current;
}
function beginDrag(event, id, handle) {
  if (!draft || saving || drag || event.isPrimary === false || (event.pointerType !== "touch" && event.button !== 0)) return;
  const visible = draft.filter((item) => item.visible && cards.has(item.id));
  if (visible.length < 2) return;
  const card = cards.get(id);
  const rect = card.getBoundingClientRect();
  const origin = visible.findIndex((item) => item.id === id);
  drag = {id, card, handle, pointer: event.pointerId, x: event.clientX, y: event.clientY,
    startX: event.clientX, startY: event.clientY, offsetX: event.clientX - rect.left, offsetY: event.clientY - rect.top,
    origin, target: origin, others: visible.filter((item) => item.id !== id), lastSwap: null,
    started: false, marker: null, frame: null, animations: new Map(), styles: null};
  try { handle.setPointerCapture(event.pointerId); } catch {}
  handle.focus({preventScroll: true});
  event.preventDefault();
}
window.addEventListener("pointerdown", (event) => {
  if (!event.target?.closest?.(".overview-width")) stopCardFollow();
}, {capture: true, passive: true});
window.addEventListener("wheel", stopCardFollow, {passive: true});
window.addEventListener("resize", stopCardFollow, {passive: true});
window.addEventListener("keydown", (event) => {
  if (event.key === " " && event.target?.closest?.(".overview-width")) return;
  if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "PageUp", "PageDown", "Home", "End", " ", "Tab", "Escape"].includes(event.key)) stopCardFollow();
}, {capture: true});
function logicalCardRect(current, node) {
  const rect = node.getBoundingClientRect();
  const motion = current.animations.get(node);
  const progress = motion?.animation.effect.getComputedTiming().progress ?? 0;
  const x = motion ? motion.x * (1 - progress) : 0;
  const y = motion ? motion.y * (1 - progress) : 0;
  return {left: rect.left - x, right: rect.right - x, top: rect.top - y, bottom: rect.bottom - y,
    width: rect.width, height: rect.height};
}
function animatePreview(current, before) {
  for (const {animation} of current.animations.values()) animation.cancel();
  current.animations.clear();
  if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
  for (const [node, previous] of before) {
    if (typeof node.animate !== "function") continue;
    const rect = node.getBoundingClientRect();
    const x = previous.left - rect.left - window.scrollX;
    const y = previous.top - rect.top - window.scrollY;
    if (Math.abs(x) < 1 && Math.abs(y) < 1) continue;
    const animation = node.animate([{transform: `translate(${x}px, ${y}px)`}, {transform: "translate(0, 0)"}],
      {duration: 200, easing: "cubic-bezier(.2, .8, .2, 1)"});
    const motion = {animation, x, y};
    current.animations.set(node, motion);
    animation.onfinish = () => { if (current.animations.get(node) === motion) current.animations.delete(node); };
  }
}
function placeMarker(current) {
  const slot = current.marker.getBoundingClientRect();
  if (current.x >= slot.left && current.x <= slot.right && current.y >= slot.top && current.y <= slot.bottom) return;
  const point = {x: current.x, y: current.y + window.scrollY};
  if (current.lastSwap && Math.hypot(point.x - current.lastSwap.x, point.y - current.lastSwap.y) < 12) return;
  let nearest = null;
  let distance = Infinity;
  for (const [index, item] of current.others.entries()) {
    const rect = logicalCardRect(current, cards.get(item.id));
    const dx = Math.max(rect.left - current.x, 0, current.x - rect.right);
    const dy = Math.max(rect.top - current.y, 0, current.y - rect.bottom);
    if (dx * dx + dy * dy < distance) { distance = dx * dx + dy * dy; nearest = {index, rect}; }
  }
  if (!nearest) return;
  const rect = nearest.rect;
  const sameRow = current.y >= rect.top && current.y <= rect.bottom && rect.width < grid.getBoundingClientRect().width * .8;
  const after = sameRow ? current.x > rect.left + rect.width / 2 : current.y > rect.top + rect.height / 2;
  const target = nearest.index + (after ? 1 : 0);
  if (target === current.target) return;
  const before = new Map(current.others.map((item) => {
    const node = cards.get(item.id), bounds = node.getBoundingClientRect();
    return [node, {left: bounds.left + window.scrollX, top: bounds.top + window.scrollY}];
  }));
  current.target = target;
  current.lastSwap = point;
  const next = current.others[target];
  grid.insertBefore(current.marker, next ? cards.get(next.id) : null);
  current.marker.classList.toggle("is-original", target === current.origin);
  animatePreview(current, before);
}
function scrollDrag(current) {
  if (drag !== current || !current.started) return;
  const edge = Math.min(80, window.innerHeight / 4);
  const distance = current.y < edge ? -Math.ceil(16 * (edge - current.y) / edge)
    : current.y > window.innerHeight - edge ? Math.ceil(16 * (current.y - window.innerHeight + edge) / edge) : 0;
  if (distance) { window.scrollBy(0, Math.max(-16, Math.min(16, distance))); placeMarker(current); }
  current.frame = window.requestAnimationFrame(() => scrollDrag(current));
}
window.addEventListener("pointermove", (event) => {
  const current = drag;
  if (!current || current.pointer !== event.pointerId) return;
  current.x = event.clientX; current.y = event.clientY;
  if (!current.started) {
    if (Math.hypot(current.x - current.startX, current.y - current.startY) < 6) return;
    current.started = true;
    const rect = current.card.getBoundingClientRect();
    current.width = rect.width;
    current.styles = ["left", "top", "width", "height"].map((name) =>
      [name, current.card.style.getPropertyValue(name), current.card.style.getPropertyPriority(name)]);
    current.marker = element("div", "overview-drop-placeholder is-original");
    current.marker.setAttribute("aria-hidden", "true");
    current.marker.dataset.width = current.card.dataset.width;
    current.marker.style.minHeight = `${rect.height}px`;
    grid.insertBefore(current.marker, current.card);
    current.card.style.left = `${rect.left}px`;
    current.card.style.top = `${rect.top}px`;
    current.card.style.width = `${rect.width}px`;
    current.card.style.height = `${rect.height}px`;
    current.card.classList.add("is-dragging");
    if (typeof window.requestAnimationFrame === "function") current.frame = window.requestAnimationFrame(() => scrollDrag(current));
  }
  current.card.style.left = `${Math.max(8, Math.min(current.x - current.offsetX, window.innerWidth - current.width - 8))}px`;
  current.card.style.top = `${current.y - current.offsetY}px`;
  placeMarker(current);
  event.preventDefault();
}, {passive: false});
window.addEventListener("pointerup", (event) => {
  if (!drag || drag.pointer !== event.pointerId) return;
  const motion = drag.started ? {positions: cardPositions(), scroll: window.scrollY} : null;
  const current = cancelDrag();
  if (current.started && current.target !== current.origin) moveCard(current.id, current.target, motion);
  else if (current.started) returnCard(current.card, motion.positions);
});
window.addEventListener("pointercancel", (event) => { if (drag?.pointer === event.pointerId) cancelDrag(); });
window.addEventListener("blur", () => { cancelDrag(); stopCardFollow(); });
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && drag) {
    const current = cancelDrag();
    current.handle.focus({preventScroll: true});
    byId("overview-order-status").textContent = "已取消排序.";
    event.preventDefault();
  }
});
document.addEventListener("visibilitychange", () => { if (document.hidden) { cancelDrag(); stopCardFollow(); } });

let active = false;
let scope = {all_groups: "true"};
let groupNames = new Map();
let pendingRevision = 0;
let pendingMessages = [];
let pendingCursor = null;
let pendingTimer = null;
let pendingRecheckTimer = null;
let pendingReadError = false;
let pendingLoadingFade = Promise.resolve();
const confirming = new Set();
let showFeedback = (message) => { byId("overview-pending-status").textContent = message; };
const messageKey = (item) => JSON.stringify([item.platform_id, item.group_id, item.message_id]);
function time(value) {
  const date = new Date(value);
  if (!value || Number.isNaN(date.getTime())) return "不可用";
  return date.toLocaleString();
}
function reason(action) {
  try {
    const parsed = JSON.parse(action.detail);
    const decision = parsed?.decision && typeof parsed.decision === "object" ? parsed.decision : parsed;
    const value = decision?.proposal?.reason || decision?.reason;
    return typeof value === "string" && value.trim() ? value : "暂无判定理由.";
  } catch { return "暂无判定理由."; }
}
function duration(value) {
  if (!Number.isSafeInteger(value) || value <= 0) return "时长不可用";
  const parts = [];
  for (const [unit, label] of [[86400, "天"], [3600, "小时"], [60, "分钟"], [1, "秒"]]) {
    const count = Math.floor(value / unit);
    if (count) parts.push(`${count}${label}`);
    value %= unit;
  }
  return parts.join(" ");
}
function progress() {
  const list = element("ol", "action-progress");
  for (const [label, state] of [["判定完成", "done"], ["待确认", "current"], ["执行结果尚未产生", "future"]]) {
    const item = element("li", "", label);
    item.dataset.state = state;
    list.appendChild(item);
  }
  return list;
}
function renderPending() {
  const list = byId("overview-pending-list");
  const expanded = new Set(Array.from(list.querySelectorAll("details[open]"), (item) => item.dataset.key));
  const focusedIntent = document.activeElement?.dataset?.intentId;
  list.textContent = "";
  for (const message of pendingMessages) {
    const section = element("details", "overview-pending-message");
    section.dataset.key = messageKey(message);
    section.open = expanded.has(section.dataset.key);
    const name = groupNames.get(JSON.stringify([message.platform_id, message.group_id])) || "未命名群";
    const first = message.actions[0];
    const summary = element("summary", "overview-pending-summary");
    summary.appendChild(element("strong", "overview-pending-group", name));
    const excerpt = typeof message.content === "string" ? message.content.replace(/\s+/g, " ").slice(0, 80) : "消息原文未保存或已过期";
    summary.appendChild(element("span", "overview-pending-excerpt", excerpt || "消息原文为空"));
    const date = first?.message_time ? `消息 ${time(first.message_time)}` : `判定 ${time(first?.created_at)}`;
    summary.appendChild(element("span", "overview-pending-meta", `${date} / ${message.actions.length} 个动作`));
    section.appendChild(summary);
    section.appendChild(element("p", "field-help", `消息时间: ${time(first?.message_time)} / 判定时间: ${time(first?.created_at)}`));
    section.appendChild(element("p", "field-help", message.content === null ? "消息原文未保存或已过期." :
      `消息原文: ${message.content}${message.content_truncated ? "..." : ""}`));
    const actions = element("ol", "overview-pending-actions");
    for (const action of message.actions) {
      const item = element("li", "overview-pending-action");
      item.appendChild(element("strong", "", `禁言 ${duration(action.duration_seconds)}`));
      item.appendChild(element("p", "field-help", `规则: ${action.rule_id} / 第 ${action.rule_version} 版`));
      item.appendChild(element("p", "field-help", `判定理由: ${reason(action)}`));
      if (action.expires_at) item.appendChild(element("p", "field-help", `动作有效期至: ${time(action.expires_at)}`));
      if (action.expires_at && Number.isFinite(Date.parse(action.expires_at)) && Date.parse(action.expires_at) <= Date.now()) {
        item.appendChild(element("p", "field-help", "动作可能已到期, 确认时将重新核验."));
      }
      item.appendChild(progress());
      item.appendChild(element("p", "field-help", "确认将尝试执行此项禁言, 实际结果以执行记录为准."));
      const execute = button("确认并尝试禁言", async () => {
        if (confirming.has(action.id)) return;
        confirming.add(action.id);
        execute.disabled = true;
        try {
          const result = await bridge.apiPost("confirm-pending-mute", {intent_id: action.id});
          const labels = {succeeded: "禁言已执行.", failed: "禁言执行失败, 请查看判定记录.",
            blocked: "禁言无法执行, 请查看判定记录.", permission_denied: "权限不足, 禁言未执行.",
            unknown: "执行结果不明, 请人工核查.", review: "动作需要人工复核, 请查看判定记录.",
            not_claimed: "动作状态已变化或执行前检查未通过, 请查看判定记录."};
          const reasons = {intent_expired: "动作已到期, 禁言未执行.", invalid_expiry: "动作有效期无效, 禁言未执行.",
            group_not_monitored: "本群监控未开启, 禁言未执行.", rule_disabled: "规则已停用, 禁言未执行.",
            rule_version_changed: "规则版本已变化, 禁言未执行.", action_not_allowed: "规则不再允许此动作, 禁言未执行.",
            duration_not_in_rule: "禁言时长不在规则设置中, 禁言未执行.", invalid_duration: "禁言时长无效或超过上限, 禁言未执行."};
          const message = result?.status === "not_claimed" && Object.hasOwn(reasons, result.reason_code)
            ? reasons[result.reason_code] : labels[result?.status] || "执行状态未知, 请查看判定记录.";
          const tone = result?.status === "succeeded" ? "success" : ["unknown", "review", "not_claimed"].includes(result?.status) ? "warning" : "error";
          showFeedback(message, tone);
        } catch {
          showFeedback("无法确认动作结果, 请查看判定记录后再操作.", "warning");
        } finally {
          confirming.delete(action.id);
          await refreshPending(true);
        }
      }, `确认并尝试禁言, ${name}`);
      execute.dataset.intentId = String(action.id);
      execute.disabled = confirming.has(action.id);
      item.appendChild(execute);
      actions.appendChild(item);
    }
    section.appendChild(actions);
    list.appendChild(section);
  }
  byId("overview-pending-more").hidden = pendingCursor === null;
  if (focusedIntent) {
    const focus = Array.from(list.querySelectorAll("button[data-intent-id]"))
      .find((item) => item.dataset.intentId === focusedIntent && !item.disabled);
    focus?.focus({preventScroll: true});
  }
}
function showPendingEmpty(empty) {
  byId("overview-pending-empty").hidden = !empty;
  byId("overview-pending-count").hidden = empty;
}
function canFadePending(node) {
  return node?.isConnected && node.getClientRects?.().length && typeof node.animate === "function" &&
    !window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
}
function fadePending(node) {
  if (canFadePending(node)) node.animate([{opacity: 0}, {opacity: 1}], {duration: 180, easing: "ease-out"});
}
async function refreshPending(preserve = false, append = false) {
  if (!active) return;
  const request = ++pendingRevision;
  const pageSize = 5;
  const target = preserve ? Math.max(pageSize, pendingMessages.length) : pageSize;
  const previous = append ? pendingMessages.slice() : [];
  let cursor = append ? pendingCursor : null;
  const entries = previous.slice();
  let result;
  byId("overview-pending-more").disabled = true;
  if (!append && !preserve) {
    showPendingEmpty(false);
    const count = byId("overview-pending-count");
    count.textContent = "正在读取...";
    count.classList.add("is-loading");
    fadePending(count);
    pendingLoadingFade = canFadePending(count) ? new Promise((resolve) => setTimeout(resolve, 180)) : Promise.resolve();
  }
  try {
    do {
      const limit = preserve && !append ? Math.min(100, Math.max(pageSize, target - entries.length)) : pageSize;
      result = await bridge.apiGet("pending-confirmations", {...scope, limit, ...(cursor === null ? {} : {before_id: cursor})});
      if (request !== pendingRevision) return;
      if (!result || !Array.isArray(result.messages) || !Number.isSafeInteger(result.total_messages)
          || result.total_messages < 0 || !Number.isSafeInteger(result.total_actions) || result.total_actions < 0
          || result.total_actions < result.total_messages || result.messages.length > result.total_messages
          || !(result.next_before_id === null || (Number.isSafeInteger(result.next_before_id) && result.next_before_id > 0))
          || (result.next_before_id !== null && (!result.messages.length || (cursor !== null && result.next_before_id >= cursor)))) throw new Error("invalid result");
      for (const item of result.messages) {
        if (!item || !Array.isArray(item.actions) || !item.actions.length || typeof item.platform_id !== "string"
            || typeof item.group_id !== "string" || typeof item.message_id !== "string"
            || !(item.content === null || typeof item.content === "string")
            || item.actions.some((action) => !action || !Number.isSafeInteger(action.id) || action.id <= 0
              || action.action !== "mute" || action.mode !== "confirm" || action.status !== "pending_confirm")) throw new Error("invalid result");
        const index = entries.findIndex((entry) => messageKey(entry) === messageKey(item));
        if (index < 0) entries.push(item); else entries[index] = item;
      }
      cursor = result.next_before_id;
    } while (!append && preserve && cursor !== null && entries.length < target);
    await pendingLoadingFade;
    if (request !== pendingRevision) return;
    if (result.total_actions === 0) entries.length = 0;
    pendingMessages = entries;
    pendingCursor = cursor;
    if (pendingReadError) byId("overview-pending-status").textContent = "";
    pendingReadError = false;
    const wasLoading = byId("overview-pending-count").classList.contains("is-loading");
    byId("overview-pending-count").classList.remove("is-loading");
    showPendingEmpty(result.total_actions === 0);
    byId("overview-pending-count").textContent = result.total_actions ? `${result.total_actions} 个动作, 涉及 ${result.total_messages} 条消息` : "";
    renderPending();
    if (wasLoading) {
      fadePending(byId(result.total_actions ? "overview-pending-count" : "overview-pending-empty"));
      fadePending(byId("overview-pending-list"));
    }
  } catch {
    await pendingLoadingFade;
    if (request !== pendingRevision) return;
    pendingMessages = [];
    pendingCursor = null;
    pendingReadError = true;
    showPendingEmpty(false);
    byId("overview-pending-count").classList.remove("is-loading");
    byId("overview-pending-count").textContent = "待确认列表不可用.";
    byId("overview-pending-status").textContent = "无法读取待确认动作, 请刷新后重试.";
    renderPending();
    fadePending(byId("overview-pending-count"));
  } finally {
    if (request === pendingRevision) byId("overview-pending-more").disabled = false;
  }
}
function schedulePending() {
  if (!active || pendingTimer !== null) return;
  pendingTimer = setTimeout(() => { pendingTimer = null; refreshPending(true); }, 300);
}
function scheduleRecheck() {
  if (pendingRecheckTimer !== null) clearTimeout(pendingRecheckTimer);
  pendingRecheckTimer = null;
  if (!active || document.hidden) return;
  pendingRecheckTimer = setTimeout(async () => {
    pendingRecheckTimer = null;
    try { await refreshPending(true); } finally { scheduleRecheck(); }
  }, 30000);
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && active) refreshPending(true);
  scheduleRecheck();
});
window.addEventListener("pagehide", () => {
  if (pendingRecheckTimer !== null) clearTimeout(pendingRecheckTimer);
  pendingRecheckTimer = null;
});
byId("overview-pending-more").addEventListener("click", () => refreshPending(false, true));
window.SentinelOverview = {
  setFeedback(callback) { if (typeof callback === "function") showFeedback = callback; },
  setScope(value) {
    scope = {...value};
    pendingRevision++;
    pendingMessages = [];
    pendingCursor = null;
    pendingReadError = false;
    showPendingEmpty(false);
    byId("overview-pending-scope").textContent = scope.all_groups ? "所有群" :
      (groupNames.get(JSON.stringify([scope.platform_id, scope.group_id])) || "当前群");
    byId("overview-pending-status").textContent = "";
    renderPending();
    if (active) refreshPending();
  },
  setGroups(items) {
    groupNames = new Map(items.map((item) => [JSON.stringify([item.platform_id, item.group_id]), item.name]));
    byId("overview-pending-scope").textContent = scope.all_groups ? "所有群" :
      (groupNames.get(JSON.stringify([scope.platform_id, scope.group_id])) || "当前群");
  },
  setActive(value) {
    active = value;
    if (active) refreshPending();
    else { pendingRevision++; if (pendingTimer !== null) clearTimeout(pendingTimer); pendingTimer = null; }
    scheduleRecheck();
  },
  setLiveState(mode) {
    const label = byId("overview-pending-live");
    label.textContent = mode === "connected" ? "实时 · 已连接" : mode === "polling" ? "已断开, 使用轮询" : "实时 · 重连中";
    label.dataset.tone = mode === "connected" ? "success" : "warning";
  },
  refresh: () => refreshPending(true),
  onEvent: schedulePending,
};
loadLayout();
