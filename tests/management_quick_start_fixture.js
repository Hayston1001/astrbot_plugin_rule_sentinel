const scenario = new URLSearchParams(location.search).get("scenario") || "empty";
const testGroup = {platform_id: "p", group_id: "g", alias: "测试群", rule_count: 0};
const settings = {
  action_execution_enabled: false, monitoring_mode: "allowlist", monitoring_groups: [],
  decision_backend: "llm", llm_provider_id: "", rule_selection_strategy: "model",
  default_execution_mode: "record", default_action_mode: "auto", message_retention_days: 30,
  llm_reason_language: "", llm_no_action_reason_enabled: true, group_overrides: {},
  protected_target_policy: "exempt", protected_target_template: "hello {rule_id}", protected_target_quote: false,
  jev_provider: "typesafe", jev_model: "", jev_api_key_set: false,
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
  : ["state-delay", "bridge-delay", "state-held", "status-held"].includes(scenario)
    ? {platform_id: "", group_id: "", active_tab: "records"} : null;
let uiState = JSON.parse(sessionStorage.getItem(uiStateKey) || "null") || initialUiState;
if (["complete", "blocklist", "jev", "jev-ready", "scope", "groups-error", "settings-error", "state-error", "save-error"].includes(scenario)) {
  groups.push({...testGroup, rule_count: 1});
  rules.push({rule_id: "first-rule", version: 1, enabled: true, text: "test", priority: 0, actions: []});
  settings.llm_provider_id = "model-a";
  settings.monitoring_groups = ["p/g"];
}
if (scenario === "blocklist") { settings.monitoring_mode = "blocklist"; settings.monitoring_groups = []; }
if (scenario === "model-ready") settings.llm_provider_id = "model-a";
if (scenario === "stale-group") { settings.monitoring_mode = "blocklist"; settings.llm_provider_id = "model-a"; }
if (scenario === "scope") groups.push({platform_id: "p", group_id: "other", alias: "另一个群", rule_count: 0});
if (scenario === "state-error") settings.quick_start_completed = null;
if (["jev", "jev-ready"].includes(scenario)) {
  settings.decision_backend = "jev"; settings.jev_model = "jev-latest"; settings.jev_api_key_set = scenario === "jev-ready";
}
window.quickStartMock = {settings, groups, rules, requests: [], uiStateReturned: false, uiStateWrites: []};
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
  async apiGet(endpoint) {
    window.quickStartMock.requests.push(endpoint);
    if (endpoint === "settings" && scenario === "settings-error") throw new Error("unavailable");
    if (endpoint === "groups" && scenario === "groups-error") throw new Error("unavailable");
    if (endpoint === "ui-state" && scenario === "ui-state-error") throw new Error("unavailable");
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
      status: {monitoring: settings.llm_provider_id || settings.jev_model ? "dry_run" : "disabled", backend: settings.decision_backend},
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
      if (body.jev_api_key) settings.jev_api_key_set = true;
      delete settings.jev_api_key;
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
    if (endpoint === "rules/preview") return {fields: body};
    if (endpoint === "rules/confirm") {
      rules.push({...body.fields, version: 1});
      groups[0].rule_count = rules.length;
      return {version: 1};
    }
    return body;
  },
  async subscribeSSE() { return "test"; },
  async unsubscribeSSE() {},
};
