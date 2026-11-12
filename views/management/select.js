/* Enhance single-select controls without changing their values or change handlers. */
const states = new WeakMap();
const suggestionStates = new WeakMap();
const valueProperty = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value");
const menu = document.createElement("div");
menu.id = "custom-select-menu";
menu.className = "custom-select-menu";
menu.setAttribute("role", "listbox");
menu.setAttribute("aria-hidden", "true");
document.body.appendChild(menu);
const suggestions = document.createElement("div");
suggestions.id = "custom-suggestions";
suggestions.className = "custom-select-menu";
suggestions.setAttribute("role", "listbox");
suggestions.setAttribute("aria-hidden", "true");
document.body.appendChild(suggestions);

let active = null;
let activeSuggestion = null;
let openingFrame = 0;
let suggestionFrame = 0;
let searchText = "";
let searchTime = 0;

function labelFor(select) {
  const labels = [...(select.labels || [])];
  return labels.map((label) => [...label.childNodes]
    .filter((node) => node.nodeType === Node.TEXT_NODE)
    .map((node) => node.textContent.trim()).filter(Boolean).join(" "))
    .filter(Boolean).join(" ") || select.getAttribute("aria-label") || "选择";
}

function available(option) {
  return !!option && !option.disabled && !option.hidden && !(option.parentElement?.tagName === "OPTGROUP" && option.parentElement.disabled);
}

function sync(state) {
  const {select, trigger, value} = state;
  const selected = select.selectedOptions[0];
  const text = selected?.label || "";
  value.textContent = text;
  trigger.title = selected?.title || text;
  trigger.disabled = select.disabled;
  trigger.setAttribute("aria-label", `${labelFor(select)}: ${text}`);
  for (const attribute of ["aria-describedby", "aria-invalid"]) {
    if (select.hasAttribute(attribute)) trigger.setAttribute(attribute, select.getAttribute(attribute));
    else trigger.removeAttribute(attribute);
  }
  if (active === state) {
    if (select.disabled || !select.isConnected) closeMenu();
    else renderOptions(state);
  }
}

function positionPopup(popup, anchor) {
  if (!anchor.isConnected) return false;
  const rect = anchor.getBoundingClientRect();
  if (!rect.width || !rect.height || rect.bottom <= 0 || rect.top >= window.innerHeight) return false;
  const margin = 8;
  const gap = 6;
  const below = Math.max(0, window.innerHeight - rect.bottom - margin - gap);
  const above = Math.max(0, rect.top - margin - gap);
  const up = below < Math.min(220, popup.scrollHeight) && above > below;
  const space = up ? above : below;
  const width = Math.min(Math.max(rect.width, 180), window.innerWidth - margin * 2);
  popup.style.width = `${Math.max(0, width)}px`;
  popup.style.left = `${Math.max(margin, Math.min(rect.left, window.innerWidth - margin - width))}px`;
  popup.style.maxHeight = `${Math.max(0, Math.min(280, space))}px`;
  popup.classList.toggle("opens-up", up);
  if (up) { popup.style.top = "auto"; popup.style.bottom = `${window.innerHeight - rect.top + gap}px`; }
  else { popup.style.bottom = "auto"; popup.style.top = `${rect.bottom + gap}px`; }
  return true;
}

function positionMenu(state) {
  if (!positionPopup(menu, state.trigger)) closeMenu();
}

function setActiveOption(state, index, scroll = true) {
  const option = menu.querySelector(`[data-index="${index}"]`);
  if (!option || option.getAttribute("aria-disabled") === "true") return;
  state.activeIndex = index;
  for (const item of menu.querySelectorAll(".custom-select-option")) item.classList.toggle("is-active", item === option);
  state.trigger.setAttribute("aria-activedescendant", option.id);
  if (scroll) {
    const top = option.offsetTop;
    const bottom = top + option.offsetHeight;
    if (top < menu.scrollTop) menu.scrollTop = top;
    else if (bottom > menu.scrollTop + menu.clientHeight) menu.scrollTop = bottom - menu.clientHeight;
  }
}

function renderOptions(state) {
  const options = [...state.select.options];
  menu.replaceChildren();
  options.forEach((option, index) => {
    if (option.hidden) return;
    const item = document.createElement("div");
    item.id = `custom-select-option-${index}`;
    item.className = "custom-select-option";
    item.dataset.index = String(index);
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", String(index === state.select.selectedIndex));
    if (!available(option)) item.setAttribute("aria-disabled", "true");
    item.title = option.title || option.label;
    const text = document.createElement("span");
    text.textContent = option.label;
    item.appendChild(text);
    item.addEventListener("pointermove", (event) => {
      if (event.pointerType === "mouse") setActiveOption(state, index, false);
    });
    item.addEventListener("click", () => chooseOption(state, index));
    menu.appendChild(item);
  });
  const first = options.findIndex(available);
  const desired = available(options[state.activeIndex]) ? state.activeIndex :
    available(options[state.select.selectedIndex]) ? state.select.selectedIndex : first;
  if (desired >= 0) setActiveOption(state, desired, false);
  else state.trigger.removeAttribute("aria-activedescendant");
  positionMenu(state);
}

