const bridge = window.AstrBotPluginView;
const el = (id) => document.getElementById(id);
function showFeedback(message, tone = "success") {
  const region = el("feedback-region");
  const toast = document.createElement("div");
  toast.className = "feedback-toast";
  toast.setAttribute("data-tone", tone);
  const text = document.createElement("span");
  text.className = "feedback-message";
  text.textContent = message;
  const close = document.createElement("button");
  close.type = "button";
  close.className = "feedback-close";
  close.setAttribute("aria-label", "关闭提示");
  close.textContent = "\u00d7";
  const duration = tone === "success" ? 3000 : 6000;
  let remaining = duration;
  let startedAt = 0;
  let timer = null;
  let exitTimer = null;
  let countdown = null;
  let leaving = false;
  let manuallyClosed = false;
  let outline = null;
  if (typeof document.createElementNS === "function" && !prefersReducedMotion()) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    if (typeof rect.animate === "function") {
      svg.setAttribute("class", "feedback-countdown");
      svg.setAttribute("aria-hidden", "true");
      svg.setAttribute("focusable", "false");
      for (const [name, value] of Object.entries({x: "1.5", y: "1.5", rx: "13.5", pathLength: "100"})) {
        rect.setAttribute(name, value);
      }
      svg.appendChild(rect);
      outline = rect;
      toast.classList.add("has-countdown");
      toast.appendChild(svg);
    }
  }
  const resetCountdown = () => {
    if (!outline) return;
    countdown?.cancel();
    countdown = null;
    outline.style.strokeDashoffset = String(-100 * (1 - remaining / duration));
  };
  const pause = () => {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
      remaining = Math.max(0, remaining - (performance.now() - startedAt));
      countdown?.pause();
    }
    if (leaving && !manuallyClosed && exitTimer !== null) {
      clearTimeout(exitTimer);
      exitTimer = null;
      leaving = false;
      remaining = Math.max(remaining, 1000);
      resetCountdown();
      toast.classList.remove("is-leaving");
      toast.classList.add("is-restored");
    }
  };
  const dismiss = (manual = false) => {
    if (leaving) {
      if (manual) manuallyClosed = true;
      return;
    }
    pause();
    leaving = true;
    manuallyClosed = manual;
    if (prefersReducedMotion()) {
      toast.remove();
      return;
    }
    toast.classList.add("is-leaving");
    exitTimer = setTimeout(() => {
      exitTimer = null;
      toast.remove();
    }, 220);
  };
  const schedule = () => {
    if (leaving || timer !== null || toast.matches?.(":hover, :focus-within")) return;
    startedAt = performance.now();
    if (outline) {
      resetCountdown();
      countdown = outline.animate([{strokeDashoffset: outline.style.strokeDashoffset}, {strokeDashoffset: "-100"}],
        {duration: remaining, easing: "linear", fill: "forwards"});
    }
    timer = setTimeout(() => {
      timer = null;
      remaining = 0;
      if (!toast.matches?.(":hover, :focus-within")) dismiss();
    }, remaining);
  };
  close.addEventListener("click", () => dismiss(true));
  toast.addEventListener("mouseenter", pause);
  toast.addEventListener("mouseleave", schedule);
  toast.addEventListener("focusin", pause);
  toast.addEventListener("focusout", () => queueMicrotask(schedule));
  toast.insertBefore(text, toast.children[0] || null);
  toast.insertBefore(close, toast.children[1] || null);
  region.insertBefore(toast, region.children[0] || null);
  region.scrollTop = 0;
  schedule();
}
function feedbackError(error, prefix = "操作失败") {
  showFeedback(`${prefix}: ${error?.message || "未知错误"}`, "error");
}
let pending = null;
let revision = 0;
let auditCursor = 0;
let auditScope = "";
let outputRevision = 0;
let saving = false;
const ruleTogglesInFlight = new Set();
let recordsCursor = null;
let recordsRevision = 0;
let rulesRevision = 0;
let knownGroups = [];
let knownPlatforms = [];
let availableGroups = [];
let availableGroupsRevision = 0;
let availableGroupsLoading = false;
let availableGroupSaving = false;
let availableGroupNamePending = false;
let selectedScope = {platform_id: "", group_id: ""};
let groupsLoaded = false;
let groupsLoadFailed = false;
let runtimeStatus = null;
let groupSummaries = new Map();
let groupSummaryFailures = new Set();
let allGroupsRevision = 0;
let allGroupsLoading = false;
let availableProviders = [];
let stateSave = Promise.resolve();
let stateRevision = 0;
let summaryRevision = 0;
let loadedRecords = [];
let currentFilter = "all";
let currentExecutionFilter = "all";
let recordSearch = "";
let searchTimer = null;
let recordSort = "time_desc";
const TAB_NAMES = ["status", "records", "rules", "groups", "settings"];
let activeTab = "status";
let tabInteracted = false;
let tabDataReady = false;
let tabSave = Promise.resolve();
let tabRevision = 0;
let actionFollow = null;
let refreshTimer = null;
let autoRefreshing = false;
let resyncPending = false;
let streamId = null;
let streamGeneration = 0;
let streamFailures = 0;
let streamLastSeen = 0;
let streamWatchdog = null;
let reconnectTimer = null;
let retryTimer = null;
let summaryTimer = null;
let liveMode = "connecting";
const recordNodes = new Map();
const streamChanges = new Map();
const expandedMessages = new Set();
let currentSettings = null;
let settingsLoadFailed = false;
let quickStartCompleted = null;
let quickStartStateFailed = false;
let quickStartSaving = false;
let quickStartSaveFailed = false;
let openGuideGroupAfterAdd = false;
let groupSettingsSaving = false;
let groupTemplateDraftInitialized = false;
let groupReasonDraftInitialized = false;
let settingsSaveQueue = Promise.resolve();
const groupDrafts = new Map();
const groupRows = new Map();
let expandedGroupKey = null;
const ruleDescriptions = [];
let groupListMessage = true;
const DEFAULT_NOTICE_TEMPLATE = "有消息触发了规则 {rule_id}, 但目标是管理员/机器人, {action} 无法执行";
let editingScope = null;
let expandedRuleCard = null;
let historyRevision = 0;
const nameLookups = new Set();

const panelTransitions = new WeakMap();
const detailsTransitions = new WeakMap();
const disclosureTiming = {duration: 260, easing: "cubic-bezier(.4, 0, .2, 1)", fill: "both"};

function canAnimateDisclosure(element) {
  return !prefersReducedMotion() && element?.isConnected === true &&
    typeof element.animate === "function" && typeof element.getBoundingClientRect === "function" &&
    typeof window.getComputedStyle === "function";
}
function panelFrame(panel) {
  const style = window.getComputedStyle(panel);
  return {height: `${panel.getBoundingClientRect().height}px`, opacity: style.opacity,
    paddingTop: style.paddingTop, paddingBottom: style.paddingBottom,
    marginTop: style.marginTop, marginBottom: style.marginBottom,
    borderTopWidth: style.borderTopWidth, borderBottomWidth: style.borderBottomWidth};
}
function collapsedFrame() {
  return {height: "0px", opacity: "0", paddingTop: "0px", paddingBottom: "0px",
    marginTop: "0px", marginBottom: "0px", borderTopWidth: "0px", borderBottomWidth: "0px"};
}
function cancelPanelTransition(panel) {
  const state = panelTransitions.get(panel);
  if (!state) return null;
  panelTransitions.delete(panel);
  state.animation.cancel();
  panel.style.overflow = state.overflow;
  panel.style.minHeight = state.minHeight;
  panel.inert = state.inert;
  return state;
}
function setPanelVisibility(panel, visible, {animate = true, force = false, onHidden = null, fromFrame = null} = {}) {
  const active = panelTransitions.get(panel);
  if (active?.visible === visible && !force) return;
  if (!active && !force && panel.hidden === !visible) return;
  const previousFrame = fromFrame || (active && canAnimateDisclosure(panel) ? panelFrame(panel) : null);
  cancelPanelTransition(panel);
  if (visible) panel.hidden = false;
  const finishImmediately = () => {
    panel.hidden = !visible;
    if (!visible) onHidden?.();
  };
  if (!animate || !canAnimateDisclosure(panel)) { finishImmediately(); return; }
  const naturalFrame = panelFrame(panel);
  if (!parseFloat(naturalFrame.height)) { finishImmediately(); return; }
  if (fromFrame && !active && Math.abs(parseFloat(fromFrame.height) - parseFloat(naturalFrame.height)) < 1) return;
  const state = {visible, overflow: panel.style.overflow, minHeight: panel.style.minHeight, inert: panel.inert};
  panel.style.overflow = "hidden";
  panel.style.minHeight = "0";
  panel.inert = visible ? state.inert : true;
  const start = previousFrame || (visible ? collapsedFrame() : naturalFrame);
  const end = visible ? naturalFrame : collapsedFrame();
  state.animation = panel.animate([start, end], disclosureTiming);
  panelTransitions.set(panel, state);
  state.animation.onfinish = () => {
    if (panelTransitions.get(panel) !== state) return;
    cancelPanelTransition(panel);
    panel.hidden = !visible;
    if (!visible) onHidden?.();
  };
}
function updateVisiblePanel(panel, update) {
  const before = canAnimateDisclosure(panel) && !panel.hidden ? panelFrame(panel) : null;
  update();
  if (before) setPanelVisibility(panel, true, {force: true, fromFrame: before});
}
function setDetailsOpen(details, open, animate = false) {
  const active = detailsTransitions.get(details);
  if (active?.open === open && animate) return;
  if (!active && Boolean(details.open) === open) {
    details.querySelector?.(":scope > summary")?.setAttribute("aria-expanded", String(open));
    return;
  }
  const start = canAnimateDisclosure(details) ? details.getBoundingClientRect().height : 0;
  if (active) {
    detailsTransitions.delete(details);
    active.animation.cancel();
    details.style.overflow = active.overflow;
    for (const [child, inert] of active.children) child.inert = inert;
  }
  const summary = details.querySelector?.(":scope > summary");
  summary?.setAttribute("aria-expanded", String(open));
  details.classList?.toggle?.("is-closing", false);
  if (!animate || !summary || !canAnimateDisclosure(details)) { details.open = open; return; }
  details.open = true;
  const style = window.getComputedStyle(details);
  const number = (value) => Number.parseFloat(value) || 0;
  const closedHeight = summary.getBoundingClientRect().height + number(style.paddingTop) + number(style.paddingBottom) +
    number(style.borderTopWidth) + number(style.borderBottomWidth) +
    number(window.getComputedStyle(summary).marginTop) + number(window.getComputedStyle(summary).marginBottom);
  const openHeight = details.getBoundingClientRect().height;
  if (!openHeight || Math.abs(openHeight - closedHeight) < 1) { details.open = open; return; }
  const state = {open, overflow: details.style.overflow,
    children: Array.from(details.children).filter((child) => child !== summary).map((child) => [child, child.inert])};
  details.style.overflow = "hidden";
  details.classList.toggle("is-closing", !open);
  for (const [child, inert] of state.children) child.inert = open ? inert : true;
  state.animation = details.animate([{height: `${start}px`}, {height: `${open ? openHeight : closedHeight}px`}], disclosureTiming);
  detailsTransitions.set(details, state);
  state.animation.onfinish = () => {
    if (detailsTransitions.get(details) !== state) return;
    detailsTransitions.delete(details);
    details.open = open;
    details.style.overflow = state.overflow;
    details.classList.toggle("is-closing", false);
    for (const [child, inert] of state.children) child.inert = inert;
    state.animation.cancel();
  };
}
document.addEventListener("click", (event) => {
  const summary = event.target?.closest?.("summary");
  const details = summary?.parentElement;
  if (!details || details.tagName !== "DETAILS" || event.defaultPrevented || event.button !== 0 ||
      event.target.closest?.("a, button, input, select, textarea")) return;
  event.preventDefault();
  setDetailsOpen(details, !(detailsTransitions.get(details)?.open ?? details.open), true);
});

function updateFilterSummary() {
  const active = currentFilter !== "all" || currentExecutionFilter !== "all" || recordSort !== "time_desc";
  el("filter-summary").textContent = active ? "筛选与排序 (已调整)" : "筛选与排序";
}
if (typeof window.matchMedia === "function") {
  const compactFilters = window.matchMedia("(max-width: 700px)");
  setDetailsOpen(el("filter-options"), !compactFilters.matches);
  compactFilters.addEventListener("change", (event) => { setDetailsOpen(el("filter-options"), !event.matches); });
}
function rememberTab(name) {
  tabInteracted = true;
  const currentRevision = ++tabRevision;
  tabSave = tabSave.catch(() => {}).then(async () => {
    try {
      await bridge.ready();
      if (currentRevision !== tabRevision) return;
      await bridge.apiPost("ui-state", {active_tab: name});
    } catch {
      if (currentRevision === tabRevision) showFeedback("无法保存上次打开的页面.", "error");
    }
  });
}
function selectTab(name, {remember = true, updateData = true} = {}) {
  if (!TAB_NAMES.includes(name)) return;
  if (name !== "rules") stopActionFollow();
  if (name !== "groups") openGuideGroupAfterAdd = false;
  activeTab = name;
  if (remember) rememberTab(name);
  const titles = {
    records: ["判定记录", "查看所选群的判定和执行结果."],
    rules: ["群规则", "管理所选群的规则和处理动作."],
    groups: ["群管理", "管理各群的监控, 名称和处理方式."],
    status: ["运行状态", "查看所有群的监控状态和统计."],
    settings: ["监控设置", "设置全局默认和运行限制."],
  };
  el("page-title").textContent = titles[name][0];
  el("page-description").textContent = titles[name][1];
  el("live-status").hidden = name !== "records";
  el("refresh-group").hidden = name !== "records";
  el("records-actions").hidden = name !== "records";
  el("new-rule").hidden = name !== "rules";
  el("group-picker").hidden = name === "groups" || name === "settings";
  for (const tab of TAB_NAMES) {
    el(`tab-${tab}`).setAttribute("aria-selected", String(tab === name));
    el(`panel-${tab}`).hidden = tab !== name;
  }
  el("tab-loading").hidden = true;
  el("page-content").hidden = false;
  if (updateData && tabDataReady) {
    if (name === "records") startLive(); else stopLive();
    if (name === "status") loadAllGroupSummaries();
    if (name === "groups" && groupsLoaded) refreshGroupCounts();
  }
}
el("records-execution-filter").addEventListener("change", () => {
  currentExecutionFilter = el("records-execution-filter").value;
  updateFilterSummary();
  loadedRecords.sort(compareRecords);
  renderRecords(loadedRecords);
});
el("records-search").addEventListener("input", () => {
  if (searchTimer !== null) clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    searchTimer = null;
    recordSearch = el("records-search").value.trim().toLocaleLowerCase();
    renderRecords(loadedRecords);
  }, 200);
});
el("records-sort").addEventListener("change", () => {
  recordSort = el("records-sort").value;
  updateFilterSummary();
  loadedRecords.sort(compareRecords);
  renderRecords(loadedRecords);
});
for (const tab of TAB_NAMES) {
  el(`tab-${tab}`).addEventListener("click", () => selectTab(tab));
  el(`tab-${tab}`).addEventListener("keydown", (event) => {
    const tabs = TAB_NAMES;
    const index = tabs.indexOf(tab);
    const next = event.key === "ArrowRight" || event.key === "ArrowDown" ? (index + 1) % tabs.length :
      event.key === "ArrowLeft" || event.key === "ArrowUp" ? (index + tabs.length - 1) % tabs.length :
      event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : -1;
    if (next < 0) return;
    event.preventDefault();
    selectTab(tabs[next]);
    el(`tab-${tabs[next]}`).focus();
  });
}
el("records-decision-filter").addEventListener("change", () => {
  currentFilter = el("records-decision-filter").value;
  updateFilterSummary();
  loadedRecords.sort(compareRecords);
  renderRecords(loadedRecords);
});

function updateFilterCounts(summary) {
  const counts = summary?.status_counts || {};
  for (const [id, label, value] of [
    ["decision-option-all", "全部", summary?.total_messages],
    ["execution-option-all", "全部", summary?.total_messages],
    ["decision-option-no_action", "未违反规则", summary ? (counts.no_action || 0) : null],
    ["decision-option-skipped", "跳过", summary ? (counts.skipped || 0) : null],
  ]) {
    const option = document.getElementById(id);
    if (option) option.textContent = value == null ? label : `${label} (${value})`;
  }
}

