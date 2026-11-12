const scenario = new URLSearchParams(location.search).get("scenario") || "empty";
const testGroup = {platform_id: "p", group_id: "g", alias: "测试群", rule_count: 0};
const settings = {
  action_execution_enabled: false, monitoring_mode: "allowlist", monitoring_groups: [],
  decision_backend: "llm", llm_provider_id: "", rule_selection_strategy: "model",
  default_execution_mode: "record", default_action_mode: "auto", message_retention_days: 30,
  llm_reason_language: "", llm_no_action_reason_enabled: true, group_overrides: {},
  protected_target_policy: "exempt", protected_target_template: "hello {rule_id}", protected_target_quote: false,
  quick_start_completed: sessionStorage.getItem(`fixture-${scenario}`) === "true", max_duration_seconds: 2592000,
  model_concurrency: 20, model_timeout_seconds: 35, global_rate_per_minute: 100,
  group_rate_per_minute: 60, model_failure_threshold: 5, model_cooldown_seconds: 120,
  queue_capacity_per_group: 100, ingress_capacity: 256, notification_target: "", notification_interval_seconds: 0,
};
const groups = [];
const rules = [];
const uiStateKey = `fixture-ui-${scenario}`;
const initialUiState = scenario === "scope" ? {platform_id: "p", group_id: "other"}
  : scenario === "stale-group" ? {platform_id: "p", group_id: "deleted"}
  : scenario === "save-stability" ? {platform_id: "p", group_id: "g", active_tab: "rules"}
  : ["state-delay", "bridge-delay", "state-held", "status-held"].includes(scenario)
    ? {platform_id: "", group_id: "", active_tab: "records"} : null;