function closeMenu() {
  if (!active) return;
  if (openingFrame) cancelAnimationFrame(openingFrame);
  openingFrame = 0;
  active.trigger.setAttribute("aria-expanded", "false");
  active.trigger.removeAttribute("aria-activedescendant");
  menu.setAttribute("aria-hidden", "true");
  menu.classList.remove("is-open");
  active = null;
  searchText = "";
}

function openMenu(state) {
  if (state.select.disabled) return;
  closeSuggestions();
  closeMenu();
  active = state;
  state.activeIndex = state.select.selectedIndex;
  state.trigger.setAttribute("aria-expanded", "true");
  menu.setAttribute("aria-hidden", "false");
  menu.setAttribute("aria-label", labelFor(state.select));
  renderOptions(state);
  if (active !== state) return;
  setActiveOption(state, state.activeIndex);
  openingFrame = requestAnimationFrame(() => {
    openingFrame = 0;
    if (active === state) menu.classList.add("is-open");
  });
}

function closeSuggestions() {
  if (!activeSuggestion) return;
  if (suggestionFrame) cancelAnimationFrame(suggestionFrame);
  suggestionFrame = 0;
  activeSuggestion.input.setAttribute("aria-expanded", "false");
  activeSuggestion.toggle.setAttribute("aria-expanded", "false");
  activeSuggestion.input.removeAttribute("aria-activedescendant");
  suggestions.setAttribute("aria-hidden", "true");
  suggestions.classList.remove("is-open");
  activeSuggestion = null;
}

function activateSuggestion(state, index) {
  const item = suggestions.querySelector(`[data-index="${index}"]`);
  if (!item) return;
  state.activeIndex = index;
  for (const option of suggestions.querySelectorAll(".custom-select-option")) {
    option.classList.toggle("is-active", option === item);
    option.setAttribute("aria-selected", String(option === item));
  }
  state.input.setAttribute("aria-activedescendant", item.id);
  if (item.offsetTop < suggestions.scrollTop) suggestions.scrollTop = item.offsetTop;
  else if (item.offsetTop + item.offsetHeight > suggestions.scrollTop + suggestions.clientHeight) {
    suggestions.scrollTop = item.offsetTop + item.offsetHeight - suggestions.clientHeight;
  }
}

function chooseSuggestion(state, value) {
  const changed = state.input.value !== value;
  closeSuggestions();
  if (changed) {
    state.input.value = value;
    state.committing = true;
    state.input.dispatchEvent(new Event("input", {bubbles: true}));
    state.input.dispatchEvent(new Event("change", {bubbles: true}));
    state.committing = false;
  }
  if (state.input.isConnected && !state.input.disabled) {
    state.committing = true;
    state.input.focus({preventScroll: true});
    state.committing = false;
  }
}

function showSuggestions(state, showAll = false) {
  if (state.input.disabled || state.input.readOnly || !state.input.isConnected || document.activeElement !== state.input) {
    if (activeSuggestion === state) closeSuggestions();
    return;
  }
  state.showAll = showAll;
  const query = showAll ? "" : state.input.value.toLocaleLowerCase();
  const options = [...state.list.options].filter((option) => !option.disabled && !option.hidden && option.value &&
    (option.value.toLocaleLowerCase().includes(query) || option.label.toLocaleLowerCase().includes(query)));
  if (!options.length) { if (activeSuggestion === state) closeSuggestions(); return; }
  const reopening = activeSuggestion !== state;
  if (reopening) { closeMenu(); closeSuggestions(); activeSuggestion = state; }
  state.activeIndex = -1;
  state.input.removeAttribute("aria-activedescendant");
  suggestions.replaceChildren();
  options.forEach((option, index) => {
    const item = document.createElement("div");
    item.id = `custom-suggestion-${index}`;
    item.className = "custom-select-option";
    item.dataset.index = String(index);
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", "false");
    item.textContent = option.value;
    item.title = option.value;
    item.addEventListener("pointermove", (event) => { if (event.pointerType === "mouse") activateSuggestion(state, index); });
    item.addEventListener("click", () => chooseSuggestion(state, option.value));
    suggestions.appendChild(item);
  });
  state.input.setAttribute("aria-expanded", "true");
  state.toggle.setAttribute("aria-expanded", "true");
  suggestions.setAttribute("aria-hidden", "false");
  if (!positionPopup(suggestions, state.input)) { closeSuggestions(); return; }
  if (reopening) suggestionFrame = requestAnimationFrame(() => {
    suggestionFrame = 0;
    if (activeSuggestion === state) suggestions.classList.add("is-open");
  });
}