function group() {
  return {...selectedScope};
}
function allGroups() { return !selectedScope.platform_id && !selectedScope.group_id; }
function scopeParams() { return allGroups() ? {all_groups: "true"} : group(); }
function recordScope(row) {
  return {platform_id: row.platform_id ?? group().platform_id, group_id: row.group_id ?? group().group_id};
}
function recordKey(row) { const scope = recordScope(row); return JSON.stringify([scope.platform_id, scope.group_id, row.message_id]); }
function clearPreview() {
  revision += 1;
  pending = null;
  if (!saving) el("save-status").textContent = "";
}
function actionModeLabel(mode, globalMode = currentSettings?.default_action_mode) {
  if (mode === "inherit") return `全局(${globalMode === "auto" ? "自动" : globalMode === "confirm" ? "人工确认" : "不可用"})`;
  return mode === "auto" ? "自动" : mode === "confirm" ? "人工确认" : "执行方式未知";
}
function showError(target, error) {
  target.textContent = `操作失败: ${error.message || "未知错误"}`;
}
for (const id of ["rule", "version", "priority", "text", "enabled"]) {
  const onChange = () => {
    clearPreview();
    if (id === "rule") {
      outputRevision += 1;
      auditCursor = 0;
      auditScope = "";
      el("audit-next").disabled = true;
      el("output").textContent = "";
    }
  };
  el(id).addEventListener("input", onChange);
  el(id).addEventListener("change", onChange);
}
async function restoreUiState() {
  try {
    const saved = await bridge.apiGet("ui-state");
    if (!stateRevision && saved && typeof saved.platform_id === "string" && typeof saved.group_id === "string") {
      selectedScope = {platform_id: saved.platform_id, group_id: saved.group_id};
    }
    if (!tabInteracted) {
      const tab = TAB_NAMES.includes(saved?.active_tab) ? saved.active_tab : "status";
      selectTab(tab, {remember: false, updateData: false});
    }
  } catch {
    if (!tabInteracted) selectTab("status", {remember: false, updateData: false});
    el("selection-status").textContent = "无法读取上次选择.";
  }
}
await bridge.ready();
const uiStateRead = restoreUiState();
async function refreshStatus() {
 try {
  const status = await bridge.apiGet("status");
  runtimeStatus = status;
  const startErrors = {
    model_not_configured: "监控未启动: 请先选择默认判定模型.",
    invalid_configuration: "监控未启动: 插件配置不合法, 请检查监控设置.",
    lock_in_use: "监控未启动: 队列锁已被占用, 请检查是否有另一个实例运行.",
    start_failed: "监控未启动: 启动失败, 请查看 AstrBot 日志."
  };
  const running = status.monitoring === "dry_run";
  const backendLabel = ({llm: "普通 LLM", jev: "Jev", none: "未选择"})[status.backend] || status.backend || "未选择";
   el("status").setAttribute("data-tone", running ? "success" : "warning");
   el("status").textContent = running ? "运行中" : "未运行";
   el("status-main").setAttribute("data-tone", running ? "success" : "warning");
   el("status-label").textContent = running ? "监控运行中" : "监控未启动";
   el("status-backend").textContent = backendLabel;
   el("status-execution").textContent = status.action_execution_available ? "可执行" : status.action_execution_enabled ? "不可用" : "未开启";
   el("status-detail").textContent = running
     ? status.action_execution_available ? "消息判定和真实执行已开启. 各群按设置处理规则动作." : status.action_execution_enabled ? "消息判定已开启, 真实执行不可用." : "消息判定已开启, 仅记录结果."
    : status.start_error || status.monitoring === "invalid_configuration"
      ? `${startErrors[status.start_error || "invalid_configuration"] || "监控未启动: 请查看 AstrBot 日志."} 判定后端: ${backendLabel}.`
      : `监控未启动. 判定后端: ${backendLabel}. 请检查监控设置.`;
  } catch (error) { runtimeStatus = null; showError(el("status-detail"), error); el("status").setAttribute("data-tone", "warning"); el("status").textContent = "状态不可用"; el("status-label").textContent = "状态不可用"; el("status-main").setAttribute("data-tone", "warning"); el("status-backend").textContent = "-"; el("status-execution").textContent = "-"; }
  renderGroupList();
  renderAllGroupStatus();
  if (currentSettings) updateGroupSettingsHints();
}
const statusRead = refreshStatus();
el("status-settings").addEventListener("click", () => selectTab("settings"));
async function showQuery(endpoint, params) {
  const requestRevision = ++outputRevision;
  el("audit-next").disabled = true;
  el("output").textContent = "";
  try {
    const data = await bridge.apiGet(endpoint, params);
    if (requestRevision === outputRevision) el("output").textContent = JSON.stringify(data, null, 2);
  } catch (error) { if (requestRevision === outputRevision) showError(el("output"), error); }
}
el("platforms").addEventListener("click", () => showQuery("platforms"));
el("load").addEventListener("click", () => showQuery("rules", group()));
el("history").addEventListener("click", async () => {
  const requestRevision = ++outputRevision;
  el("audit-next").disabled = true;
  el("output").textContent = "";
  try {
    const scope = {...group(), rule_id: el("rule").value.trim()};
    if (!scope.platform_id || !scope.group_id || !scope.rule_id) throw new Error("请填写平台, 群和规则标识");
    const history = await bridge.apiGet("rules/history", scope);
    if (requestRevision === outputRevision) el("output").textContent = JSON.stringify({scope, history}, null, 2);
  } catch (error) { if (requestRevision === outputRevision) showError(el("output"), error); }
});
async function loadAudit(reset) {
  const requestRevision = ++outputRevision;
  const current = group();
  const scope = JSON.stringify(current);
  if (reset || scope !== auditScope) auditCursor = 0;
  auditScope = scope;
  el("audit-next").disabled = true;
  el("output").textContent = "";
  try {
    const rows = await bridge.apiGet("audit", {...current, limit: 50, after_id: auditCursor});
    if (requestRevision !== outputRevision) return;
    el("output").textContent = JSON.stringify(rows, null, 2);
    if (rows.length) auditCursor = rows[rows.length - 1].id;
    el("audit-next").disabled = rows.length < 50;
  } catch (error) { if (requestRevision === outputRevision) showError(el("output"), error); }
}
el("audit").addEventListener("click", () => loadAudit(true));
el("audit-next").addEventListener("click", () => loadAudit(false));
function localTimeFor(value) {
  const timestamp = String(value ?? "");
  const date = new Date(timestamp);
  const pad = (part) => String(part).padStart(2, "0");
  return Number.isNaN(date.getTime()) ? timestamp || "-" :
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}
function compactTime(value) {
  const full = localTimeFor(value);
  return /^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$/.test(full) ? full.slice(5, 16) : full;
}
function formatDuration(seconds) {
  if (!Number.isSafeInteger(seconds) || seconds <= 0) return "-";
  let rest = seconds;
  const parts = [];
  for (const [unit, label] of [[86400, "天"], [3600, "小时"], [60, "分钟"], [1, "秒"]]) {
    const count = Math.floor(rest / unit);
    if (count) parts.push(`${count}${label}`);
    rest %= unit;
  }
  return parts.join(" ");
}
const recordStatuses = {proposed: ["拟执行", "warning"], no_action: ["未违反规则", "muted"],
  skipped: ["跳过", "muted"], review: ["需复核", "warning"], failed: ["失败", "error"],
  timed_out: ["超时", "error"], rejected: ["已拒绝", "warning"], started: ["处理中", "muted"]};
function recordStatus(row) {
  if (row.status !== "proposed") return recordStatuses[row.status] || [String(row.status ?? "-"), "muted"];
  if (allProtectedTargetFailures(row)) return ["全部失败", "error"];
  if (row.execution_summary) {
    const labels = {succeeded: ["全部成功", "success"], partial_failed: ["部分失败", "warning"],
      failed: ["全部失败", "error"], blocked: ["全部无法执行", "warning"], unknown: ["结果不明", "warning"],
      pending_confirm: ["待确认", "warning"], executing: ["执行中", "warning"], inactive: ["拟执行", "warning"]};
    return labels[row.execution_summary] || ["状态未知", "warning"];
  }
  const execution = row.execution;
  if (!execution || execution.status === "inactive") return ["拟执行", "warning"];
  if (execution.action === "recall") {
    if (execution.status === "succeeded") return ["已撤回", "success"];
    if (execution.status === "blocked" && execution.reason_code === "recall_expired") return ["已超时不可撤回", "warning"];
    if (execution.status === "blocked" && execution.reason_code === "protected_target") return ["无法撤回", "warning"];
    if (execution.status === "failed") return ["撤回失败", "error"];
  }
  if (execution.status === "failed" && String(execution.platform_code) === "40103004") return ["无法执行", "warning"];
  const labels = {pending_auto: ["待执行", "warning"], pending_confirm: ["待确认", "warning"],
    pending_retry: ["执行中", "warning"], executing: ["执行中", "warning"], succeeded: ["已禁言", "success"],
    blocked: ["无法执行", "warning"], permission_denied: ["无法执行", "warning"], denied: ["无法执行", "warning"],
    unsupported: ["无法执行", "warning"], failed: ["执行失败", "error"], unknown: ["结果不明", "warning"],
    review: ["需复核", "warning"]};
  return labels[execution.status] || ["状态未知", "warning"];
}
function recordRules(decision, status) {
  if (status === "no_action") return {text: "-", title: ""};
  const proposal = decision.proposal || {};
  const entries = proposal.rule_id ? [{rule_id: proposal.rule_id, version: proposal.rule_version}]
    : Array.isArray(decision.rules) ? decision.rules.filter((rule) => rule && typeof rule.rule_id === "string" && rule.rule_id) : [];
  if (!entries.length) return {text: "-", title: ""};
  const format = (rule, shorten) => {
    const chars = Array.from(String(rule.rule_id));
    const id = shorten && chars.length > 24 ? `${chars.slice(0, 21).join("")}...` : chars.join("");
    return `${id} / ${rule.version ?? "-"}`;
  };
  const shown = entries.length > 2 ? `${format(entries[0], true)} 等 ${entries.length} 条` : entries.map((rule) => format(rule, true)).join(", ");
  return {text: shown, title: entries.map((rule) => format(rule, false)).join(", ")};
}
function recordDecision(row) {
  let detail;
  try { detail = JSON.parse(row.detail); } catch { detail = {}; }
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) detail = {};
  const decision = detail.decision && typeof detail.decision === "object" ? detail.decision : detail;
  return {detail, decision, proposal: decision.proposal || {}};
}
const actionNames = {mute: "禁言", reply: "发言", recall: "撤回"};
const actionStatusNames = {inactive: "未执行", pending_auto: "待执行", pending_confirm: "待确认",
  pending_retry: "等待重试", executing: "执行中", succeeded: "已执行", blocked: "无法执行",
  permission_denied: "权限不足", denied: "权限不足", unsupported: "不支持", failed: "执行失败",
  unknown: "结果不明", review: "需复核"};