let uiState = JSON.parse(sessionStorage.getItem(uiStateKey) || "null") || initialUiState;
if (["complete", "blocklist", "model-missing", "scope", "groups-error", "settings-error", "state-error", "save-error"].includes(scenario)) {
  groups.push({...testGroup, rule_count: 1});
  rules.push({rule_id: "first-rule", version: 1, enabled: true, text: "test", priority: 0, actions: []});
  settings.llm_provider_id = "model-a";
  settings.monitoring_groups = ["p/g"];
}
if (scenario === "blocklist") { settings.monitoring_mode = "blocklist"; settings.monitoring_groups = []; }
if (scenario === "model-ready") settings.llm_provider_id = "model-a";
if (scenario === "legacy-unselected") { settings.decision_backend = ""; settings.llm_provider_id = "model-a"; }
if (scenario === "save-stability") {
  groups.push({...testGroup, rule_count: 1});
  groups.push({platform_id: "p", group_id: "other", alias: "另一个群", rule_count: 1});
  rules.push({platform_id: "p", group_id: "g", rule_id: "rule-a", version: 1, enabled: true, text: "original rule", priority: 0,
    actions: [{type: "recall", mode: "auto"},
      {type: "mute", mode: "auto", duration_mode: "strict", severity_seconds: {low: 60}}]});
  rules.push({platform_id: "p", group_id: "other", rule_id: "rule-b", version: 1, enabled: true,
    text: "other group rule", priority: 0, actions: []});
  settings.llm_provider_id = "model-a";
  settings.monitoring_groups = ["p/g"];
  settings.quick_start_completed = true;
}
if (scenario === "stale-group") { settings.monitoring_mode = "blocklist"; settings.llm_provider_id = "model-a"; }
if (scenario === "scope") groups.push({platform_id: "p", group_id: "other", alias: "另一个群", rule_count: 0});
if (scenario === "state-error") settings.quick_start_completed = null;
if (scenario === "model-missing") settings.llm_provider_id = "";
window.quickStartMock = {settings, groups, rules, requests: [], uiStateReturned: false, uiStateWrites: []};
window.quickStartMock.holdNextRuleRead = () => {
  let release;
  const promise = new Promise((resolve) => { release = resolve; });
  window.quickStartMock.heldRuleRead = {promise, release, started: false};
  window.quickStartMock.releaseRuleRead = release;
  window.quickStartMock.ruleReadStarted = false;
};
window.quickStartMock.holdNextRuleConfirm = () => {
  let release;
  const promise = new Promise((resolve) => { release = resolve; });
  window.quickStartMock.heldRuleConfirm = {promise, release};
  window.quickStartMock.releaseRuleConfirm = release;
  window.quickStartMock.ruleConfirmStarted = false;
};
let releaseReady = null;
const ready = scenario === "bridge-delay" ? new Promise((resolve) => { releaseReady = resolve; }) : Promise.resolve();
window.quickStartMock.releaseReady = () => releaseReady?.();
let releaseState = null;
const stateReady = scenario === "state-held" ? new Promise((resolve) => { releaseState = resolve; }) : Promise.resolve();
window.quickStartMock.releaseState = () => releaseState?.();
let releaseStatus = null;
const statusReady = scenario === "status-held" ? new Promise((resolve) => { releaseStatus = resolve; }) : Promise.resolve();
window.quickStartMock.releaseStatus = () => releaseStatus?.();
window.AstrBotPluginView = {
  async ready() { await ready; },
  async apiGet(endpoint, params) {
    window.quickStartMock.requests.push(endpoint);
    if (endpoint === "settings" && scenario === "settings-error") throw new Error("unavailable");
    if (endpoint === "groups" && scenario === "groups-error") throw new Error("unavailable");
    if (endpoint === "ui-state" && scenario === "ui-state-error") throw new Error("unavailable");
    if (endpoint === "rules" && window.quickStartMock.heldRuleRead) {
      const held = window.quickStartMock.heldRuleRead;
      held.started = true;
      window.quickStartMock.ruleReadStarted = true;
      window.quickStartMock.heldRuleRead = null;
      await held.promise;
    }
    if (endpoint === "rules" && window.quickStartMock.failNextRuleRead) {
      window.quickStartMock.failNextRuleRead = false;
      throw new Error("rule read unavailable");
    }
    if (endpoint === "rules" && scenario === "save-stability") {
      const result = rules.filter((rule) => rule.platform_id === params.platform_id && rule.group_id === params.group_id);
      if (window.quickStartMock.malformedNextRuleRead) {
        window.quickStartMock.malformedNextRuleRead = false;
        result.push(null);
      }
      return structuredClone(result);
    }
    if (endpoint === "status") await statusReady;
    if (endpoint === "ui-state" && scenario === "state-held") {
      const saved = structuredClone(uiState);
      await stateReady;
      window.quickStartMock.uiStateReturned = true;
      return saved;
    }
    if (endpoint === "ui-state" && scenario === "state-delay") {
      const saved = structuredClone(uiState);
      await new Promise((resolve) => setTimeout(resolve, 300));
      window.quickStartMock.uiStateReturned = true;
      return saved;
    }
    const data = {
      status: {monitoring: settings.decision_backend === "llm" && settings.llm_provider_id ? "dry_run" : "disabled", backend: settings.decision_backend},
      settings, providers: [{id: "model-a"}], "ui-state": uiState,
      platforms: [{id: "p", type: "qq_official"}], groups, rules, messages: [],
      "available-groups": {groups: [{platform_id: "p", group_id: "g", name: "测试群"}], history_available: true},
      summary: {total_messages: 0, status_counts: {}, recent_24h: {messages: 0, proposed: 0, no_action: 0}},
    };
    return structuredClone(data[endpoint] ?? []);
  },
  async apiPost(endpoint, body) {
    window.quickStartMock.requests.push(endpoint);
    if (endpoint === "ui-state") {
      window.quickStartMock.uiStateWrites.push(structuredClone(body));
      if (scenario === "slow-tab-save" && body.active_tab === "rules") {
        await new Promise((resolve) => setTimeout(resolve, 200));
      }
      uiState = {platform_id: "", group_id: "", active_tab: "status", ...uiState, ...body};
      sessionStorage.setItem(uiStateKey, JSON.stringify(uiState));
      return structuredClone(body);
    }
    if (endpoint === "settings") {
      Object.assign(settings, body);
      return {settings: structuredClone(settings)};
    }
    if (endpoint === "quick-start/complete") {
      if (scenario === "save-error") throw new Error("unavailable");
      settings.quick_start_completed = true;
      sessionStorage.setItem(`fixture-${scenario}`, "true");
      return {completed: true};
    }
    if (endpoint === "group-register") { groups.push({...testGroup}); return structuredClone(testGroup); }
    if (endpoint === "group-monitoring") {
      settings.monitoring_groups = body.enabled ? ["p/g"] : [];
      return {settings: structuredClone(settings)};
    }
    if (endpoint === "group-settings") {
      const key = `${body.platform_id}/${body.group_id}`;
      if (Object.keys(body.overrides).length) settings.group_overrides[key] = body.overrides;
      else delete settings.group_overrides[key];
      return {settings: structuredClone(settings)};
    }
    if (endpoint === "group-alias") {
      const item = groups.find((entry) => entry.platform_id === body.platform_id && entry.group_id === body.group_id);
      if (item) item.alias = body.name;
      return {...body, alias: body.name, platform_name: ""};
    }
    if (endpoint === "rules/preview") return {fields: body};
    if (endpoint === "rules/confirm") {
      if (scenario === "save-stability") {
        if (window.quickStartMock.heldRuleConfirm) {
          const held = window.quickStartMock.heldRuleConfirm;
          window.quickStartMock.heldRuleConfirm = null;
          window.quickStartMock.ruleConfirmStarted = true;
          await held.promise;
        }
        if (window.quickStartMock.failNextRuleConfirm) {
          window.quickStartMock.failNextRuleConfirm = false;
          throw new Error("version conflict");
        }
        const rule = rules.find((entry) => entry.platform_id === body.fields.platform_id &&
          entry.group_id === body.fields.group_id && entry.rule_id === body.fields.rule_id);
        if ((rule?.version || 0) !== body.fields.expected_version) throw new Error("version conflict");
        if (rule) Object.assign(rule, body.fields, {version: body.fields.expected_version + 1});
        else rules.push({...body.fields, version: 1});
        return {version: body.fields.expected_version + 1};
      }
      rules.push({...body.fields, version: 1});
      groups[0].rule_count = rules.length;
      return {version: 1};
    }
    return body;
  },
  async subscribeSSE() { return "test"; },
  async unsubscribeSSE() {},
};