function enhanceSuggestions(input) {
  if (suggestionStates.has(input)) return;
  const list = document.getElementById(input.getAttribute("list"));
  if (!list || list.tagName !== "DATALIST") return;
  const wrapper = document.createElement("span");
  wrapper.className = "suggestion-field";
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "suggestion-toggle";
  toggle.setAttribute("aria-label", `显示${labelFor(input)}选项`);
  toggle.setAttribute("aria-controls", suggestions.id);
  toggle.setAttribute("aria-expanded", "false");
  const arrow = document.createElement("span");
  arrow.className = "custom-select-arrow";
  arrow.setAttribute("aria-hidden", "true");
  toggle.appendChild(arrow);
  input.parentNode.insertBefore(wrapper, input);
  wrapper.append(input, toggle);
  toggle.disabled = input.disabled || input.readOnly;
  const state = {input, list, wrapper, toggle, activeIndex: -1, committing: false, showAll: false};
  suggestionStates.set(input, state);
  input.removeAttribute("list");
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-haspopup", "listbox");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-controls", suggestions.id);
  input.addEventListener("focus", () => { if (!state.committing) showSuggestions(state); });
  input.addEventListener("click", () => { if (activeSuggestion !== state) showSuggestions(state); });
  input.addEventListener("input", () => { if (!state.committing) showSuggestions(state); });
  toggle.addEventListener("click", () => {
    if (activeSuggestion === state && state.showAll) { closeSuggestions(); return; }
    state.committing = true;
    input.focus({preventScroll: true});
    state.committing = false;
    showSuggestions(state, true);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && activeSuggestion === state) { event.preventDefault(); closeSuggestions(); }
    else if (event.key === "Tab" && activeSuggestion === state) closeSuggestions();
    else if (["ArrowDown", "ArrowUp"].includes(event.key)) {
      if (activeSuggestion !== state) showSuggestions(state);
      if (activeSuggestion !== state) return;
      event.preventDefault();
      const count = suggestions.children.length;
      const next = event.key === "ArrowDown" ? Math.min(count - 1, state.activeIndex + 1) :
        state.activeIndex < 0 ? count - 1 : Math.max(0, state.activeIndex - 1);
      activateSuggestion(state, next);
    } else if (event.key === "Enter" && activeSuggestion === state && state.activeIndex >= 0) {
      const item = suggestions.querySelector(`[data-index="${state.activeIndex}"]`);
      if (item) { event.preventDefault(); chooseSuggestion(state, item.textContent); }
    }
  });
  new MutationObserver(() => { if (activeSuggestion === state) showSuggestions(state, state.showAll); })
    .observe(list, {subtree: true, childList: true, characterData: true, attributes: true,
      attributeFilter: ["disabled", "hidden", "label", "value"]});
  new MutationObserver(() => {
    toggle.disabled = input.disabled || input.readOnly;
    if (activeSuggestion === state && toggle.disabled) closeSuggestions();
  })
    .observe(input, {attributes: true, attributeFilter: ["disabled", "readonly"]});
}

function chooseOption(state, index) {
  const option = state.select.options[index];
  if (!option || !available(option)) return;
  const changed = state.select.selectedIndex !== index;
  state.select.selectedIndex = index;
  closeMenu();
  sync(state);
  if (changed) {
    state.select.dispatchEvent(new Event("input", {bubbles: true}));
    state.select.dispatchEvent(new Event("change", {bubbles: true}));
    sync(state);
  }
  if (state.trigger.isConnected && !state.trigger.disabled) state.trigger.focus({preventScroll: true});
}

function navigate(state, key) {
  const options = [...state.select.options];
  const enabled = options.map((option, index) => available(option) ? index : -1).filter((index) => index >= 0);
  if (!enabled.length) return;
  const current = enabled.indexOf(state.activeIndex);
  const next = key === "Home" ? 0 : key === "End" ? enabled.length - 1 :
    key === "ArrowDown" ? Math.min(enabled.length - 1, current + 1) : Math.max(0, current - 1);
  setActiveOption(state, enabled[next]);
}