function recordExecutions(row) {
  const items = Array.isArray(row.executions) && row.executions.length ? row.executions : row.execution ? [row.execution] : [];
  return items.filter((item) => item && typeof item === "object" && !Array.isArray(item));
}
function isProtectedTargetFailure(execution) {
  return ["blocked", "failed"].includes(execution?.status) &&
    (execution.reason_code === "protected_target" || (!execution.reason_code && String(execution.platform_code) === "40103004"));
}
function allProtectedTargetFailures(row) {
  if (row.execution_summary && !["blocked", "failed", "partial_failed"].includes(row.execution_summary)) return false;
  const executions = recordExecutions(row);
  return executions.length > 0 && executions.every(isProtectedTargetFailure);
}
function recordActions(row, decision, proposal) {
  const executions = recordExecutions(row);
  if (executions.length) {
    const actions = executions.map((execution) => ({kind: execution.action || (executions.length === 1 ? proposal.action : ""),
      duration: execution.duration_seconds ?? proposal.duration_seconds, mode: execution.mode, execution})).filter((action) => typeof action.kind === "string" && action.kind);
    if (actions.length) return actions;
  }
  if (proposal.action) return [{kind: proposal.action, duration: proposal.duration_seconds, mode: proposal.mode}];
  const rules = Array.isArray(decision.rules) ? decision.rules : [];
  const rule = rules.find((item) => item && item.rule_id === proposal.rule_id &&
    Number.isSafeInteger(proposal.rule_version) && item.version === proposal.rule_version);
  return Array.isArray(rule?.actions) ? rule.actions.filter((item) => item && typeof item.type === "string")
    .map((item) => ({kind: item.type, duration: proposal.duration_seconds, mode: item.mode})) : [];
}
function describeRecordAction(action) {
  const name = Object.hasOwn(actionNames, action.kind) ? actionNames[action.kind] : String(action.kind);
  const duration = action.kind === "mute" ? formatDuration(action.duration) : "-";
  return duration === "-" ? name : `${name} ${duration}`;
}
const failureStates = new Set(["blocked", "permission_denied", "denied", "unsupported", "failed", "unknown", "review"]);
function executionFailureReason(execution) {
  const reasons = {protected_target: "目标是管理员或机器人", permission_denied: "机器人权限不足或核验失败",
    recall_expired: "消息已超过可撤回时间", invalid_message: "消息标识无效", quote_expired: "引用消息已过期",
    group_not_monitored: "本群监控未开启", rule_disabled: "规则已停用", rule_version_changed: "规则版本已变化",
    rule_scope_mismatch: "规则与当前群不匹配", action_not_allowed: "规则不再允许此动作", invalid_duration: "禁言时长无效或超过上限",
    duration_not_in_rule: "禁言时长不在规则设置中", intent_expired: "动作已过期", invalid_expiry: "动作到期时间无效"};
  let reason = Object.hasOwn(reasons, execution.reason_code) ? reasons[execution.reason_code] : "";
  if (!reason && String(execution.platform_code) === "40103004") reason = "目标是管理员或机器人";
  if (!reason && execution.status === "unsupported") reason = "当前版本不支持该动作";
  if (!reason && ["permission_denied", "denied"].includes(execution.status)) reason = "机器人权限不足或核验失败";
  if (!reason && execution.status === "unknown") reason = "执行结果不明, 请人工核查";
  if (!reason && execution.status === "review") reason = "执行前检查未通过, 需要人工复核";
  if (!reason && execution.status === "inactive") reason = "动作未执行";
  if (!reason) reason = "未提供具体原因";
  return reason;
}
function recordFailureReason(row, detail, decision, actions) {
  const label = recordStatus(row)[0];
  if (!["全部失败", "部分失败", "全部无法执行", "无法执行", "无法撤回", "已超时不可撤回",
    "执行失败", "撤回失败", "失败", "已拒绝", "超时", "结果不明", "需复核"].includes(label)) return "";
  const failures = actions.filter((action) => action.execution &&
    (failureStates.has(action.execution.status) || (label === "部分失败" && action.execution.status === "inactive")));
  if (failures.length) return failures.map((action) => `${describeRecordAction(action)}: ${executionFailureReason(action.execution)}`).join("\n");
  if (row.execution && failureStates.has(row.execution.status)) return executionFailureReason(row.execution);
  const value = detail.review_reason || decision.review_reason || decision.validation ||
    (typeof row.detail === "string" && !row.detail.startsWith("{") ? row.detail : "");
  const reasons = {decision_error: "模型判定失败", decision_timeout: "模型判定超时", context_length_exceeded: "内容超过模型处理限制",
    evaluation_audit_unavailable: "无法保存判定记录", rule_snapshot_too_large: "规则信息超出记录大小限制",
    invalid_message_snapshot: "消息信息不完整", invalid_decider_result: "模型判定结果无效",
    invalid_decider_status: "模型判定状态无效", audit_detail_invalid_or_too_large: "判定记录无效或超出大小限制",
    invalid_assessment: "模型返回的规则判定无效", invalid_assessment_fields: "模型返回的规则判定不完整",
    invalid_parameters: "模型返回的数据未通过校验", invalid_severity: "模型返回的严重级别无效",
    invalid_duration: "禁言时长无效或超过上限", unknown_rule: "模型选择的规则不存在",
    unsupported_jev_rule_policy: "当前 Jev 模型不支持此规则的禁言时长模式",
    rule_snapshot_changed: "判定期间规则已变化", rule_snapshot_changed_at_commit: "判定期间规则已变化"};
  return Object.hasOwn(reasons, value) ? reasons[value] : value && !/^[a-z0-9_]+$/.test(String(value))
    ? String(value) : "暂无具体原因, 请查看原始记录.";
}
function recordElement(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}
function addRecordDetail(container, title, value, fallback = false) {
  const section = recordElement("section", "record-detail-section");
  section.appendChild(recordElement("h3", "", title));
  section.appendChild(recordElement("p", fallback ? "record-detail-value record-reason-fallback" : "record-detail-value", value));
  container.appendChild(section);
}
function decisionBucket(row) {
  if (row.status === "no_action" || row.status === "skipped") return row.status;
  const labels = {"全部成功": "succeeded", "部分失败": "partial_failed", "全部失败": "failed", "全部无法执行": "blocked", "拟执行": "proposed", "已禁言": "succeeded", "已撤回": "recalled", "无法执行": "blocked", "无法撤回": "recall_blocked", "已超时不可撤回": "recall_expired", "执行失败": "failed", "撤回失败": "recall_failed",
    "结果不明": "unknown", "待确认": "pending_confirm"};
  return row.status === "proposed" ? labels[recordStatus(row)[0]] || "other" : "other";
}
function executionBucket(row) {
  if (allProtectedTargetFailures(row)) return "failed";
  if (row.execution_summary) return row.execution_summary === "inactive" ? "none" : row.execution_summary;
  const execution = row.execution;
  if (!execution || execution.status === "inactive") return "none";
  if (execution.action === "recall") {
    if (execution.status === "succeeded") return "recalled";
    if (execution.status === "failed") return "recall_failed";
    if (execution.status === "blocked") return execution.reason_code === "recall_expired" ? "recall_expired" : "recall_blocked";
  }
  if (execution.status === "failed" && String(execution.platform_code) === "40103004") return "blocked";
  if (["blocked", "permission_denied", "denied", "unsupported"].includes(execution.status)) return "blocked";
  return execution.status;
}
function recordReason(row, detail, decision, proposal) {
  const supplied = proposal.reason || decision.reason;
  if (typeof supplied === "string" && supplied.trim()) return {text: supplied, fallback: false};
  if (row.status === "no_action") return {text: decision.no_action_reason_enabled === false
    ? "未违反规则, 已关闭理由记录." : "未违反规则, 模型未提供具体理由.", fallback: true};
  const value = String(detail.review_reason || decision.review_reason || decision.validation ||
    (typeof row.detail === "string" && !row.detail.startsWith("{") ? row.detail : ""));
  const nonText = row.status === "skipped" && value === "non_text_or_unknown";
  return {text: nonText ? "消息包含非文本内容或类型无法识别." : value, fallback: nonText};
}
function matchesRecord(row) {
  if (currentFilter !== "all" && decisionBucket(row) !== currentFilter) return false;
  if (currentExecutionFilter !== "all" && executionBucket(row) !== currentExecutionFilter) return false;
  if (!recordSearch) return true;
  const {detail, decision, proposal} = recordDecision(row);
  const ruleIds = Array.isArray(decision.rules) ? decision.rules.map((item) => item?.rule_id || "") : [];
  const reason = recordReason(row, detail, decision, proposal).text;
  const scope = recordScope(row);
  const item = knownGroups.find((candidate) => groupKey(candidate) === groupKey(scope));
  return [row.message_id, proposal.rule_id, ...ruleIds, reason, scope.platform_id, scope.group_id, item && groupName(item)].some((value) => String(value ?? "").toLocaleLowerCase().includes(recordSearch));
}
function compareRecords(left, right) {
  const latency = (row) => {
    const value = recordDecision(row).decision.model_latency_ms;
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  };
  const time = (row) => { const value = Date.parse(row.created_at); return Number.isFinite(value) ? value : null; };
  const a = recordSort.startsWith("latency_") ? latency(left) : time(left);
  const b = recordSort.startsWith("latency_") ? latency(right) : time(right);
  if (a === null && b !== null) return 1;
  if (b === null && a !== null) return -1;
  return (a !== null && b !== null ? (a - b) * (recordSort.endsWith("_asc") ? 1 : -1) : 0) || right.id - left.id;
}
function renderRecords(rows, append = false, update = false) {
  const body = el("records-body");
  if (!append && !update) { body.textContent = ""; recordNodes.clear(); }
  for (const row of rows) {
    const key = recordKey(row);
    const old = recordNodes.get(key);
    if (!matchesRecord(row)) {
      if (update && old) { old[0].remove(); old[1].remove(); recordNodes.delete(key); }
      continue;
    }
    const {detail, decision, proposal} = recordDecision(row);
    const rules = recordRules(decision, row.status);
    const execution = row.execution;
    const noticeLabels = {pending: "等待发送提示", sending: "正在发送提示", pending_retry: "等待重试提示",
      succeeded: "已发送提示", failed: "提示发送失败", unknown: "提示发送结果不明", inactive: "提示未发送", review: "提示需人工核查"};
    const downgradeLabels = {passive_expired: "被动回复已过期, 已保留引用", reference_rejected: "引用被拒, 已改为不引用发送", markdown_rejected: "已降级为纯文本"};
    const executions = recordExecutions(row);
    const actions = recordActions(row, decision, proposal);
    const failureReason = recordFailureReason(row, detail, decision, actions);
    const {text: fullReason, fallback: fallbackReason} = recordReason(row, detail, decision, proposal);
    const reason = fullReason ? fullReason.slice(0, 100) : "-";
    const latency = typeof decision.model_latency_ms === "number" ? `${decision.model_latency_ms} ms` : "-";
    const tr = document.createElement("tr");
    tr.className = "record";
    tr.tabIndex = 0;
    tr.setAttribute("aria-expanded", "false");
    const timestamp = String(row.created_at ?? "");
    const localTime = localTimeFor(timestamp);
    const messageId = String(row.message_id ?? "-");
    const rowScope = recordScope(row);
    const rowGroup = knownGroups.find((candidate) => groupKey(candidate) === groupKey(rowScope));
    const groupLabel = groupName(rowGroup || rowScope);
    tr.setAttribute("aria-label", allGroups() ? `查看 ${groupLabel} 的消息 ${messageId} 的详情` : `查看消息 ${messageId} 的详情`);
    for (const [index, value] of [compactTime(timestamp), row.status, reason, rules.text, latency].entries()) {
      const cell = document.createElement("td");
      cell.setAttribute("data-label", ["时间", "结果", "理由摘要", "规则 ID / 版本", "模型延迟"][index]);
      if (index === 1) {
        const badge = document.createElement("span");
        const [label, tone] = recordStatus(row);
        badge.className = failureReason ? "badge has-reason" : "badge";
        if (row.status === "proposed" && tone === "success") badge.className += " record-executed";
        if (label === "拟执行") badge.className += " record-planned";
        badge.setAttribute("data-tone", tone);
        if (failureReason) { badge.title = failureReason; badge.setAttribute("aria-label", `${label}: ${failureReason}`); }
        badge.textContent = label;
        cell.appendChild(badge);
      } else {
        cell.textContent = String(value ?? "-");
        if (index === 2 && fallbackReason) cell.className = "record-reason-fallback";
      }
      if (index === 4) cell.className = "number";
      if (index === 0) cell.title = localTime;
      if (index === 0 && allGroups()) {
        const label = recordElement("span", "record-group", groupLabel);
        cell.appendChild(label);
      }
      if (index === 3) cell.title = rules.title;
      tr.appendChild(cell);
    }
    const detailRow = document.createElement("tr");
    detailRow.hidden = !expandedMessages.has(key);
    const cell = document.createElement("td");
    cell.className = "record-detail-cell";
    cell.colSpan = 5;
    const content = recordElement("div", "record-details");
    const info = recordElement("div", "record-detail-info");
    addRecordDetail(info, "消息标识", messageId);
    const originalSection = recordElement("section", "record-detail-section");
    originalSection.appendChild(recordElement("h3", "", "消息原文"));
    const original = recordElement("p", "record-detail-value", "展开后读取.");
    originalSection.appendChild(original);
    info.appendChild(originalSection);
    const explainedByActions = actions.some((action) => action.execution &&
      (failureStates.has(action.execution.status) || (recordStatus(row)[0] === "部分失败" && action.execution.status === "inactive")));
    if (failureReason && !explainedByActions) addRecordDetail(info, "结果说明", failureReason);
    addRecordDetail(info, "判定理由", fullReason || "暂无理由.", fallbackReason);
    addRecordDetail(info, "相关规则", rules.title || "暂无规则信息.");
    content.appendChild(info);
    const process = recordElement("div", "record-detail-process");
    const actionSection = recordElement("section", "record-detail-section");
    actionSection.appendChild(recordElement("h3", "", "处理动作"));
    const actionList = recordElement("ol", "record-action-list");
    for (const [index, action] of actions.entries()) {
      const item = recordElement("li", "record-action");
      const heading = recordElement("div", "record-action-heading");
      heading.appendChild(recordElement("strong", "", `${index + 1}. ${describeRecordAction(action)}`));
      const state = action.execution?.status;
      const protectedFailure = isProtectedTargetFailure(action.execution);
      const planned = row.status === "proposed" && (!action.execution || (state === "inactive" && recordStatus(row)[0] === "拟执行"));
      const statusText = planned ? "拟执行" : protectedFailure ? "执行失败" : action.execution && Object.hasOwn(actionStatusNames, state) ? actionStatusNames[state]
        : action.execution ? "执行状态未知" : "无执行记录";
      const status = recordElement("span", state === "succeeded" ? "badge record-executed" : planned ? "badge record-planned" : "badge", statusText);
      status.setAttribute("data-tone", state === "succeeded" ? "success" : state === "failed" || protectedFailure ? "error"
        : planned || failureStates.has(state) || state === "pending_confirm" ? "warning" : "muted");
      heading.appendChild(status);
      if (action.mode === "confirm" || action.mode === "auto") heading.appendChild(recordElement("span", "record-detail-muted", action.mode === "confirm" ? "人工确认" : "自动"));
      item.appendChild(heading);
      if (failureStates.has(state)) item.appendChild(recordElement("p", "record-action-note", `原因: ${executionFailureReason(action.execution)}`));
      if (action.kind === "mute" && state === "succeeded" && action.execution.expires_at) {
        item.appendChild(recordElement("p", "record-action-note", `到期时间: ${localTimeFor(action.execution.expires_at)}`));
      }
      const notice = action.execution?.notice;
      if (notice) {
        const parts = [noticeLabels[notice.status] || "提示状态未知"];
        if (notice.sent_quoted != null) parts.push(notice.sent_quoted ? "已引用触发消息" : "未引用触发消息");
        if (notice.downgrade_reason) parts.push(downgradeLabels[notice.downgrade_reason] || "已降级");
        item.appendChild(recordElement("p", "record-action-note", `群提示: ${parts.join(" / ")}`));
      }
      if (action.mode === "confirm" && state === "pending_confirm") {
        const button = recordElement("button", "secondary", `执行${Object.hasOwn(actionNames, action.kind) ? actionNames[action.kind] : action.kind}`);
        button.type = "button";
        button.addEventListener("click", async () => {
          button.disabled = true;
          try {
            const result = await bridge.apiPost("execute-action", {intent_id: action.execution.id});
            const feedback = {succeeded: ["动作已执行.", "success"], failed: ["动作执行失败, 请查看记录.", "error"],
              blocked: ["动作无法执行, 请查看记录.", "error"], permission_denied: ["权限不足, 动作未执行.", "error"],
              unknown: ["执行结果不明, 请核查后再操作.", "warning"], review: ["动作需复核, 请查看记录.", "warning"],
              not_claimed: ["动作未执行, 请查看记录.", "warning"]};
            const [message, tone] = feedback[result?.status] || ["执行状态未知, 请查看记录.", "warning"];
            showFeedback(message, tone);
          } catch (error) { button.disabled = false; feedbackError(error); }
        });
        item.appendChild(button);
      }
      actionList.appendChild(item);
    }
    actionSection.appendChild(actions.length ? actionList : recordElement("p", "record-detail-muted", "暂无动作信息."));
    process.appendChild(actionSection);
    const events = Array.isArray(row.events) ? row.events : [];
    const timeline = recordElement("section", "record-detail-section");
    timeline.appendChild(recordElement("h3", "", "判定过程"));
    const eventList = recordElement("ol", "record-timeline");
    for (const event of events) eventList.appendChild(recordElement("li", "", `${localTimeFor(event.created_at)}  ${recordStatuses[event.status]?.[0] || event.status}`));
    timeline.appendChild(events.length ? eventList : recordElement("p", "record-detail-muted", "暂无过程记录."));
    process.appendChild(timeline);
    content.appendChild(process);
    let latestDetail = row.detail;
    try { latestDetail = JSON.parse(row.detail); } catch { /* Keep non-JSON details as text. */ }
    const latest = {id: row.id, message_id: row.message_id, status: row.status, created_at: row.created_at,
      attempt_id: row.attempt_id ?? null, detail: latestDetail, execution: row.execution ?? null,
      executions: Array.isArray(row.executions) ? row.executions : executions, events};
    const raw = recordElement("details", "record-raw");
    raw.appendChild(recordElement("summary", "", "查看原始记录"));
    raw.appendChild(recordElement("pre", "", JSON.stringify(latest, null, 2)));
    content.appendChild(raw);
    cell.appendChild(content);
    detailRow.appendChild(cell);
    tr.setAttribute("aria-expanded", String(!detailRow.hidden));
    let contentLoading = false;
    let contentLoaded = false;
    const contentScope = recordScope(row);
    const selectedAtRender = JSON.stringify(group());
    const loadOriginal = async () => {
      if (contentLoading || contentLoaded || !expandedMessages.has(key)) return;
      contentLoading = true;
      updateVisiblePanel(content, () => { original.textContent = "正在读取消息原文..."; });
      try {
        const result = await bridge.apiGet("message-content", {...contentScope, message_id: messageId});
        if (!expandedMessages.has(key) || selectedAtRender !== JSON.stringify(group()) || detailRow.isConnected === false) return;
        updateVisiblePanel(content, () => { original.textContent = typeof result?.content === "string" ? result.content : "原文不可用或已过期."; });
        contentLoaded = true;
      } catch {
        if (expandedMessages.has(key) && detailRow.isConnected !== false) {
          updateVisiblePanel(content, () => { original.textContent = "无法读取消息原文."; });
        }
      } finally { contentLoading = false; }
    };
    const toggle = () => {
      const expanded = !expandedMessages.has(key);
      if (expanded) {
        expandedMessages.add(key);
        detailRow.hidden = false;
        setPanelVisibility(content, true, {force: true});
        void loadOriginal();
      } else {
        expandedMessages.delete(key);
        contentLoaded = false;
        setPanelVisibility(content, false, {onHidden: () => {
          detailRow.hidden = true;
          original.textContent = "展开后读取.";
        }});
      }
      tr.setAttribute("aria-expanded", String(expanded));
    };
    tr.addEventListener("click", toggle);
    if (!detailRow.hidden) void loadOriginal();
    tr.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggle(); }
    });
    if (update) {
      if (old) { old[0].remove(); old[1].remove(); }
      const index = loadedRecords.findIndex((item) => recordKey(item) === key);
      const following = loadedRecords.slice(index + 1).map((item) => recordNodes.get(recordKey(item))).find(Boolean);
      const anchor = following ? following[0] : null;
      body.insertBefore(tr, anchor);
      body.insertBefore(detailRow, anchor);
    } else { body.appendChild(tr); body.appendChild(detailRow); }
    recordNodes.set(key, [tr, detailRow]);
  }
  const count = loadedRecords.filter(matchesRecord).length;
  el("records-match-count").textContent = `匹配 ${count} 条`;
  el("records-empty").hidden = count > 0;
  el("records-empty").textContent = loadedRecords.length || recordSearch || currentFilter !== "all" || currentExecutionFilter !== "all" ? "没有匹配的记录" : allGroups() ? "还没有判定记录." : "该群还没有判定记录.";
  const latest = loadedRecords.filter(matchesRecord).slice().sort((left, right) => {
    const a = Date.parse(left.created_at), b = Date.parse(right.created_at);
    return (Number.isFinite(a) && Number.isFinite(b) ? b - a : 0) || right.id - left.id;
  }).slice(0, 10);
  const latencies = latest.map((row) => recordDecision(row).decision.model_latency_ms)
    .filter((value) => typeof value === "number" && Number.isFinite(value) && value >= 0);
  el("stat-latency").textContent = latencies.length ? `${Math.round(latencies.reduce((sum, value) => sum + value, 0) / latencies.length)} ms` : "—";
}
async function loadRecords(reset) {
  const requestRevision = ++recordsRevision;
  const current = scopeParams();
  if (reset) { recordsCursor = null; loadedRecords = []; }
  el("records-next").disabled = true;
  el("records-status").textContent = "正在读取记录...";
  try {
    const rows = await bridge.apiGet("messages", {...current, limit: 50, ...(recordsCursor === null ? {} : {before_id: recordsCursor})});
    if (requestRevision !== recordsRevision) return;
    for (const row of rows) {
      const index = loadedRecords.findIndex((item) => recordKey(item) === recordKey(row));
      if (index === -1) loadedRecords.push(row);
      else if (loadedRecords[index].id < row.id) loadedRecords[index] = row;
    }
    loadedRecords.sort(compareRecords);
    renderRecords(loadedRecords);
    if (rows.length) recordsCursor = rows[rows.length - 1].id;
    el("records-next").disabled = rows.length < 50;
    el("records-status").textContent = rows.length ? `已加载 ${loadedRecords.length} 条消息.` : "没有更多消息.";
  } catch (error) { if (requestRevision === recordsRevision) showError(el("records-status"), error); }
}
el("records-next").addEventListener("click", () => loadRecords(false));
async function loadSummary() {
  const current = scopeParams();
  const requestRevision = ++summaryRevision;
  try {
    const summary = await bridge.apiGet("summary", current);
    if (requestRevision !== summaryRevision) return;
    el("stat-messages").textContent = String(summary.recent_24h.messages);
    el("stat-proposed").textContent = String(summary.recent_24h.proposed);
    el("stat-no-action").textContent = String(summary.recent_24h.no_action);
    updateFilterCounts(summary);
    el("summary-status").textContent = summary.truncated ? `统计仅覆盖最近 ${summary.limit} 条消息.` : "";
  } catch (error) { if (requestRevision === summaryRevision) showError(el("summary-status"), error); }
}
function scheduleRefresh() {
  if (refreshTimer !== null) clearTimeout(refreshTimer);
  refreshTimer = null;
  if (liveMode !== "polling" || document.hidden || activeTab !== "records") return;
  refreshTimer = setTimeout(async () => {
    refreshTimer = null;
    try { await refreshVisibleRecords(); } finally { scheduleRefresh(); }
  }, 5000);
  if (typeof refreshTimer?.unref === "function") refreshTimer.unref();
}
async function refreshVisibleRecords() {
  if (autoRefreshing || document.hidden || activeTab !== "records") return;
  const current = scopeParams();
  autoRefreshing = true;
  streamChanges.clear();
  const requestRevision = ++recordsRevision;
  const target = Math.max(loadedRecords.length, 50);
  const refreshed = [];
  let cursor = null;
  const previousCursor = recordsCursor;
  const previousRecords = loadedRecords.slice();
  let lastPage = [];
  try {
    do {
      lastPage = await bridge.apiGet("messages", {...current, limit: 50, ...(cursor === null ? {} : {before_id: cursor})});
      if (requestRevision !== recordsRevision) return;
      for (const row of lastPage) {
        const existing = refreshed.findIndex((item) => recordKey(item) === recordKey(row));
        if (existing < 0) refreshed.push(row); else refreshed[existing] = row;
      }
      if (lastPage.length) cursor = lastPage[lastPage.length - 1].id;
    } while (lastPage.length === 50 && (previousCursor === null ? refreshed.length < target : cursor > previousCursor));
    if (requestRevision !== recordsRevision) return;
    for (const row of previousRecords) {
      const index = refreshed.findIndex((item) => recordKey(item) === recordKey(row));
      if (index < 0) refreshed.push(row);
      else if (refreshed[index].id < row.id) refreshed[index] = row;
    }
    for (const row of streamChanges.values()) {
      const index = refreshed.findIndex((item) => recordKey(item) === recordKey(row));
      if (index < 0) refreshed.push(row);
      else if (refreshed[index].id <= row.id) refreshed[index] = row;
    }
    streamChanges.clear();
    loadedRecords = refreshed.sort(compareRecords);
    recordsCursor = cursor;
    renderRecords(loadedRecords);
    el("records-next").disabled = lastPage.length < 50;
    el("records-status").textContent = `已加载 ${loadedRecords.length} 条消息.`;
    await loadSummary();
  } catch (error) { if (requestRevision === recordsRevision) showError(el("records-status"), error); }
  finally {
    autoRefreshing = false;
    if (resyncPending) { resyncPending = false; refreshVisibleRecords(); }
  }
}
function stopLive() {
  streamGeneration++;
  for (const timer of [refreshTimer, streamWatchdog, reconnectTimer, retryTimer, summaryTimer]) if (timer !== null) clearTimeout(timer);
  refreshTimer = streamWatchdog = reconnectTimer = retryTimer = summaryTimer = null;
  const id = streamId;
  streamId = null;
  if (id !== null && bridge.unsubscribeSSE) bridge.unsubscribeSSE(id).catch(() => {});
}
function liveState(mode) {
  liveMode = mode;
  el("live-status").textContent = mode === "connected" ? "实时 · 已连接" : mode === "polling" ? "已断开, 使用轮询" : "实时 · 重连中";
  el("live-status").setAttribute("data-tone", mode === "connected" ? "success" : "warning");
}
function failLive(generation) {
  if (generation !== streamGeneration) return;
  stopLive();
  streamFailures++;
  if (streamFailures >= 2) {
    liveState("polling");
    scheduleRefresh();
    retryTimer = setTimeout(() => { streamFailures = 1; startLive(true); }, 60000);
  } else {
    liveState("connecting");
    reconnectTimer = setTimeout(() => startLive(), 0);
  }
}
function checkLive(generation) {
  if (generation !== streamGeneration) return;
  if (Date.now() - streamLastSeen > 45000) { failLive(generation); return; }
  streamWatchdog = setTimeout(() => checkLive(generation), 10000);
  if (typeof streamWatchdog?.unref === "function") streamWatchdog.unref();
}
function startLive(retry = false) {
  stopLive();
  if (document.hidden || activeTab !== "records") return;
  const scope = scopeParams();
  liveState(retry ? "polling" : "connecting");
  if (retry) scheduleRefresh();
  const generation = streamGeneration;
  const connectedAt = Date.now();
  streamLastSeen = connectedAt;
  streamWatchdog = setTimeout(() => checkLive(generation), 10000);
  if (typeof streamWatchdog?.unref === "function") streamWatchdog.unref();
  if (typeof bridge.subscribeSSE !== "function") { failLive(generation); return; }
  bridge.subscribeSSE("events", {
    onOpen() { if (generation === streamGeneration && !retry) { liveState("connected"); refreshVisibleRecords(); } },
    onError() { failLive(generation); },
    onMessage(event) {
      if (generation !== streamGeneration) return;
      streamLastSeen = Date.now();
      if (retry && liveMode === "polling") { liveState("connected"); scheduleRefresh(); refreshVisibleRecords(); }
      if (Date.now() - connectedAt >= 45000) streamFailures = 0;
      if (event.eventType === "ping") return;
      if (event.eventType === "resync") {
        if (autoRefreshing) resyncPending = true; else refreshVisibleRecords();
        return;
      }
      const row = event.parsed;
      if (!row || typeof row.message_id !== "string" || !Number.isSafeInteger(row.id)) return;
      const key = recordKey(row);
      const index = loadedRecords.findIndex((item) => recordKey(item) === key);
      if (index >= 0 && (loadedRecords[index].id > row.id || (loadedRecords[index].id === row.id &&
          JSON.stringify(loadedRecords[index].execution ?? null) === JSON.stringify(row.execution ?? null) &&
          JSON.stringify(loadedRecords[index].executions ?? null) === JSON.stringify(row.executions ?? null) &&
          loadedRecords[index].execution_summary === row.execution_summary))) return;
      if (index >= 0) loadedRecords.splice(index, 1);
      loadedRecords.push(row);
      loadedRecords.sort(compareRecords);
      if (autoRefreshing) streamChanges.set(key, row);
      renderRecords([row], false, true);
      el("records-status").textContent = `已加载 ${loadedRecords.length} 条消息.`;
      if (summaryTimer === null) summaryTimer = setTimeout(() => { summaryTimer = null; loadSummary(); }, 1000);
    },
  }, scope).then((id) => {
    if (generation !== streamGeneration) { bridge.unsubscribeSSE(id).catch(() => {}); return; }
    streamId = id;
  }).catch(() => failLive(generation));
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { stopLive(); recordsRevision++; summaryRevision++; }
  else { streamFailures = 0; startLive(); refreshVisibleRecords(); }
});
window.addEventListener("pagehide", stopLive);
el("overflow").addEventListener("click", () => showQuery("audit", {platform_id: "__rule_sentinel_system__", group_id: "__ingress_overflow__", limit: 50}));
el("notifications").addEventListener("click", () => showQuery("notifications", {limit: 50}));
let actionRows = [];
let actionSequence = 0;
let actionDrag = null;
const actionAnimations = new WeakMap();
function prefersReducedMotion() {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}
function actionPositions() {
  return new Map(actionRows.filter((item) => typeof item.row.getBoundingClientRect === "function")
    .map((item) => [item.row, item.row.getBoundingClientRect().top + (window.scrollY || 0)]));
}
function animateActionOrder(before, synchronized = false) {
  if (prefersReducedMotion()) return [];
  const animations = [];
  for (const item of actionRows) {
    const top = before.get(item.row);
    if (top === undefined || typeof item.row.animate !== "function") continue;
    actionAnimations.get(item.row)?.cancel();
    const distance = top - item.row.getBoundingClientRect().top - (window.scrollY || 0);
    if (Math.abs(distance) < 1) continue;
    const animation = item.row.animate(
      [{transform: `translateY(${distance}px)`}, {transform: "translateY(0)"}],
      synchronized ? {duration: 320, easing: "linear", fill: "both"} : {duration: 220, easing: "cubic-bezier(.2, .8, .2, 1)"});
    if (synchronized) { animation.pause(); animation.currentTime = 0; }
    actionAnimations.set(item.row, animation);
    animations.push(animation);
  }
  return animations;
}
function stopActionFollow(finish = false) {
  const follow = actionFollow;
  if (!follow) return;
  actionFollow = null;
  window.cancelAnimationFrame?.(follow.frame);
  if (finish) window.scrollTo?.({top: follow.destination, behavior: "instant"});
  for (const animation of follow.animations) animation.cancel();
}
function updateActionColor(row, type, mode, template, duration, mappingRows) {
  const levels = type.value === "mute" && duration.value === "strict"
    ? mappingRows.map(({levelInput}) => levelInput.value.normalize("NFC").trim()).filter(Boolean).sort() : [];
  const text = type.value === "reply" ? template.value.normalize("NFC") : "";
  const key = JSON.stringify([type.value, mode.value, levels, text]);
  let hash = 2166136261;
  for (let index = 0; index < key.length; index++) hash = Math.imul(hash ^ key.charCodeAt(index), 16777619);
  const ranges = {reply: [205, 80], mute: [4, 74], recall: [108, 74]};
  const [base, span] = Object.hasOwn(ranges, type.value) ? ranges[type.value] : [0, 360];
  row.style.setProperty("--action-hue", String(base + (hash >>> 0) % span));
}
function updateActionOrder() {
  actionRows.forEach((item, index) => {
    const number = index + 1;
    item.number.textContent = String(number);
    item.row.setAttribute("aria-label", `第 ${number} 个处理动作`);
    item.handle.setAttribute("aria-label", `动作 ${number} 排序, 可拖动或按方向键`);
    item.handle.disabled = actionRows.length < 2;
    item.up.setAttribute("aria-label", `上移动作 ${number}`);
    item.up.setAttribute("aria-disabled", String(index === 0));
    item.down.setAttribute("aria-label", `下移动作 ${number}`);
    item.down.setAttribute("aria-disabled", String(index === actionRows.length - 1));
  });
}
function actionScrollPadding() {
  const style = window.getComputedStyle?.(document.documentElement);
  return {top: parseFloat(style?.scrollPaddingTop) || 0, bottom: parseFloat(style?.scrollPaddingBottom) || 0};
}
function reserveActionFollowSpace(target, padding) {
  const scroll = document.scrollingElement;
  if (!scroll || !target?.getBoundingClientRect || !window.getComputedStyle) return;
  const panel = el("panel-rules");
  const bounds = target.getBoundingClientRect();
  const style = window.getComputedStyle(target);
  const marginTop = parseFloat(style.scrollMarginTop) || 0;
  const marginBottom = parseFloat(style.scrollMarginBottom) || 0;
  const center = (bounds.top + bounds.bottom + marginBottom - marginTop) / 2;
  const viewportCenter = (window.innerHeight + padding.top - padding.bottom) / 2;
  const desired = (window.scrollY || 0) + center - viewportCenter;
  const needed = Math.ceil(desired - (scroll.scrollHeight - window.innerHeight));
  if (needed > 0) panel.style.paddingBottom = `${(parseFloat(panel.style.paddingBottom) || 0) + needed}px`;
}
function actionFollowTarget(control, card, padding) {
  const available = window.innerHeight - padding.top - padding.bottom;
  return card && card.getBoundingClientRect?.().height <= available ? card : control;
}
function actionFollowDestination(target, padding) {
  const bounds = target.getBoundingClientRect();
  const style = window.getComputedStyle(target);
  const marginTop = parseFloat(style.scrollMarginTop) || 0;
  const marginBottom = parseFloat(style.scrollMarginBottom) || 0;
  const center = (bounds.top + bounds.bottom + marginBottom - marginTop) / 2;
  const viewportCenter = (window.innerHeight + padding.top - padding.bottom) / 2;
  return Math.max(0, Math.min((window.scrollY || 0) + center - viewportCenter,
    document.scrollingElement.scrollHeight - window.innerHeight));
}
function followActionControl(control, card = null) {
  control?.focus?.({preventScroll: true});
  const padding = actionScrollPadding();
  const target = actionFollowTarget(control, card, padding);
  if (card) reserveActionFollowSpace(target, padding);
  target?.scrollIntoView?.({behavior: prefersReducedMotion() ? "auto" : "smooth", block: card ? "center" : "nearest", inline: "nearest"});
}
function followActionSmoothly(item, control, before, startScroll) {
  if (prefersReducedMotion() || !document.scrollingElement || !window.getComputedStyle ||
      !window.scrollTo || !window.requestAnimationFrame) return false;
  control?.focus?.({preventScroll: true});
  window.scrollTo({top: startScroll, behavior: "instant"});
  const padding = actionScrollPadding();
  const target = actionFollowTarget(control, item.row, padding);
  reserveActionFollowSpace(target, padding);
  const destination = actionFollowDestination(target, padding);
  const follow = {frame: null, destination, animations: animateActionOrder(before, true)};
  actionFollow = follow;
  let started = null;
  function advance(time) {
    if (actionFollow !== follow) return;
    if (started === null) started = time;
    const progress = Math.min(1, (time - started) / 320);
    const eased = progress * progress * (3 - 2 * progress);
    for (const animation of follow.animations) animation.currentTime = eased * 320;
    window.scrollTo({top: startScroll + (destination - startScroll) * eased, behavior: "instant"});
    if (progress < 1) follow.frame = window.requestAnimationFrame(advance);
    else stopActionFollow();
  }
  follow.frame = window.requestAnimationFrame(advance);
  return true;
}
function moveAction(item, target, control, centerCard = false) {
  const from = actionRows.indexOf(item);
  if (from < 0 || target < 0 || target >= actionRows.length || from === target) return false;
  const before = actionPositions();
  const startScroll = window.scrollY || 0;
  stopActionFollow();
  for (const entry of actionRows) actionAnimations.get(entry.row)?.cancel();
  actionRows.splice(from, 1);
  actionRows.splice(target, 0, item);
  el("action-list").insertBefore(item.row, actionRows[target + 1]?.row || null);
  updateActionOrder();
  clearPreview();
  el("action-order-status").textContent = `动作 ${from + 1} 已移动到第 ${target + 1} 位.`;
  if (!centerCard || !followActionSmoothly(item, control, before, startScroll)) {
    followActionControl(control, centerCard ? item.row : null);
    animateActionOrder(before);
  }
  return true;
}
function cancelActionDrag() {
  const drag = actionDrag;
  if (!drag) return null;
  actionDrag = null;
  if (drag.frame !== null) window.cancelAnimationFrame?.(drag.frame);
  drag.marker?.remove();
  drag.ghost?.remove();
  drag.item.row.classList?.remove("is-dragging");
  drag.handle.classList?.remove("is-dragging");
  try {
    if (drag.handle.hasPointerCapture?.(drag.pointerId)) drag.handle.releasePointerCapture(drag.pointerId);
  } catch { /* The pointer may already have been released. */ }
  return drag;
}
function placeActionMarker(drag) {
  const others = actionRows.filter((item) => item !== drag.item);
  const index = others.findIndex((item) => {
    const bounds = item.row.getBoundingClientRect();
    return drag.y < bounds.top + bounds.height / 2;
  });
  drag.target = index < 0 ? others.length : index;
  if (drag.target === actionRows.indexOf(drag.item)) {
    if (drag.marker.parentNode) {
      const before = actionPositions();
      drag.marker.remove();
      animateActionOrder(before);
    }
    return;
  }
  const next = others[drag.target]?.row || null;
  if (drag.marker.parentNode === el("action-list") && drag.marker.nextElementSibling === next) return;
  const before = actionPositions();
  el("action-list").insertBefore(drag.marker, next);
  animateActionOrder(before);
}
function scrollActionDrag(drag) {
  if (actionDrag !== drag || !drag.started) return;
  const edge = Math.min(80, window.innerHeight / 4);
  const distance = drag.y < edge ? -Math.ceil(18 * (edge - drag.y) / edge)
    : drag.y > window.innerHeight - edge ? Math.ceil(18 * (drag.y - window.innerHeight + edge) / edge) : 0;
  if (distance) { window.scrollBy(0, Math.max(-18, Math.min(18, distance))); placeActionMarker(drag); }
  drag.frame = window.requestAnimationFrame(scrollActionDrag.bind(null, drag));
}
function startActionDrag(event, item) {
  if (actionDrag || actionRows.length < 2 || event.isPrimary === false || (event.pointerType !== "touch" && event.button !== 0)) return;
  stopActionFollow();
  actionDrag = {item, handle: item.handle, pointerId: event.pointerId, startX: event.clientX, startY: event.clientY,
    x: event.clientX, y: event.clientY, target: actionRows.indexOf(item), started: false, marker: null, ghost: null, frame: null};
  try { item.handle.setPointerCapture(event.pointerId); } catch { /* Window listeners handle the drag. */ }
  item.handle.focus?.({preventScroll: true});
  event.preventDefault();
}
function updateActionDrag(event) {
  const drag = actionDrag;
  if (!drag || drag.pointerId !== event.pointerId) return;
  drag.x = event.clientX;
  drag.y = event.clientY;
  if (!drag.started) {
    if (Math.hypot(drag.x - drag.startX, drag.y - drag.startY) < 6) return;
    drag.started = true;
    drag.item.row.classList.add("is-dragging");
    drag.handle.classList.add("is-dragging");
    drag.marker = document.createElement("div");
    drag.marker.className = "action-drop-placeholder";
    drag.marker.style.setProperty("--action-hue", drag.item.row.style.getPropertyValue("--action-hue"));
    drag.marker.textContent = "放在此处";
    drag.marker.setAttribute("aria-hidden", "true");
    drag.ghost = document.createElement("div");
    drag.ghost.className = "action-drag-preview";
    drag.ghost.textContent = `动作 ${actionRows.indexOf(drag.item) + 1}: ${Object.hasOwn(actionNames, drag.item.type.value) ? actionNames[drag.item.type.value] : "处理动作"}`;
    drag.ghost.setAttribute("aria-hidden", "true");
    document.body.appendChild(drag.ghost);
    if (typeof window.requestAnimationFrame === "function") drag.frame = window.requestAnimationFrame(scrollActionDrag.bind(null, drag));
  }
  drag.ghost.style.left = `${Math.max(8, Math.min(drag.x + 12, window.innerWidth - 248))}px`;
  drag.ghost.style.top = `${Math.max(8, Math.min(drag.y + 12, window.innerHeight - 56))}px`;
  placeActionMarker(drag);
  event.preventDefault();
}
function finishActionDrag(event) {
  if (!actionDrag || actionDrag.pointerId !== event.pointerId) return;
  const drag = cancelActionDrag();
  if (drag.started && !moveAction(drag.item, drag.target, drag.handle)) followActionControl(drag.handle);
}
window.addEventListener("pointermove", updateActionDrag, {passive: false});
window.addEventListener("pointerup", finishActionDrag);
window.addEventListener("pointercancel", (event) => { if (actionDrag?.pointerId === event.pointerId) cancelActionDrag(); });
window.addEventListener("wheel", () => stopActionFollow(), {passive: true});
window.addEventListener("touchstart", () => stopActionFollow(), {passive: true});
window.addEventListener("blur", () => cancelActionDrag());
window.addEventListener("pagehide", () => { stopActionFollow(true); cancelActionDrag(); });
document.addEventListener("visibilitychange", () => { if (document.hidden) { stopActionFollow(true); cancelActionDrag(); } });
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && actionDrag) {
    const drag = cancelActionDrag();
    el("action-order-status").textContent = "已取消排序.";
    drag.handle.focus?.({preventScroll: true});
    event.preventDefault();
  }
});
function addAction(initial = {}) {
  const row = document.createElement("div");
  row.className = "form-panel";
  row.setAttribute("role", "listitem");
  const type = document.createElement("select");
  const actionId = ++actionSequence;
  type.id = `action-type-${actionId}`;
  type.className = "action-type";
  for (const [value, label] of [["", "请选择动作"], ["reply", "发言"], ["mute", "禁言"], ["recall", "撤回"]]) addOption(type, value, label);
  type.value = initial.type || "";
  const mode = document.createElement("select");
  mode.id = `action-mode-${actionId}`;
  mode.className = "action-mode";
  const inheritOption = addOption(mode, "inherit", actionModeLabel("inherit"));
  for (const [value, label] of [["auto", "自动"], ["confirm", "人工确认"]]) addOption(mode, value, label);
  mode.value = initial.mode || "inherit";
  const templateLabel = document.createElement("label");
  templateLabel.textContent = "发言模板";
  const template = document.createElement("textarea");
  template.value = initial.template || "";
  templateLabel.appendChild(template);
  const durationLabel = document.createElement("label");
  durationLabel.textContent = "禁言时长模式";
  const duration = document.createElement("select");
  for (const [value, label] of [["strict", "严格映射"], ["autonomous", "模型提出时长"]]) addOption(duration, value, label);
  duration.value = initial.duration_mode || "strict";
  durationLabel.appendChild(duration);
  const mappingLabel = document.createElement("div");
  mappingLabel.className = "severity-editor";
  const mappingTitle = document.createElement("strong");
  mappingTitle.textContent = "严重级别和禁言时长";
  const mappingHelp = document.createElement("p");
  mappingHelp.className = "field-help";
  mappingHelp.textContent = "模型按严重级别选择对应的禁言时长. 例如 low 表示轻微, high 表示严重; 也可自定义级别名称.";
  const mapping = document.createElement("div");
  mapping.className = "severity-list";
  const mappingRows = [];
  const updateColor = () => updateActionColor(row, type, mode, template, duration, mappingRows);
  function addSeverity(level = "", seconds = "") {
    const entry = document.createElement("div");
    entry.className = "severity-row";
    const levelLabel = document.createElement("label");
    levelLabel.textContent = "级别名称";
    const levelInput = document.createElement("input");
    levelInput.value = level;
    levelInput.placeholder = "例如 low";
    levelLabel.appendChild(levelInput);
    const secondsLabel = document.createElement("label");
    secondsLabel.textContent = "禁言时长";
    const secondsInput = document.createElement("input");
    secondsInput.type = "number";
    secondsInput.min = "1";
    secondsInput.step = "1";
    const units = [[86400, "天"], [3600, "小时"], [60, "分钟"], [1, "秒"]];
    const initialUnit = Number.isSafeInteger(seconds) && seconds > 0 ? units.find(([size]) => seconds % size === 0)[0] : 1;
    secondsInput.value = String(seconds === "" ? "" : seconds / initialUnit);
    secondsLabel.appendChild(secondsInput);
    const unitLabel = document.createElement("label");
    unitLabel.textContent = "单位";
    const unitInput = document.createElement("select");
    for (const [size, label] of units) addOption(unitInput, String(size), label);
    unitInput.value = String(initialUnit);
    unitLabel.appendChild(unitInput);
    const removeSeverity = document.createElement("button");
    removeSeverity.type = "button";
    removeSeverity.textContent = "删除级别";
    const record = {entry, levelInput, secondsInput, unitInput};
    removeSeverity.addEventListener("click", () => { mappingRows.splice(mappingRows.indexOf(record), 1); entry.remove(); clearPreview(); updateColor(); });
    for (const input of [levelInput, secondsInput, unitInput]) {
      input.addEventListener("input", clearPreview);
      input.addEventListener("change", () => { clearPreview(); if (input === levelInput) updateColor(); });
    }
    for (const child of [levelLabel, secondsLabel, unitLabel, removeSeverity]) entry.appendChild(child);
    mappingRows.push(record);
    mapping.appendChild(entry);
  }
  for (const [level, seconds] of Object.entries(initial.severity_seconds ?? {})) addSeverity(level, seconds);
  const addSeverityButton = document.createElement("button");
  addSeverityButton.type = "button";
  addSeverityButton.textContent = "添加级别";
  addSeverityButton.addEventListener("click", () => { addSeverity(); clearPreview(); updateColor(); });
  for (const child of [mappingTitle, mapping, addSeverityButton, mappingHelp]) mappingLabel.appendChild(child);
  const remove = document.createElement("button");
  remove.type = "button"; remove.textContent = "删除动作";
  const updateVisibility = (animate = true) => {
    setPanelVisibility(templateLabel, type.value === "reply", {animate});
    setPanelVisibility(durationLabel, type.value === "mute", {animate});
    setPanelVisibility(mappingLabel, type.value === "mute" && duration.value === "strict", {animate});
  };
  for (const field of [type, mode, template, duration]) {
    field.addEventListener("input", clearPreview);
    field.addEventListener("change", () => { clearPreview(); updateVisibility(); updateColor(); });
  }
  const up = document.createElement("button");
  const down = document.createElement("button");
  up.type = down.type = "button";
  up.className = "action-move-up";
  down.className = "action-move-down";
  up.textContent = "上移"; down.textContent = "下移";
  const header = document.createElement("div");
  header.className = "action-row-header";
  const order = document.createElement("span");
  order.className = "action-order";
  const orderLabel = document.createElement("span");
  orderLabel.textContent = "动作";
  const number = document.createElement("strong");
  number.className = "action-order-number";
  const handle = document.createElement("button");
  handle.type = "button";
  handle.className = "action-drag-handle";
  handle.textContent = "拖动排序";
  handle.setAttribute("aria-describedby", "action-order-help");
  order.appendChild(orderLabel);
  order.appendChild(number);
  header.appendChild(order);
  header.appendChild(handle);
  const item = {row, type, mode, inheritOption, template, duration, mappingRows, number, handle, up, down};
  remove.addEventListener("click", () => { stopActionFollow(); cancelActionDrag(); actionRows = actionRows.filter((entry) => entry !== item); row.remove(); updateActionOrder(); clearPreview(); });
  up.addEventListener("click", () => { cancelActionDrag(); moveAction(item, actionRows.indexOf(item) - 1, up, true); });
  down.addEventListener("click", () => { cancelActionDrag(); moveAction(item, actionRows.indexOf(item) + 1, down, true); });
  handle.addEventListener("pointerdown", (event) => startActionDrag(event, item));
  handle.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    event.preventDefault();
    cancelActionDrag();
    moveAction(item, actionRows.indexOf(item) + (event.key === "ArrowUp" ? -1 : 1), handle, true);
  });
  row.appendChild(header);
  for (const child of [type, mode, templateLabel, durationLabel, mappingLabel, remove, up, down]) row.appendChild(child);
  const typeLabel = document.createElement("label");
  typeLabel.className = "action-type-label";
  typeLabel.setAttribute("for", type.id);
  typeLabel.textContent = "动作类型";
  const modeLabel = document.createElement("label");
  modeLabel.className = "action-mode-label";
  modeLabel.setAttribute("for", mode.id);
  modeLabel.textContent = "执行方式";
  row.appendChild(typeLabel);
  row.appendChild(modeLabel);
  el("action-list").appendChild(row);
  actionRows.push(item);
  updateActionOrder();
  updateVisibility(false);
  updateColor();
}
function setActions(actions) {
  stopActionFollow();
  cancelActionDrag();
  el("panel-rules").style.paddingBottom = "";
  actionRows = [];
  el("action-list").textContent = "";
  el("action-order-status").textContent = "";
  for (const action of actions) addAction(action);
}
function readActions() {
  return actionRows.map(({type, mode, template, duration, mappingRows}) => {
    if (!["reply", "mute", "recall"].includes(type.value)) { type.focus?.(); throw new Error("请选择动作类型"); }
    const result = {type: type.value, mode: mode.value};
    if (type.value === "reply") result.template = template.value;
    if (type.value === "mute") {
      result.duration_mode = duration.value;
      const mapping = {};
      if (duration.value === "strict") {
        if (!mappingRows.length) throw new Error("请至少添加一个严重级别");
        for (const {levelInput, secondsInput, unitInput} of mappingRows) {
          const level = levelInput.value;
          const amount = Number(secondsInput.value);
          const seconds = amount * Number(unitInput.value);
          if (!level.trim()) throw new Error("请填写级别名称");
          if (Object.hasOwn(mapping, level)) throw new Error("级别名称不能重复");
          if (!secondsInput.value.trim() || !Number.isSafeInteger(amount) || amount <= 0 || !Number.isSafeInteger(seconds) || seconds > 2592000) throw new Error("禁言时长须为正整数, 最长 30 天");
          Object.defineProperty(mapping, level, {value: seconds, enumerable: true});
        }
      }
      result.severity_seconds = mapping;
    }
    return result;
  });
}
el("add-action").addEventListener("click", () => { stopActionFollow(); cancelActionDrag(); clearPreview(); addAction(); });
el("preview").addEventListener("click", async () => {
  if (saving) return;
  stopActionFollow();
  el("panel-rules").style.paddingBottom = "";
  clearPreview();
  const saveRevision = revision;
  saving = true;
  updateRuleSaveControls();
  try {
    const scope = editingScope ? JSON.parse(editingScope) : group();
    if (!scope.platform_id || !scope.group_id) throw new Error("请选择所属群");
    if (!allGroups() && JSON.stringify(group()) !== JSON.stringify(scope)) throw new Error("请返回原群编辑, 或重新选择当前群的规则");
    const version = Number(el("version").value);
    if (!Number.isSafeInteger(version) || version < 0) throw new Error("版本必须是非负整数");
    const priority = Number(el("priority").value);
    if (!Number.isInteger(priority) || priority < -2147483648 || priority > 2147483647) throw new Error("优先级必须是 32 位整数");
    const fields = {...scope, rule_id: el("rule").value.trim(), expected_version: version,
      text: el("text").value, priority, enabled: el("enabled").checked, actions: readActions()};
    if (ruleTogglesInFlight.has(JSON.stringify([scope.platform_id, scope.group_id, fields.rule_id]))) throw new Error("规则状态正在保存, 请稍后重试");
    el("save-status").textContent = "正在保存规则...";
    const received = await bridge.apiPost("rules/preview", fields);
    if (revision !== saveRevision) return;
    pending = received;
    const saved = await bridge.apiPost("rules/confirm", received);
    const changedDuringConfirm = revision !== saveRevision;
    const sameDraft = () => editingScope === JSON.stringify(scope) && el("rule").value.trim() === fields.rule_id;
    if (!changedDuringConfirm || (sameDraft() && Number(el("version").value) === version)) {
      if (Number.isSafeInteger(saved.version)) {
        el("version").value = String(saved.version);
        el("rule").readOnly = true;
        el("rule-group-picker").hidden = true;
        el("new-rule-enabled").hidden = true;
        el("rule-form-title").textContent = "编辑规则";
      }
      clearPreview();
      const completedRevision = revision;
      const currentScope = allGroups() || JSON.stringify(group()) === JSON.stringify(scope);
      await Promise.all([currentScope ? loadCurrentRules() : Promise.resolve(), refreshGroupCounts()]);
      if ((changedDuringConfirm || revision !== completedRevision) && sameDraft()) {
        showFeedback("规则已保存, 新修改尚未保存.", "warning");
      } else showFeedback("规则已保存.");
    } else {
      await refreshGroupCounts();
      if (editingScope === JSON.stringify(scope) && el("rule").value.trim() === fields.rule_id) {
        showFeedback("规则已保存, 新修改尚未保存.", "warning");
      } else showFeedback("规则已保存.");
    }
  } catch (error) {
    if (revision === saveRevision) feedbackError(error, "保存失败");
  } finally {
    if (el("save-status").textContent === "正在保存规则...") el("save-status").textContent = "";
    pending = null;
    saving = false;
    updateRuleSaveControls();
  }
});

function addOption(select, value, label) {
  const option = document.createElement("option");
  option.value = value;
  option.title = label;
  option.textContent = label;
  select.appendChild(option);
  return option;
}
function groupKey(item) { return JSON.stringify([item.platform_id, item.group_id]); }
function baseGroupName(item) { return item.alias?.trim() || item.platform_name?.trim() || "未命名群"; }
let readableGroupNames = new Map();
function updateReadableGroupNames() {
  const groupsByName = new Map();
  for (const item of knownGroups) {
    const name = baseGroupName(item);
    if (!groupsByName.has(name)) groupsByName.set(name, []);
    groupsByName.get(name).push(groupKey(item));
  }
  readableGroupNames = new Map();
  for (const [name, keys] of groupsByName) {
    keys.sort().forEach((key, index) => readableGroupNames.set(key, keys.length > 1 ? `${name} (${index + 1})` : name));
  }
}
function groupName(item) { return readableGroupNames.get(groupKey(item)) || baseGroupName(item); }
function updateGroupOptions() {
  const select = el("group-select");
  const current = group();
  updateReadableGroupNames();
  select.textContent = "";
  addOption(select, "", "所有群");
  for (const item of knownGroups) addOption(select, groupKey(item), groupName(item));
  select.value = knownGroups.some((item) => groupKey(item) === groupKey(current)) ? groupKey(current) : "";
}
function currentGroupInfo() {
  const scope = group();
  return knownGroups.find((item) => item.platform_id === scope.platform_id && item.group_id === scope.group_id);
}
function updateGroupNameControls(resetAlias = true) {
  const scope = group();
  const item = currentGroupInfo();
  const selected = !!scope.platform_id && !!scope.group_id;
  if (resetAlias) el("group-alias").value = item?.alias || "";
  el("save-group-alias").disabled = !selected || nameLookups.has(JSON.stringify(["group-alias", scope.platform_id, scope.group_id]));
  el("get-group-name").disabled = !selected || nameLookups.has(JSON.stringify(["group-name", scope.platform_id, scope.group_id]));
  el("group-detail").hidden = !selected;
  el("group-detail-empty").hidden = selected;
  el("group-detail-name").textContent = selected ? groupName(item || scope) : "";
}
async function changeGroupName(endpoint, {automatic = false} = {}) {
  const scope = group();
  if (!scope.platform_id || !scope.group_id) return;
  const requestKey = JSON.stringify([endpoint, scope.platform_id, scope.group_id]);
  if (nameLookups.has(requestKey)) return;
  nameLookups.add(requestKey);
  const button = el(endpoint === "group-alias" ? "save-group-alias" : "get-group-name");
  const aliasDraft = el("group-alias").value;
  button.disabled = true;
  el("group-name-status").textContent = endpoint === "group-name" ? "正在获取群名..." : "正在保存备注...";
  try {
    const result = await bridge.apiPost(endpoint, endpoint === "group-alias" ? {...scope, name: aliasDraft} : scope);
    const index = knownGroups.findIndex((item) => item.platform_id === scope.platform_id && item.group_id === scope.group_id);
    const changedName = endpoint === "group-alias" ? {alias: result.alias} : {platform_name: result.platform_name};
    if (index < 0) knownGroups.push({...scope, rule_count: 0, ...changedName});
    else knownGroups[index] = {...knownGroups[index], ...changedName};
    if (JSON.stringify(group()) !== JSON.stringify(scope)) {
      updateReadableGroupNames();
      if (!automatic) showFeedback(endpoint === "group-alias" ? "备注已保存." : "群名已更新.");
      return;
    }
    const currentDraft = el("group-alias").value;
    updateGroupOptions();
    updateGroupNameControls();
    renderGroupList();
    renderAllGroupStatus();
    if (endpoint === "group-name" || currentDraft !== aliasDraft) el("group-alias").value = currentDraft;
    el("group-name-status").textContent = "";
    if (!automatic) showFeedback(endpoint === "group-alias" ? currentDraft !== aliasDraft
      ? "备注已保存, 新修改尚未保存." : "备注已保存." : "群名已更新.", currentDraft !== aliasDraft && endpoint === "group-alias" ? "warning" : "success");
  } catch {
    const message = endpoint === "group-alias" ? "备注保存失败." : "无法获取群名, 可填写群备注.";
    if (automatic && JSON.stringify(group()) === JSON.stringify(scope)) el("group-name-status").textContent = message;
    else if (!automatic) { el("group-name-status").textContent = ""; showFeedback(message, "error"); }
  } finally {
    nameLookups.delete(requestKey);
    updateGroupNameControls(false);
  }
}