function handleKey(state, event) {
  if (event.key === "Tab") { if (active === state) closeMenu(); return; }
  if (event.key === "Escape") { if (active === state) { event.preventDefault(); closeMenu(); } return; }
  if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
    event.preventDefault();
    if (active !== state) openMenu(state);
    else navigate(state, event.key);
    return;
  }
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    if (active === state) chooseOption(state, state.activeIndex);
    else openMenu(state);
    return;
  }
  if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey && !event.isComposing) {
    const now = performance.now();
    searchText = (now - searchTime > 700 ? "" : searchText) + event.key.toLocaleLowerCase();
    searchTime = now;
    if (active !== state) { const query = searchText; openMenu(state); searchText = query; }
    const index = [...state.select.options].findIndex((option) => available(option) && option.label.toLocaleLowerCase().startsWith(searchText));
    if (index >= 0) setActiveOption(state, index);
  }
}

function enhance(select) {
  if (states.has(select) || select.multiple || select.size > 1 || !select.parentNode) return;
  const wrapper = document.createElement("span");
  wrapper.className = "custom-select";
  if (select.classList.contains("action-type")) wrapper.classList.add("action-type-control");
  if (select.classList.contains("action-mode")) wrapper.classList.add("action-mode-control");
  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "custom-select-trigger";
  trigger.setAttribute("role", "combobox");
  trigger.setAttribute("aria-haspopup", "listbox");
  trigger.setAttribute("aria-expanded", "false");
  trigger.setAttribute("aria-controls", menu.id);
  const value = document.createElement("span");
  value.className = "custom-select-value";
  const arrow = document.createElement("span");
  arrow.className = "custom-select-arrow";
  arrow.setAttribute("aria-hidden", "true");
  trigger.append(value, arrow);
  select.parentNode.insertBefore(wrapper, select);
  wrapper.append(select, trigger);
  select.classList.add("custom-select-native");
  select.tabIndex = -1;
  select.setAttribute("aria-hidden", "true");
  const state = {select, wrapper, trigger, value, activeIndex: -1};
  states.set(select, state);
  if (valueProperty?.get && valueProperty?.set) Object.defineProperty(select, "value", {
    configurable: true,
    get() { return valueProperty.get.call(this); },
    set(next) { valueProperty.set.call(this, next); sync(state); },
  });
  select.focus = (options) => trigger.focus(options);
  select.addEventListener("click", (event) => { event.preventDefault(); trigger.focus(); openMenu(state); });
  select.addEventListener("change", () => sync(state));
  trigger.addEventListener("click", () => { if (active === state) closeMenu(); else openMenu(state); });
  trigger.addEventListener("keydown", (event) => handleKey(state, event));
  new MutationObserver(() => sync(state)).observe(select, {subtree: true, childList: true, characterData: true, attributes: true,
    attributeFilter: ["disabled", "hidden", "label", "value", "selected", "title", "aria-describedby", "aria-invalid"]});
  sync(state);
}

function enhanceWithin(node) {
  if (node.nodeType !== Node.ELEMENT_NODE) return;
  if (node.matches("select")) enhance(node);
  if (node.matches("input[list]")) enhanceSuggestions(node);
  for (const select of node.querySelectorAll("select")) enhance(select);
  for (const input of node.querySelectorAll("input[list]")) enhanceSuggestions(input);
}

for (const select of document.querySelectorAll("select")) enhance(select);
for (const input of document.querySelectorAll("input[list]")) enhanceSuggestions(input);
new MutationObserver((records) => {
  for (const record of records) for (const node of record.addedNodes) enhanceWithin(node);
  if (active && !active.trigger.isConnected) closeMenu();
  if (activeSuggestion && !activeSuggestion.input.isConnected) closeSuggestions();
}).observe(document.body, {subtree: true, childList: true});
document.addEventListener("pointerdown", (event) => {
  if (active && !active.wrapper.contains(event.target) && !menu.contains(event.target)) closeMenu();
  if (activeSuggestion && !activeSuggestion.wrapper.contains(event.target) && !suggestions.contains(event.target)) closeSuggestions();
});
menu.addEventListener("mousedown", (event) => event.preventDefault());
suggestions.addEventListener("mousedown", (event) => event.preventDefault());
document.addEventListener("focusin", (event) => {
  if (active && !active.wrapper.contains(event.target) && !menu.contains(event.target)) closeMenu();
  if (activeSuggestion && !activeSuggestion.wrapper.contains(event.target) && !suggestions.contains(event.target)) closeSuggestions();
});
function repositionPopups() {
  if (active) positionMenu(active);
  if (activeSuggestion && !positionPopup(suggestions, activeSuggestion.input)) closeSuggestions();
}
window.addEventListener("scroll", repositionPopups, true);
window.addEventListener("resize", repositionPopups);
window.addEventListener("blur", () => { closeMenu(); closeSuggestions(); });