function groupMonitoring(item) {
  if (!currentSettings || !Array.isArray(currentSettings.monitoring_groups)) return null;
  const listed = currentSettings.monitoring_groups.includes(`${item.platform_id}/${item.group_id}`);
  return currentSettings.monitoring_mode === "allowlist" ? listed : !listed;
}
function groupMonitoringState(item) {
  const allowed = groupMonitoring(item);
  if (allowed === null) return ["状态不可用", "muted"];
  if (!allowed) return ["已停用", "muted"];
  if (!runtimeStatus) return ["状态不可用", "muted"];
  return runtimeStatus.monitoring === "dry_run" ? ["允许监控", "success"] : ["监控未启动", "warning"];
}
function renderQuickStart() {
  if (quickStartCompleted === true) { el("quick-start").hidden = true; return; }
  const settings = currentSettings;
  const groups = knownGroups.filter((item) => !item.restored_only);
  const hasText = (value) => typeof value === "string" && value.trim().length > 0;
  const model = !settings ? null : settings.decision_backend === "llm" ? hasText(settings.llm_provider_id)
    : settings.decision_backend === "jev" ? hasText(settings.jev_model) && settings.jev_api_key_set === true : false;
  const group = !groupsLoaded || !settings || !Array.isArray(settings.monitoring_groups) ? null
    : groups.some((item) => groupMonitoring(item) === true);
  const validCount = (item) => Number.isSafeInteger(item.rule_count) && item.rule_count >= 0;
  const rules = !groupsLoaded ? null : groups.some((item) => validCount(item) && item.rule_count > 0)
    ? true : groups.every(validCount) ? false : null;
  const steps = [
    ["model", model, settingsLoadFailed],
    ["group", group, groupsLoadFailed || settingsLoadFailed || (!!settings && !Array.isArray(settings.monitoring_groups))],
    ["rule", rules, groupsLoadFailed || (groupsLoaded && rules === null)],
  ];
  for (const [name, done, unavailable] of steps) {
    const state = el(`quick-start-${name}-state`);
    const value = done === true ? "done" : done === false ? "pending" : "unknown";
    state.textContent = done === true ? "已完成" : done === false ? "未完成" : unavailable ? "状态不可用" : "读取中";
    state.setAttribute("data-state", value);
    state.closest?.("li")?.setAttribute("data-state", value);
  }
  const completed = steps.filter(([, done]) => done === true).length;
  el("quick-start").hidden = false;
  if (completed === steps.length && quickStartCompleted === false && !quickStartSaving && !quickStartSaveFailed) {
    quickStartSaving = true;
    bridge.apiPost("quick-start/complete", {}).then((result) => {
      if (result?.completed === true) quickStartCompleted = true;
      else quickStartSaveFailed = true;
    }).catch(() => { quickStartSaveFailed = true; }).finally(() => { quickStartSaving = false; renderQuickStart(); });
  }
  el("quick-start-progress").textContent = quickStartSaving ? "正在保存进度..." : quickStartSaveFailed
    ? "无法保存进度, 请刷新页面重试." : quickStartStateFailed ? "无法读取进度, 请刷新页面重试."
    : steps.every(([, done]) => done !== null) ? `已完成 ${completed} / ${steps.length}`
    : steps.some(([, done, unavailable]) => done === null && unavailable) ? "部分进度不可用" : "正在读取进度...";
}
function chooseGroup(item) {
  selectedScope = {platform_id: item.platform_id, group_id: item.group_id};
  updateGroupOptions();
  return selectCurrentGroup();
}
function renderGroupList(preserveClosing = false) {
  renderQuickStart();
  const container = el("groups-list");
  el("groups-count").textContent = groupsLoaded ? `${knownGroups.length} 个群` : "";
  const editor = el("group-detail-panel");
  const home = el("group-detail-home");
  if (!groupsLoaded || !knownGroups.length) {
    expandedGroupKey = null;
    cancelPanelTransition(editor);
    editor.hidden = false;
    if (editor.parentNode !== home) home.appendChild(editor);
    groupRows.clear();
    container.textContent = !groupsLoaded ? (groupsLoadFailed ? "群列表不可用." : "正在读取群列表...") : "还没有已知群, 可在下方添加.";
    groupListMessage = true;
    return;
  }
  if (groupListMessage) { container.textContent = ""; groupListMessage = false; }
  const selectedKey = groupKey(group());
  const keys = new Set(knownGroups.map(groupKey));
  if (expandedGroupKey !== selectedKey || !keys.has(expandedGroupKey)) expandedGroupKey = null;
  if (!expandedGroupKey && editor.parentNode !== home && !preserveClosing) {
    cancelPanelTransition(editor);
    editor.hidden = false;
    home.appendChild(editor);
  }
  for (const [key, entry] of groupRows) {
    if (keys.has(key)) continue;
    if (editor.parentNode === entry.row) home.appendChild(editor);
    entry.row.remove();
    groupRows.delete(key);
  }
  for (const [index, item] of knownGroups.entries()) {
    const key = groupKey(item);
    let entry = groupRows.get(key);
    if (!entry) {
      const row = document.createElement("div");
      const identity = document.createElement("div");
      const name = document.createElement("strong");
      const meta = document.createElement("p");
      identity.appendChild(name); identity.appendChild(meta);
      const state = document.createElement("span");
      state.className = "badge";
      const viewRules = document.createElement("button");
      viewRules.type = "button";
      viewRules.className = "secondary group-rules";
      viewRules.textContent = "查看群规则";
      viewRules.addEventListener("click", async () => {
        const chosen = knownGroups.find((group) => groupKey(group) === key);
        if (!chosen) return;
        await chooseGroup(chosen);
        if (groupKey(group()) === key) selectTab("rules");
      });
      const button = document.createElement("button");
      button.type = "button"; button.textContent = "配置";
      button.setAttribute("aria-controls", "group-detail-panel");
      button.addEventListener("click", () => {
        const chosen = knownGroups.find((group) => groupKey(group) === key);
        if (!chosen) return;
        if (expandedGroupKey === key) {
          expandedGroupKey = null;
          renderGroupList(true);
          setPanelVisibility(editor, false, {onHidden: () => {
            if (!expandedGroupKey && editor.parentNode === row) home.appendChild(editor);
          }});
          return;
        }
        expandedGroupKey = key;
        let result;
        if (key === groupKey(group())) renderGroupList();
        else result = chooseGroup(chosen);
        setPanelVisibility(editor, true, {force: true});
        row.scrollIntoView?.({behavior: prefersReducedMotion() ? "auto" : "smooth", block: "start"});
        return result;
      });
      row.appendChild(identity); row.appendChild(state); row.appendChild(viewRules); row.appendChild(button);
      entry = {row, name, meta, state, button};
      groupRows.set(key, entry);
    }
    const expanded = key === expandedGroupKey;
    entry.row.className = expanded ? "group-row is-editing" : "group-row";
    entry.name.textContent = groupName(item);
    entry.meta.textContent = `${item.rule_count ?? 0} 条规则`;
    const allowed = groupMonitoring(item);
    entry.state.setAttribute("data-tone", allowed === true ? "success" : allowed === false ? "error" : "muted");
    entry.state.textContent = allowed === true ? "启用监控" : allowed === false ? "关闭监控" : "状态不可用";
    entry.state.title = groupMonitoringState(item)[0];
    entry.button.textContent = expanded ? "收起" : "配置";
    entry.button.setAttribute("aria-expanded", String(expanded));
    if (container.children[index] !== entry.row) container.insertBefore(entry.row, container.children[index] || null);
    if (expanded && editor.parentNode !== entry.row) {
      cancelPanelTransition(editor);
      entry.row.appendChild(editor);
    }
  }
}
async function refreshGroupCounts() {
  try {
    const groups = await bridge.apiGet("groups");
    if (!Array.isArray(groups)) return;
    const counts = new Map(groups.map((item) => [groupKey(item), item.rule_count]));
    for (const item of knownGroups) {
      if (counts.has(groupKey(item))) { item.rule_count = counts.get(groupKey(item)); delete item.restored_only; }
    }
    renderGroupList();
  } catch { /* Counts stay as last loaded. */ }
}
function renderAllGroupStatus() {
  const body = el("groups-status-body");
  body.textContent = "";
  const displayedGroups = allGroups() ? knownGroups : knownGroups.filter((item) => groupKey(item) === groupKey(group()));
  el("status-groups-total").textContent = groupsLoaded ? String(displayedGroups.length) : "-";
  el("status-groups-enabled").textContent = groupsLoaded && currentSettings
    ? String(displayedGroups.filter((item) => groupMonitoring(item) === true).length) : "-";
  if (!groupsLoaded) { el("all-groups-status-message").textContent = groupsLoadFailed ? "群列表不可用." : "正在读取群列表..."; return; }
  if (!knownGroups.length) { el("all-groups-status-message").textContent = "还没有已知群."; return; }
  const truncated = displayedGroups.some((item) => groupSummaries.get(groupKey(item))?.truncated);
  el("all-groups-status-message").textContent = allGroupsLoading ? "正在读取统计..." : truncated ? "带 + 的统计仅覆盖该群最近的部分消息." : "";
  if (!allGroupsLoading && displayedGroups.some((item) => groupSummaryFailures.has(groupKey(item)))) el("all-groups-status-message").textContent += " 部分群统计不可用.";
  for (const item of displayedGroups) {
    const summary = groupSummaries.get(groupKey(item));
    const recent = summary?.recent_24h;
    const number = (value) => value == null ? "-" : `${value}${summary.truncated ? "+" : ""}`;
    const [monitorLabel, monitorTone] = groupMonitoringState(item);
    const values = [groupName(item), monitorLabel, number(recent?.messages), number(recent?.proposed),
      number(recent?.no_action), summary?.latency_p95_ms == null ? "-" : `${summary.latency_p95_ms} ms`,
      item.last_activity ? localTimeFor(item.last_activity) : "-"];
    const row = document.createElement("tr");
    for (const [index, value] of values.entries()) {
      const cell = document.createElement("td");
      cell.setAttribute("data-label", ["群", "监控状态", "消息数", "拟执行", "未违反规则", "P95 延迟", "最近活动"][index]);
      if (index === 1) {
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.setAttribute("data-tone", monitorTone);
        badge.textContent = value;
        cell.appendChild(badge);
      } else cell.textContent = value;
      if (index >= 2 && index <= 5) cell.className = "number";
      row.appendChild(cell);
    }
    body.appendChild(row);
  }
}
async function loadAllGroupSummaries() {
  const requestRevision = ++allGroupsRevision;
  if (!groupsLoaded || !knownGroups.length) { allGroupsLoading = false; renderAllGroupStatus(); return; }
  allGroupsLoading = true;
  renderAllGroupStatus();
  const items = [...knownGroups];
  let next = 0;
  const failed = new Set();
  const results = new Map();
  async function worker() {
    while (next < items.length) {
      const item = items[next++];
      try { results.set(groupKey(item), await bridge.apiGet("summary", {platform_id: item.platform_id, group_id: item.group_id})); }
      catch { failed.add(groupKey(item)); }
    }
  }
  await Promise.all(Array.from({length: Math.min(items.length, 6)}, worker));
  if (requestRevision !== allGroupsRevision) return;
  allGroupsLoading = false;
  groupSummaries = results;
  groupSummaryFailures = failed;
  renderAllGroupStatus();
}
el("refresh-all-groups").addEventListener("click", loadAllGroupSummaries);
el("save-group-alias").addEventListener("click", () => changeGroupName("group-alias"));
el("get-group-name").addEventListener("click", () => changeGroupName("group-name"));
const attemptedAutomaticNames = new Set();
function lookupMissingGroupName() {
  const scope = group();
  const item = currentGroupInfo();
  const key = JSON.stringify(scope);
  if (!scope.platform_id || !scope.group_id || item?.alias || item?.platform_name || attemptedAutomaticNames.has(key)) return;
  if (!knownPlatforms.some((platform) => platform.id === scope.platform_id)) return;
  attemptedAutomaticNames.add(key);
  changeGroupName("group-name", {automatic: true});
}
function rememberGroup() {
  const selection = group();
  const currentRevision = ++stateRevision;
  stateSave = stateSave.catch(() => {}).then(async () => {
    if (currentRevision !== stateRevision) return;
    try {
      await bridge.apiPost("ui-state", selection);
      if (currentRevision === stateRevision) el("selection-status").textContent = "";
    } catch {
      if (currentRevision === stateRevision) showFeedback("无法保存上次选择.", "error");
    }
  });
}
function describeRuleAction(action) {
  const name = Object.hasOwn(actionNames, action.type) ? actionNames[action.type] : String(action.type ?? "未知动作");
  const mode = actionModeLabel(action.mode);
  let extra = "";
  if (action.type === "mute") {
    if (action.duration_mode === "strict") {
      const levels = Object.entries(action.severity_seconds || {}).map(([level, seconds]) => `${level}: ${formatDuration(seconds)}`);
      extra = ` (${levels.join(", ") || "未配置级别"})`;
    } else extra = action.duration_mode === "autonomous" ? " (模型提出时长)" : " (时长模式未知)";
  } else if (action.type === "reply") extra = ` (${action.template ?? ""})`;
  return `${name} / ${mode}${extra}`;
}
function detachedRuleDraft() {
  return editingScope !== null && !expandedRuleCard && !el("rule-form").hidden;
}
function updateNewRuleButton() {
  const button = el("new-rule");
  const canCancel = detachedRuleDraft();
  const scope = group();
  button.textContent = canCancel ? "取消" : "新建规则";
  button.setAttribute("aria-expanded", String(canCancel));
  button.disabled = saving || (!canCancel && (allGroups() ? !groupsLoaded || !knownGroups.length : !scope.platform_id || !scope.group_id));
  button.title = button.disabled ? (saving ? "正在保存规则" : "请先添加群") : "";
}
function updateRuleSaveControls() {
  el("preview").disabled = saving;
  const buttons = document.querySelectorAll?.(".rule-edit") || (expandedRuleCard ? [expandedRuleCard.querySelector?.(".rule-edit")].filter(Boolean) : []);
  for (const button of buttons) button.disabled = saving;
  for (const toggle of document.querySelectorAll?.(".rule-state-toggle input") || []) {
    toggle.disabled = saving || ruleTogglesInFlight.has(toggle.ruleKey);
  }
  updateNewRuleButton();
}
function parkRuleEditor() {
  const form = el("rule-form");
  const transition = cancelPanelTransition(form);
  if (transition && !transition.visible) form.hidden = true;
  const parent = form.parentNode;
  const panel = el("panel-rules");
  if (expandedRuleCard === parent) expandedRuleCard = null;
  if (parent && parent !== panel) {
    parent.classList?.remove("is-editing");
    const button = parent.querySelector?.(".rule-edit");
    if (button) { button.setAttribute("aria-expanded", "false"); button.textContent = "编辑"; button.title = ""; }
    panel.insertBefore(form, el("current-rules"));
  }
  updateNewRuleButton();
}
function attachRuleEditor(card, button) {
  const form = el("rule-form");
  if (form.parentNode !== card) parkRuleEditor();
  expandedRuleCard = card;
  card.classList?.add("is-editing");
  button.setAttribute("aria-expanded", "true");
  button.textContent = "收起";
  button.title = "收起并放弃未保存的修改";
  if (form.parentNode !== card) card.appendChild(form);
  updateNewRuleButton();
}
function closeRuleEditor() {
  stopActionFollow();
  cancelActionDrag();
  el("panel-rules").style.paddingBottom = "";
  clearPreview();
  const form = el("rule-form");
  editingScope = null;
  const parent = form.parentNode;
  if (expandedRuleCard === parent) expandedRuleCard = null;
  parent?.classList?.remove("is-editing");
  const button = parent?.querySelector?.(".rule-edit");
  if (button) { button.setAttribute("aria-expanded", "false"); button.textContent = "编辑"; button.title = ""; }
  updateNewRuleButton();
  setPanelVisibility(form, false, {onHidden: parkRuleEditor});
  el("save-status").textContent = "";
}
async function loadCurrentRules() {
  const grouped = allGroups();
  const scopes = grouped ? knownGroups.map((item) => ({platform_id: item.platform_id, group_id: item.group_id})) : [group()];
  const requestRevision = ++rulesRevision;
  const container = el("current-rules");
  if (!container.children.length) container.textContent = "正在读取规则...";
  try {
    const batches = scopes.map((scope) => ({scope, rules: null, error: null}));
    let next = 0;
    async function worker() {
      while (next < batches.length) {
        const batch = batches[next++];
        try { batch.rules = await bridge.apiGet("rules", batch.scope); }
        catch (error) { batch.error = error; }
      }
    }
    await Promise.all(Array.from({length: Math.min(batches.length, 6)}, worker));
    if (requestRevision !== rulesRevision) return;
    const form = el("rule-form");
    const anchorBefore = !form.hidden && form.getClientRects?.().length ? form.getBoundingClientRect().top : null;
    const focused = form.contains?.(document.activeElement) ? document.activeElement : null;
    let selection = null;
    try {
      if (Number.isInteger(focused?.selectionStart) && Number.isInteger(focused?.selectionEnd)) {
        selection = [focused.selectionStart, focused.selectionEnd, focused.selectionDirection];
      }
    } catch { /* Some input types do not expose a text selection. */ }
    if (expandedRuleCard && form.parentNode === expandedRuleCard) {
      for (const {row} of actionRows) row.style.animation = "none";
    }
    parkRuleEditor();
    ruleDescriptions.length = 0;
    const nextContainer = document.createElement("div");
    let editorDestination = null;
    if (!batches.length) nextContainer.textContent = groupsLoadFailed ? "群列表不可用." : "还没有已知群.";
    for (const batch of batches) {
      const current = batch.scope;
      let destination = nextContainer;
      if (grouped) {
        const section = document.createElement("section");
        section.className = "rule-group";
        const heading = document.createElement("h2");
        const info = knownGroups.find((item) => groupKey(item) === groupKey(current));
        heading.textContent = groupName(info || current);
        section.appendChild(heading);
        destination = document.createElement("div");
        destination.className = "rule-group-items";
        section.appendChild(destination);
        nextContainer.appendChild(section);
      }
      if (batch.error) { showError(destination, batch.error); continue; }
      const rules = batch.rules;
      if (!Array.isArray(rules)) { destination.textContent = "无法读取规则."; continue; }
      if (!rules.length) destination.textContent = "该群还没有规则.";
      for (const rule of rules) {
      const item = document.createElement("div");
      item.className = "rule-card";
      const content = document.createElement("div");
      const meta = document.createElement("div");
      meta.className = "rule-meta";
      const title = document.createElement("strong");
      title.textContent = rule.rule_id;
      const ruleKey = JSON.stringify([current.platform_id, current.group_id, rule.rule_id]);
      const toggleLabel = document.createElement("label");
      toggleLabel.className = "rule-state-toggle";
      const state = document.createElement("span");
      state.textContent = rule.enabled ? "已启用" : "已关闭";
      const toggle = document.createElement("input");
      toggle.type = "checkbox";
      toggle.checked = rule.enabled;
      toggle.ruleKey = ruleKey;
      toggle.disabled = saving || ruleTogglesInFlight.has(ruleKey);
      toggle.setAttribute("role", "switch");
      toggle.setAttribute("aria-label", `启用规则 ${rule.rule_id}`);
      toggle.addEventListener("change", async () => {
        if (saving || ruleTogglesInFlight.has(ruleKey)) { toggle.checked = rule.enabled; return; }
        const enabled = toggle.checked;
        const originalVersion = rule.version;
        ruleTogglesInFlight.add(ruleKey);
        toggle.disabled = true;
        el("rule-list-status").textContent = "正在更新规则状态...";
        try {
          const fields = {...current, rule_id: rule.rule_id, expected_version: originalVersion,
            text: rule.text, priority: rule.priority ?? 0, enabled, actions: rule.actions || []};
          const preview = await bridge.apiPost("rules/preview", fields);
          const saved = await bridge.apiPost("rules/confirm", preview);
          if (editingScope === JSON.stringify(current) && el("rule").value === rule.rule_id &&
              el("rule").readOnly && el("version").value === String(originalVersion) && Number.isSafeInteger(saved.version)) {
            el("version").value = String(saved.version);
            el("enabled").checked = enabled;
            clearPreview();
          }
          ruleTogglesInFlight.delete(ruleKey);
          if (allGroups() || JSON.stringify(group()) === JSON.stringify(current)) await loadCurrentRules();
          el("rule-list-status").textContent = "";
          showFeedback(enabled ? "规则已启用." : "规则已关闭.");
        } catch (error) {
          toggle.checked = rule.enabled;
          ruleTogglesInFlight.delete(ruleKey);
          if (allGroups() || JSON.stringify(group()) === JSON.stringify(current)) await loadCurrentRules();
          el("rule-list-status").textContent = "";
          feedbackError(error);
        } finally {
          ruleTogglesInFlight.delete(ruleKey);
          toggle.disabled = false;
        }
      });
      toggleLabel.appendChild(state);
      toggleLabel.appendChild(toggle);
      meta.appendChild(title);
      meta.appendChild(toggleLabel);
      const version = document.createElement("span");
      version.className = "rule-version";
      version.textContent = `当前版本: 第 ${rule.version} 版`;
      version.title = "每次保存都会生成新版本";
      meta.appendChild(version);
      const text = document.createElement("p");
      text.textContent = rule.text;
      const currentConfig = document.createElement("section");
      currentConfig.className = "rule-current-config";
      const configHeading = document.createElement("div");
      configHeading.className = "rule-current-heading";
      const configTitle = document.createElement("strong");
      configTitle.textContent = "当前配置";
      const priority = document.createElement("span");
      priority.textContent = `优先级: ${rule.priority ?? 0}`;
      configHeading.appendChild(configTitle);
      configHeading.appendChild(priority);
      currentConfig.appendChild(configHeading);
      const actions = Array.isArray(rule.actions) ? rule.actions : [];
      if (actions.length) {
        const list = document.createElement("ol");
        list.className = "rule-current-actions";
        for (const [index, action] of actions.entries()) {
          const line = document.createElement("li");
          const number = document.createElement("span");
          number.className = "rule-action-number";
          number.textContent = String(index + 1);
          const description = document.createElement("span");
          line.appendChild(number);
          line.appendChild(description);
          const update = () => { description.textContent = describeRuleAction(action); };
          update(); ruleDescriptions.push(update);
          list.appendChild(line);
        }
        currentConfig.appendChild(list);
      } else {
        const empty = document.createElement("p");
        empty.textContent = "无处理动作";
        currentConfig.appendChild(empty);
      }
      const edit = document.createElement("button");
      edit.type = "button";
      edit.className = "rule-edit";
      edit.textContent = "编辑";
      edit.disabled = saving;
      edit.setAttribute("aria-controls", "rule-form");
      edit.setAttribute("aria-expanded", "false");
      edit.addEventListener("click", () => {
        if (expandedRuleCard === item) { closeRuleEditor(); return; }
        clearPreview();
        el("rule-form-title").textContent = "编辑规则";
        el("save-status").textContent = "";
        el("rule").value = rule.rule_id;
        el("rule").readOnly = true;
        editingScope = JSON.stringify(current);
        el("rule-group-picker").hidden = true;
        el("new-rule-enabled").hidden = true;
        el("version").value = String(rule.version);
        el("priority").value = String(rule.priority ?? 0);
        el("text").value = rule.text;
        setActions(rule.actions || []);
        el("enabled").checked = rule.enabled;
        attachRuleEditor(item, edit);
        setPanelVisibility(el("rule-form"), true, {force: true});
        item.scrollIntoView({behavior: prefersReducedMotion() ? "auto" : "smooth", block: "start"});
      });
      content.appendChild(meta);
      content.appendChild(text);
      content.appendChild(currentConfig);
      item.appendChild(content);
      const historyButton = document.createElement("button");
      historyButton.type = "button";
      historyButton.textContent = "历史版本";
      historyButton.setAttribute("aria-expanded", "false");
      const historyPanel = document.createElement("div");
      historyPanel.className = "rule-history";
      historyPanel.hidden = true;
      let panelRevision = 0;
      let historyExpanded = false;
      historyButton.addEventListener("click", async () => {
        const requestRevision = ++panelRevision;
        historyExpanded = !historyExpanded;
        historyButton.setAttribute("aria-expanded", String(historyExpanded));
        if (!historyExpanded) { setPanelVisibility(historyPanel, false); return; }
        const scopeRevision = historyRevision;
        historyPanel.textContent = "正在读取历史版本...";
        setPanelVisibility(historyPanel, true);
        try {
          const history = await bridge.apiGet("rules/history", {...current, rule_id: rule.rule_id});
          if (requestRevision !== panelRevision || scopeRevision !== historyRevision) return;
          if (history.some((entry) => entry.version > rule.version)) {
            updateVisiblePanel(historyPanel, () => { historyPanel.textContent = "规则已更新, 请刷新页面后查看历史版本."; });
            return;
          }
          const older = history.filter((entry) => entry.version < rule.version).reverse();
          updateVisiblePanel(historyPanel, () => {
            historyPanel.textContent = "";
            if (!older.length) { historyPanel.textContent = "暂无历史版本."; return; }
            const hint = document.createElement("p");
            hint.className = "history-hint";
            hint.textContent = "历史版本仅供查看, 不参与判定.";
            historyPanel.appendChild(hint);
            for (const entry of older) {
              const section = document.createElement("section");
              section.className = "history-entry";
              const heading = document.createElement("strong");
              heading.textContent = `第 ${entry.version} 版`;
              const info = document.createElement("p");
              info.textContent = `${localTimeFor(entry.created_at)} / ${entry.modified_by || "-"} / 保存时: ${entry.enabled ? "启用" : "停用"} / 优先级 ${entry.priority ?? 0}`;
              const text = document.createElement("p");
              text.textContent = entry.text;
              const actions = document.createElement("p");
              const update = () => { actions.textContent = (entry.actions || []).map((action, index) => `${index + 1}. ${describeRuleAction(action)}`).join("\n") || "未配置动作"; };
              update(); ruleDescriptions.push(update);
              for (const child of [heading, info, text, actions]) section.appendChild(child);
              historyPanel.appendChild(section);
            }
          });
        } catch (error) {
          if (requestRevision === panelRevision && scopeRevision === historyRevision) updateVisiblePanel(historyPanel, () => showError(historyPanel, error));
        }
      });
      item.appendChild(historyButton);
      item.appendChild(edit);
      item.appendChild(historyPanel);
      destination.appendChild(item);
      if (!form.hidden && editingScope === JSON.stringify(current) && el("rule").readOnly && el("rule").value === rule.rule_id) {
        editorDestination = {item, edit};
      }
      }
    }
    container.textContent = "";
    if (nextContainer.children.length) {
      while (nextContainer.children.length) container.appendChild(nextContainer.children[0]);
    } else container.textContent = nextContainer.textContent;
    if (editorDestination) attachRuleEditor(editorDestination.item, editorDestination.edit);
    if (anchorBefore !== null && !form.hidden && form.getClientRects?.().length) {
      const delta = form.getBoundingClientRect().top - anchorBefore;
      if (Math.abs(delta) > 1) window.scrollBy?.({top: delta, behavior: "instant"});
    }
    if (focused?.isConnected && !focused.disabled && document.activeElement !== focused) {
      focused.focus?.({preventScroll: true});
      if (selection) focused.setSelectionRange?.(...selection);
    }
  } catch (error) {
    if (requestRevision === rulesRevision) { parkRuleEditor(); showError(container, error); }
  }
}
function selectCurrentGroup(remember = true) {
  stopActionFollow();
  cancelActionDrag();
  parkRuleEditor();
  stopLive();
  streamFailures = 0;
  el("group-name-status").textContent = "";
  updateGroupNameControls();
  showGroupOverrides();
  renderGroupList();
  historyRevision += 1;
  clearPreview();
  if (remember && !el("rule-form").hidden) {
    el("save-status").textContent = allGroups() || editingScope === JSON.stringify(group())
      ? "草稿仍未保存." : "当前群已更改, 请返回原群后保存.";
  }
  outputRevision += 1;
  recordsRevision += 1;
  rulesRevision += 1;
  summaryRevision += 1;
  loadedRecords = [];
  streamChanges.clear();
  recordNodes.clear();
  if (remember) expandedMessages.clear();
  recordsCursor = null;
  auditCursor = 0;
  auditScope = "";
  el("audit-next").disabled = true;
  el("records-next").disabled = true;
  el("output").textContent = "";
  el("records-body").textContent = "";
  el("records-match-count").textContent = "匹配 0 条";
  el("current-rules").textContent = "";
  el("records-status").textContent = "";
  el("records-empty").hidden = true;
  el("summary-status").textContent = "";
  if (remember) el("rule-list-status").textContent = "";
  for (const id of ["stat-messages", "stat-proposed", "stat-no-action", "stat-latency"]) el(id).textContent = "—";
  updateFilterCounts(null);
  if (remember) rememberGroup();
  updateGroupMonitoring();
  if (!allGroups()) lookupMissingGroupName();
  renderAllGroupStatus();
  startLive();
  return Promise.all([loadCurrentRules(), loadRecords(true), loadSummary()]);
}
el("group-select").addEventListener("change", () => {
  const item = knownGroups.find((entry) => groupKey(entry) === el("group-select").value);
  return item ? chooseGroup(item) : chooseGroup({platform_id: "", group_id: ""});
});
function renderAvailableGroups() {
  const select = el("available-group-select");
  const selected = select.value;
  const existing = new Set(knownGroups.map(groupKey));
  const items = availableGroups.filter((item) => !existing.has(groupKey(item)));
  const pendingName = "正在获取群名...";
  const displayName = (item) => item.name?.trim() || (availableGroupNamePending ? pendingName : "未命名群");
  const names = new Map();
  for (const item of items) {
    const name = displayName(item);
    if (!names.has(name)) names.set(name, []);
    names.get(name).push(groupKey(item));
  }
  select.textContent = "";
  addOption(select, "", availableGroupsLoading ? "正在读取群列表..." : items.length ? "请选择群" : "暂无可选群");
  for (const item of items) {
    const name = displayName(item);
    const duplicates = name === pendingName ? [groupKey(item)] : names.get(name).sort();
    addOption(select, groupKey(item), duplicates.length > 1 ? `${name} (${duplicates.indexOf(groupKey(item)) + 1})` : name);
  }
  select.value = items.some((item) => groupKey(item) === selected) ? selected : "";
  select.disabled = availableGroupsLoading || availableGroupSaving || !items.length;
  el("refresh-available-groups").disabled = availableGroupsLoading || availableGroupSaving;
  el("add-group").disabled = availableGroupsLoading || availableGroupSaving || !select.value;
}
async function loadAvailableGroups() {
  if (availableGroupSaving) return;
  const current = ++availableGroupsRevision;
  availableGroupsLoading = true;
  availableGroupNamePending = false;
  el("add-group-status").textContent = "正在读取群列表...";
  renderAvailableGroups();
  let result;
  try {
    result = await bridge.apiGet("available-groups");
    if (!Array.isArray(result?.groups)) throw new Error("invalid group list");
    if (current !== availableGroupsRevision) return;
    availableGroups = result.groups.filter((item) => typeof item?.platform_id === "string" &&
      typeof item?.group_id === "string" && typeof item?.name === "string");
    availableGroupsLoading = false;
    renderAvailableGroups();
  } catch {
    if (current !== availableGroupsRevision) return;
    availableGroupsLoading = false;
    availableGroups = [];
    renderAvailableGroups();
    el("add-group-status").textContent = "群列表读取失败, 请刷新重试.";
    return;
  }
  const remaining = () => availableGroups.filter((item) => !knownGroups.some((known) => groupKey(known) === groupKey(item)));
  const initial = remaining();
  const partial = result.history_available === false || result.truncated === true;
  const baseStatus = result.platforms_available === false
    ? "没有可用的 QQ 官方机器人, 请先连接机器人."
    : !initial.length
    ? partial ? "群列表不完整, 请刷新重试. 也可在目标群向机器人发送消息后刷新."
      : "暂无可选群. 请在目标群向机器人发送消息, 然后刷新."
    : partial ? "部分群列表暂不可用, 可刷新重试." : "请选择机器人收到过消息的群.";
  el("add-group-status").textContent = baseStatus;
  const unnamed = initial.filter((entry) => !entry.name);
  let failedNames = false;
  if (unnamed.length) {
    availableGroupNamePending = true;
    renderAvailableGroups();
    el("add-group-status").textContent = "正在获取群名...";
  }
  for (const item of unnamed) {
    if (current !== availableGroupsRevision) return;
    try {
      const response = await bridge.apiPost("available-group-name", {platform_id: item.platform_id, group_id: item.group_id});
      if (current !== availableGroupsRevision) return;
      if (typeof response?.name !== "string" || !response.name.trim()) throw new Error("name unavailable");
      item.name = response.name.trim();
      renderAvailableGroups();
    } catch {
      if (current !== availableGroupsRevision) return;
      failedNames = true;
    }
  }
  if (current !== availableGroupsRevision) return;
  availableGroupNamePending = false;
  renderAvailableGroups();
  el("add-group-status").textContent = failedNames && remaining().length
    ? partial ? "部分群列表或群名暂不可用, 可刷新重试." : "部分群名不可用, 添加后可设置群备注."
    : baseStatus;
}
el("add-group-panel").addEventListener("toggle", () => {
  if (el("add-group-panel").open) return loadAvailableGroups();
});
el("refresh-available-groups").addEventListener("click", loadAvailableGroups);
el("available-group-select").addEventListener("change", renderAvailableGroups);
el("add-group").addEventListener("click", async () => {
  const item = availableGroups.find((entry) => groupKey(entry) === el("available-group-select").value);
  if (!item || knownGroups.some((entry) => groupKey(entry) === groupKey(item))) return;
  const scope = {platform_id: item.platform_id, group_id: item.group_id};
  availableGroupSaving = true;
  renderAvailableGroups();
  try {
    const saved = await bridge.apiPost("group-register", scope);
    const existing = knownGroups.find((item) => groupKey(item) === groupKey(scope));
    if (existing) { Object.assign(existing, saved); delete existing.restored_only; }
    else knownGroups.push({...saved, rule_count: 0, last_activity: null});
    if (!groupsLoaded) {
      try {
        const all = await bridge.apiGet("groups");
        if (Array.isArray(all)) { knownGroups = all; groupsLoaded = true; groupsLoadFailed = false; }
        else groupsLoadFailed = true;
      } catch { groupsLoadFailed = true; }
    }
    updateGroupOptions();
    renderAvailableGroups();
    await chooseGroup(scope);
    if (openGuideGroupAfterAdd && activeTab === "groups") {
      openGuideGroupAfterAdd = false;
      groupRows.get(groupKey(scope))?.button.click();
      focusQuickStartTarget(el("monitor-group"));
    }
    if (!availableGroups.some((entry) => !knownGroups.some((known) => groupKey(known) === groupKey(entry)))) {
      el("add-group-status").textContent = "暂无可选群. 请在目标群向机器人发送消息, 然后刷新.";
    }
    showFeedback(groupsLoaded ? "群已添加." : "群已添加, 群列表不可用.", groupsLoaded ? "success" : "warning");
  } catch { showFeedback("无法添加群, 请重试.", "error"); }
  finally { availableGroupSaving = false; renderAvailableGroups(); }
});
el("refresh-group").addEventListener("click", () => { scheduleRefresh(); return selectCurrentGroup(false); });
el("new-rule").addEventListener("click", () => {
  if (detachedRuleDraft()) { closeRuleEditor(); return; }
  parkRuleEditor();
  clearPreview();
  el("rule-form-title").textContent = "新建规则";
  el("save-status").textContent = "";
  el("rule").value = "";
  el("rule").readOnly = false;
  editingScope = JSON.stringify(group());
  el("rule-group-picker").hidden = !allGroups();
  el("rule-group").textContent = "";
  addOption(el("rule-group"), "", "请选择群");
  for (const item of knownGroups) addOption(el("rule-group"), groupKey(item), groupName(item));
  el("rule-group").value = "";
  el("new-rule-enabled").hidden = false;
  el("version").value = "0";
  el("priority").value = "0";
  el("text").value = "";
  setActions([]);
  el("enabled").checked = false;
  setPanelVisibility(el("rule-form"), true, {force: true});
  updateNewRuleButton();
  el("rule-form").scrollIntoView({behavior: prefersReducedMotion() ? "auto" : "smooth", block: "start"});
});
el("rule-group").addEventListener("change", () => {
  const chosen = knownGroups.find((item) => groupKey(item) === el("rule-group").value);
  editingScope = JSON.stringify(chosen ? {platform_id: chosen.platform_id, group_id: chosen.group_id} : {platform_id: "", group_id: ""});
  clearPreview();
});
const settingKeys = ["action_execution_enabled", "monitoring_mode", "decision_backend", "rule_selection_strategy", "default_execution_mode", "default_action_mode", "message_retention_days", "llm_reason_language", "llm_no_action_reason_enabled",
  "protected_target_policy", "protected_target_template", "protected_target_quote",
  "llm_provider_id", "max_duration_seconds", "model_concurrency", "model_timeout_seconds",
  "global_rate_per_minute", "group_rate_per_minute", "model_failure_threshold", "model_cooldown_seconds",
  "queue_capacity_per_group", "ingress_capacity", "notification_target", "notification_interval_seconds", "jev_provider", "jev_model"];
const numericSettings = new Set(["message_retention_days", "max_duration_seconds", "model_concurrency", "model_timeout_seconds",
  "global_rate_per_minute", "group_rate_per_minute", "model_failure_threshold", "model_cooldown_seconds",
  "queue_capacity_per_group", "ingress_capacity", "notification_interval_seconds"]);
const booleanSettings = new Set(["action_execution_enabled", "protected_target_quote", "llm_no_action_reason_enabled"]);
let backendChoiceChanged = false;
const groupDraftFields = ["group-model-backend", "group-model", "group-strategy", "group-execution", "group-reason-language-mode", "group-reason-language",
  "group-protected-policy", "group-protected-quote", "group-protected-template"];
function rememberGroupDraft() {
  const scope = group();
  if (!currentSettings || !scope.platform_id || !scope.group_id) return;
  const values = Object.fromEntries(groupDraftFields.map((id) => [id, el(id).value]));
  groupDrafts.set(groupKey(scope), {...values, customTemplate: el("group-template-override").checked,
    templateInitialized: groupTemplateDraftInitialized, reasonInitialized: groupReasonDraftInitialized});
  el("group-settings-status").textContent = "有尚未保存的修改.";
}
function postSettings(endpoint, data) {
  const request = settingsSaveQueue.then(() => bridge.apiPost(endpoint, data));
  settingsSaveQueue = request.then(() => {}, () => {});
  return request;
}
function updateGroupMonitoring() {
  const scope = group();
  const toggle = el("monitor-group");
  updateNewRuleButton();
  toggle.disabled = !currentSettings || !scope.platform_id || !scope.group_id;
  if (toggle.disabled) { toggle.checked = false; el("group-monitor-help").textContent = ""; return; }
  toggle.checked = groupMonitoring(scope) === true;
  el("group-monitor-help").textContent = groupMonitoringState(scope)[0];
}
function updateGroupModelFields(animate = false) {
  const backend = el("group-model-backend").value;
  setPanelVisibility(el("group-model-label"), !!backend, {animate});
  el("group-model-name-help").textContent = backend === "jev"
    ? "填写 Jev 模型名. 提供方和密钥使用监控设置中的全局配置."
    : backend === "llm" ? "填写 AstrBot 中已配置的模型提供商标识." : "";
  el("group-model-options").textContent = "";
  if (backend === "llm") for (const provider of availableProviders) addOption(el("group-model-options"), provider, provider);
  if (backend === "jev") {
    const model = currentSettings?.jev_provider === "openrouter" ? "typesafe/jev-latest" : "jev-latest";
    addOption(el("group-model-options"), model, model);
  }
}
function formatNoticeExample(template) {
  if (typeof template !== "string") return null;
  const values = {rule_id: "rule-1", sender_id: "member-1", action: "mute"};
  let result = "";
  for (let i = 0; i < template.length; i++) {
    const char = template[i];
    if (char === "{") {
      if (template[i + 1] === "{") { result += "{"; i++; continue; }
      const end = template.indexOf("}", i + 1);
      if (end < 0) return null;
      const rawField = template.slice(i + 1, end);
      const field = rawField.endsWith(":") ? rawField.slice(0, -1) : rawField;
      if (!Object.hasOwn(values, field)) return null;
      result += values[field];
      i = end;
    } else if (char === "}") {
      if (template[i + 1] !== "}") return null;
      result += "}";
      i++;
    } else result += char;
  }
  return result;
}
function renderNoticeExample(target, template) {
  const example = formatNoticeExample(template);
  el(target).textContent = !template?.trim() ? "尚未填写提示内容." : example === null
    ? "无法生成示例, 请检查花括号和变量名称." : example;
}
function updateGroupSettingsHints(animate = false) {
  const settings = currentSettings;
  if (!settings) return;
  const inheritedPolicy = settings.protected_target_policy === "group_message" ? "发送群提示" : "只记录";
  const inheritedQuote = settings.protected_target_quote === true ? "引用" : "不引用";
  const model = settings.decision_backend === "llm" ? `普通 LLM / ${settings.llm_provider_id || "未选择模型"}`
    : settings.decision_backend === "jev" ? `Jev / ${settings.jev_model || "未选择模型"}` : "未选择模型";
  const strategy = settings.rule_selection_strategy === "priority" ? "按规则优先级" : settings.rule_selection_strategy === "model" ? "模型选择" : "不可用";
  const groupExecution = settings.default_execution_mode === "record" ? "只记录" : settings.default_execution_mode === "execute" ? "允许执行" : "不可用";
  el("group-model-inherit").textContent = `全局(${model})`;
  el("group-strategy-inherit").textContent = `全局(${strategy})`;
  el("group-reason-language-inherit").textContent = `全局(${settings.llm_reason_language?.trim() || "自动选择"})`;
  setPanelVisibility(el("group-reason-language-label"), el("group-reason-language-mode").value === "custom", {animate});
  el("group-execution-inherit").textContent = `全局(${groupExecution})`;
  el("group-policy-inherit").textContent = `全局(${inheritedPolicy})`;
  el("group-quote-inherit").textContent = `全局(${inheritedQuote})`;
  const execution = el("group-execution").value || settings.default_execution_mode;
  let reason = "";
  if (execution === "record") reason = "按当前选择保存后, 本群只记录, 不执行规则动作.";
  else if (settings.action_execution_enabled !== true) reason = "全局真实执行未开启, 本群选择允许执行后仍只记录.";
  else if (groupMonitoring(group()) === false) reason = "本群监控未开启, 当前不会判定消息.";
  else if (!runtimeStatus) reason = "无法读取运行状态, 请查看运行状态.";
  else if (runtimeStatus.monitoring !== "dry_run") reason = "监控未启动, 请查看运行状态.";
  else if (runtimeStatus?.action_execution_available === false) reason = "真实执行当前不可用, 请查看运行状态.";
  el("group-execution-state").textContent = reason || "允许执行时, 每项动作按规则中的自动或人工确认设置处理.";
  const policy = el("group-protected-policy").value || settings.protected_target_policy || "exempt";
  el("group-protected-state").textContent = policy === "group_message"
    ? `满足上方条件时, 使用下方的提示内容和引用设置.${reason ? ` ${reason}` : ""}`
    : "选择只记录时不会发送群提示. 下方内容和引用设置只在选择发送群提示时使用.";
  const template = el("group-template-override").checked ? el("group-protected-template").value : settings.protected_target_template;
  setPanelVisibility(el("group-template-label"), el("group-template-override").checked, {animate});
  el("group-template-source").textContent = el("group-template-override").checked
    ? "群提示使用下方本群内容, 不改变全局默认."
    : "群提示使用监控设置中的全局默认内容.";
  renderNoticeExample("group-template-preview", template || "");
}
function showGroupOverrides() {
  const scope = group();
  const settings = currentSettings;
  const overrides = currentSettings?.group_overrides?.[`${scope.platform_id}/${scope.group_id}`] || {};
  const draft = groupDrafts.get(groupKey(scope));
  const model = settings?.decision_backend === "llm" ? settings.llm_provider_id : settings?.decision_backend === "jev" ? settings.jev_model : "";
  const strategy = settings?.rule_selection_strategy === "priority" ? "按规则优先级" : "模型选择";
  const execution = settings?.default_execution_mode === "record" ? "只记录" : "允许执行";
  el("group-defaults").textContent = settings ? `全局默认: ${model || "未选择模型"} / ${strategy} / ${execution}` : "全局设置不可用.";
  el("group-settings-save").disabled = groupSettingsSaving || !settings || !scope.platform_id || !scope.group_id;
  el("group-model-backend").value = overrides.model?.backend || "";
  el("group-model").value = overrides.model?.model || "";
  el("group-strategy").value = overrides.strategy || "";
  groupReasonDraftInitialized = Object.hasOwn(overrides, "llm_reason_language");
  el("group-reason-language-mode").value = groupReasonDraftInitialized ? "custom" : "";
  el("group-reason-language").value = groupReasonDraftInitialized ? overrides.llm_reason_language : "";
  el("group-execution").value = overrides.execution_mode || "";
  el("group-protected-policy").value = overrides.protected_policy || "";
  el("group-protected-quote").value = Object.hasOwn(overrides, "protected_quote") ? String(overrides.protected_quote) : "";
  groupTemplateDraftInitialized = Object.hasOwn(overrides, "protected_template");
  el("group-template-override").checked = groupTemplateDraftInitialized;
  el("group-protected-template").value = overrides.protected_template || "";
  if (draft) {
    for (const id of groupDraftFields) el(id).value = draft[id];
    el("group-template-override").checked = draft.customTemplate;
    groupTemplateDraftInitialized = draft.templateInitialized;
    groupReasonDraftInitialized = draft.reasonInitialized;
  }
  el("group-settings-status").textContent = draft ? "有尚未保存的修改." : "";
  updateGroupModelFields();
  updateGroupSettingsHints();
}
function readGroupOverrides() {
  const overrides = {};
  const backend = el("group-model-backend").value;
  if (backend) {
    const model = el("group-model").value.trim();
    if (!model) throw new Error("请填写模型名称");
    overrides.model = {backend, model};
  }
  for (const [id, key] of [["group-strategy", "strategy"], ["group-execution", "execution_mode"],
    ["group-protected-policy", "protected_policy"]]) if (el(id).value) overrides[key] = el(id).value;
  if (el("group-reason-language-mode").value === "custom") {
    overrides.llm_reason_language = el("group-reason-language").value.trim();
  } else if (el("group-reason-language-mode").value !== "") throw new Error("请选择有效的理由语言来源");
  if (el("group-protected-quote").value) overrides.protected_quote = el("group-protected-quote").value === "true";
  const customTemplate = el("group-template-override").checked;
  const template = customTemplate ? el("group-protected-template").value : currentSettings.protected_target_template || "";
  if (customTemplate && formatNoticeExample(template) === null) {
    el("group-protected-template").focus?.();
    throw new Error("提示内容的花括号或变量名称无效, 请使用列出的变量");
  }
  const policy = el("group-protected-policy").value || currentSettings.protected_target_policy || "exempt";
  if (policy === "group_message" && !template.trim()) {
    if (customTemplate) el("group-protected-template").focus?.();
    throw new Error(customTemplate ? "发送群提示时请填写本群提示内容" : "全局提示内容为空, 请为本群设置提示内容或修改全局默认");
  }
  if (customTemplate) overrides.protected_template = template;
  return overrides;
}
el("group-model-backend").addEventListener("change", () => updateGroupModelFields(true));
el("group-reason-language-mode").addEventListener("change", () => {
  if (el("group-reason-language-mode").value === "custom" && !groupReasonDraftInitialized) {
    el("group-reason-language").value = currentSettings?.llm_reason_language || "";
    groupReasonDraftInitialized = true;
  }
  updateGroupSettingsHints(true);
});
el("group-reason-language").addEventListener("input", () => { groupReasonDraftInitialized = true; });
for (const id of ["group-execution", "group-protected-policy", "group-protected-quote"]) {
  el(id).addEventListener("change", () => updateGroupSettingsHints(true));
}
el("group-template-override").addEventListener("change", () => {
  if (el("group-template-override").checked && !groupTemplateDraftInitialized) {
    const inherited = currentSettings?.protected_target_template;
    el("group-protected-template").value = typeof inherited === "string" && inherited.trim() ? inherited : DEFAULT_NOTICE_TEMPLATE;
    groupTemplateDraftInitialized = true;
  }
  updateGroupSettingsHints(true);
});
el("group-protected-template").addEventListener("input", () => { groupTemplateDraftInitialized = true; updateGroupSettingsHints(); });
for (const id of [...groupDraftFields, "group-template-override"]) {
  el(id).addEventListener("input", rememberGroupDraft);
  el(id).addEventListener("change", rememberGroupDraft);
}
el("group-settings-save").addEventListener("click", async () => {
  const scope = group();
  const button = el("group-settings-save");
  if (!currentSettings || groupSettingsSaving || !scope.platform_id || !scope.group_id) return;
  groupSettingsSaving = true;
  button.disabled = true;
  try {
    const overrides = readGroupOverrides();
    const key = groupKey(scope);
    const submittedDraft = groupDrafts.get(key);
    const result = await postSettings("group-settings", {...scope, overrides});
    currentSettings = result.settings;
    const changedSinceSubmit = groupDrafts.get(key) !== submittedDraft;
    if (!changedSinceSubmit) groupDrafts.delete(key);
    renderGroupList(); renderAllGroupStatus();
    if (key === groupKey(group()) && !changedSinceSubmit) showGroupOverrides();
    const message = result.start_error === "model_not_configured"
      ? `${changedSinceSubmit ? "群配置已保存, 新修改尚未保存." : "群配置已保存."} 请选择默认判定模型.` : result.start_error
      ? "群配置已保存, 但监控启动失败." : changedSinceSubmit ? "群配置已保存, 新修改尚未保存." : "群配置已保存.";
    showFeedback(message, result.start_error || changedSinceSubmit ? "warning" : "success");
    await refreshStatus();
  } catch (error) { feedbackError(error, "群配置保存失败"); }
  finally { groupSettingsSaving = false; button.disabled = !currentSettings || !group().platform_id || !group().group_id; }
});
function showSettings(settings, submitted = null) {
  const previousActionMode = currentSettings?.default_action_mode;
  currentSettings = settings;
  settingsLoadFailed = false;
  if (settings.quick_start_completed === true) quickStartCompleted = true;
  else if (quickStartCompleted === null && settings.quick_start_completed === false) quickStartCompleted = false;
  if (!submitted) quickStartStateFailed = typeof settings.quick_start_completed !== "boolean";
  if (previousActionMode && previousActionMode !== settings.default_action_mode && pending?.fields?.actions?.some((action) => action.mode === "inherit")) {
    clearPreview();
    el("save-status").textContent = "全局动作执行方式已更改, 请重新确认.";
  }
  for (const item of actionRows) item.inheritOption.textContent = actionModeLabel("inherit");
  for (const update of ruleDescriptions) update();
  for (const key of settingKeys) {
    const field = el(`setting-${key}`);
    if (booleanSettings.has(key)) {
      if (!submitted || field.checked === submitted[key]) field.checked = settings[key] === true;
    } else if (!submitted || field.value === String(submitted[key] ?? "")) {
      field.value = key === "decision_backend" && settings[key] === "" ? "llm" : String(settings[key] ?? "");
    }
  }
  if (!submitted || el("setting-jev_api_key").value === submitted.jev_api_key) el("setting-jev_api_key").value = "";
  el("setting-jev_api_key").placeholder = settings.jev_api_key_set ? "留空保持现有密钥" : "填写 Jev API Key";
  el("jev-api-key-help").textContent = settings.jev_api_key_set
    ? "已配置密钥, 所有使用 Jev 的群共用. 留空可保持现有密钥."
    : "尚未配置密钥. 使用 Jev 的群共用此密钥.";
  renderNoticeExample("setting-template-preview", el("setting-protected_target_template").value);
  if (settings.decision_backend === "jev") setDetailsOpen(el("jev-settings"), true);
  updateGroupMonitoring();
  showGroupOverrides();
  renderGroupList();
  renderAllGroupStatus();
}
el("setting-protected_target_template").addEventListener("input", () => {
  renderNoticeExample("setting-template-preview", el("setting-protected_target_template").value);
});
el("setting-decision_backend").addEventListener("change", () => {
  backendChoiceChanged = true;
  if (el("setting-decision_backend").value === "jev") setDetailsOpen(el("jev-settings"), true, true);
});
async function loadSettings() {
  try { showSettings(await bridge.apiGet("settings")); }
  catch (error) { settingsLoadFailed = true; renderQuickStart(); showError(el("settings-status"), error); }
  try {
    const providers = await bridge.apiGet("providers");
    availableProviders = providers.map((provider) => provider.id);
    el("provider-options").textContent = "";
    for (const provider of providers) addOption(el("provider-options"), provider.id, provider.id);
    updateGroupModelFields();
  } catch { /* The model input remains available without a provider list. */ }
}
el("setting-jev_provider").addEventListener("change", () => {
  el("jev-model-options").textContent = "";
  el("jev-models-status").textContent = "";
});
el("jev-models-fetch").addEventListener("click", async () => {
  const button = el("jev-models-fetch");
  const provider = el("setting-jev_provider").value;
  const key = el("setting-jev_api_key").value;
  const model = el("setting-jev_model").value;
  button.disabled = true;
  el("jev-models-status").textContent = "";
  try {
    const result = await bridge.apiPost("jev/models", {provider, api_key: key});
    const expected = provider === "openrouter" ? "typesafe/jev-latest" : "jev-latest";
    if (!Array.isArray(result.models) || result.models.length !== 1 || result.models[0] !== expected) throw new Error();
    if (provider !== el("setting-jev_provider").value || key !== el("setting-jev_api_key").value) return;
    el("jev-model-options").textContent = "";
    addOption(el("jev-model-options"), expected, expected);
    if (el("setting-jev_model").value === model) el("setting-jev_model").value = expected;
    showFeedback("模型列表已更新.");
  } catch { showFeedback("获取模型列表失败.", "error"); }
  finally { button.disabled = false; }
});
el("settings-save").addEventListener("click", async () => {
  el("settings-save").disabled = true;
  try {
    const data = {};
    for (const key of settingKeys) {
      const field = el(`setting-${key}`);
      if (booleanSettings.has(key)) data[key] = field.checked;
      else if (numericSettings.has(key)) {
        const invalid = (message) => {
          const details = field.closest?.("details");
          if (details) setDetailsOpen(details, true);
          field.focus?.();
          const label = field.closest?.("label")?.firstChild?.textContent?.trim();
          throw new Error(label ? `${label}: ${message}` : message);
        };
        if (!field.value.trim()) invalid("数值不能为空");
        data[key] = Number(field.value);
        if (!Number.isSafeInteger(data[key])) invalid("数值必须为整数");
      } else data[key] = field.value;
    }
    data.jev_api_key = el("setting-jev_api_key").value;
    const template = data.protected_target_template;
    if (formatNoticeExample(template) === null || (data.protected_target_policy === "group_message" && !template.trim())) {
      el("setting-protected_target_template").focus?.();
      throw new Error("请填写有效的默认群提示内容, 变量只支持 {rule_id}, {sender_id} 和 {action}");
    }
    for (const [key, overrides] of Object.entries(currentSettings?.group_overrides || {})) {
      const policy = overrides.protected_policy || data.protected_target_policy;
      const custom = Object.hasOwn(overrides, "protected_template");
      const groupTemplate = custom ? overrides.protected_template : template;
      if (policy !== "group_message" || groupTemplate.trim()) continue;
      if (!custom) {
        el("setting-protected_target_template").focus?.();
        throw new Error("有群选择发送提示并使用全局内容, 请填写默认群提示内容");
      }
      const item = knownGroups.find((entry) => `${entry.platform_id}/${entry.group_id}` === key);
      throw new Error(`${item ? groupName(item) : "某个群"} 的单独提示内容为空, 请在群管理中填写或调整处理方式`);
    }
    const requestData = {...data};
    if (currentSettings?.decision_backend === "" && data.decision_backend === "llm" && !backendChoiceChanged
        && data.llm_provider_id === currentSettings.llm_provider_id) requestData.decision_backend = "";
    const result = await postSettings("settings", requestData);
    const changedSinceSubmit = settingKeys.some((key) => booleanSettings.has(key)
      ? el(`setting-${key}`).checked !== data[key]
      : el(`setting-${key}`).value !== String(data[key] ?? "")) || el("setting-jev_api_key").value !== data.jev_api_key;
    showSettings(result.settings, data);
    if (el("setting-decision_backend").value === data.decision_backend) backendChoiceChanged = false;
    const message = result.start_error === "model_not_configured"
      ? `${changedSinceSubmit ? "设置已保存, 新修改尚未保存." : "设置已保存."} 请选择默认判定模型.`
      : result.start_error ? "设置已保存, 但监控启动失败; 请查看状态页."
      : changedSinceSubmit ? "设置已保存, 新修改尚未保存." : "设置已保存并应用.";
    showFeedback(message, result.start_error || changedSinceSubmit ? "warning" : "success");
    await refreshStatus();
  } catch (error) { feedbackError(error); }
  finally { el("settings-save").disabled = false; }
});
el("monitor-group").addEventListener("change", async () => {
  const toggle = el("monitor-group");
  const scope = group();
  const enabled = toggle.checked;
  toggle.disabled = true;
  try {
    const result = await postSettings("group-monitoring", {...scope, enabled});
    currentSettings = result.settings;
    const message = result.start_error === "model_not_configured" ? "群设置已保存. 请选择默认判定模型."
      : result.start_error ? "群设置已保存, 但监控启动失败." : enabled ? "群监控已开启." : "群监控已关闭.";
    showFeedback(message, result.start_error ? "warning" : "success");
    renderGroupList(); renderAllGroupStatus();
    await refreshStatus();
  } catch { showFeedback("无法保存群监控设置.", "error"); }
  finally { updateGroupMonitoring(); updateGroupSettingsHints(); }
});
let quickStartHighlight = null;
let quickStartHighlightTimer = null;
function focusQuickStartTarget(target, {highlight = false} = {}) {
  const visibleTarget = highlight ? target?.closest?.("label") || target : target;
  visibleTarget?.scrollIntoView?.(highlight
    ? {behavior: prefersReducedMotion() ? "auto" : "smooth", block: "center"} : {block: "center"});
  target?.focus?.({preventScroll: true});
  if (!highlight || !visibleTarget?.classList) return;
  if (quickStartHighlightTimer !== null) clearTimeout(quickStartHighlightTimer);
  quickStartHighlight?.classList.remove("quick-start-target");
  quickStartHighlight = visibleTarget;
  visibleTarget.classList.add("quick-start-target");
  quickStartHighlightTimer = setTimeout(() => {
    visibleTarget.classList.remove("quick-start-target");
    quickStartHighlight = null;
    quickStartHighlightTimer = null;
  }, 3500);
}
el("quick-start-model").addEventListener("click", () => {
  selectTab("settings");
  const showTarget = () => {
    if (activeTab === "settings") focusQuickStartTarget(el("setting-decision_backend"), {highlight: true});
  };
  if (typeof window.requestAnimationFrame === "function") window.requestAnimationFrame(showTarget);
  else setTimeout(showTarget, 0);
});
el("quick-start-group").addEventListener("click", () => {
  selectTab("groups");
  openGuideGroupAfterAdd = true;
  setDetailsOpen(el("add-group-panel"), true);
  focusQuickStartTarget(el("add-group-panel").querySelector?.("summary"));
});
el("quick-start-rule").addEventListener("click", () => {
  selectTab("rules");
  if (el("rule-form").hidden && !el("new-rule").disabled) el("new-rule").click();
  if (!el("rule-form").hidden) focusQuickStartTarget(el("rule"));
});
async function initializeSelection() {
  await uiStateRead;
  const [platforms, groups] = await Promise.allSettled([bridge.apiGet("platforms"), bridge.apiGet("groups")]);
  if (platforms.status === "fulfilled" && Array.isArray(platforms.value)) knownPlatforms = platforms.value;
  if (groups.status === "fulfilled" && Array.isArray(groups.value)) {
    knownGroups = groups.value;
    groupsLoaded = true;
    if (selectedScope.platform_id && selectedScope.group_id && !knownGroups.some((item) => groupKey(item) === groupKey(selectedScope))) {
      knownGroups.push({...selectedScope, rule_count: 0, last_activity: null, restored_only: true});
    }
  } else groupsLoadFailed = true;
  const platformCounts = new Map();
  for (const platform of knownPlatforms) {
    const name = ["qq_official", "qq_official_webhook"].includes(platform.type) ? "QQ 官方机器人" : "其他平台";
    platformCounts.set(name, (platformCounts.get(name) || 0) + 1);
  }
  el("platforms-list").textContent = platformCounts.size
    ? [...platformCounts].map(([name, count]) => `${name}: ${count} 个实例`).join("\n") : "没有可用平台实例.";
  updateGroupOptions();
  renderGroupList();
  renderAllGroupStatus();
  if (activeTab === "status") loadAllGroupSummaries();
  if (platforms.status === "rejected" || groups.status === "rejected") {
    el("selection-status").textContent = "平台或群列表读取失败, 请刷新页面重试.";
  }
  return selectCurrentGroup(false);
}
tabDataReady = true;
await Promise.all([initializeSelection(), loadSettings(), statusRead]);
