const scenario = new URLSearchParams(location.search).get("scenario") || "empty";
const testGroup = {platform_id: "p", group_id: "g", alias: "测试群", rule_count: 0};
const settings = {
  action_execution_enabled: false, monitoring_mode: "allowlist", monitoring_groups: [],
  decision_backend: "llm", llm_provider_id: "", equal_priority_policy: "execute_all",
  default_execution_mode: "record", default_action_mode: "auto", message_retention_days: 30, audit_retention_days: 90, diagnostic_capture: false,
  reply_quote: true, reply_mention_sender: false,
  llm_reason_language: "", llm_no_action_reason_enabled: true, context_messages: 0, group_overrides: {},
  chat_query_items: ["status", "mute", "recall", "reply"],
  protected_target_policy: "exempt", protected_target_template: "hello {rule_id}", protected_target_quote: false,
  quick_start_completed: sessionStorage.getItem(`fixture-${scenario}`) === "true", max_duration_seconds: 2592000,
  model_concurrency: 20, model_timeout_seconds: 35, global_rate_per_minute: 100,
  group_rate_per_minute: 60, model_failure_threshold: 5, model_cooldown_seconds: 120,
  queue_capacity_per_group: 100, ingress_capacity: 256, notification_targets: [], notification_interval_seconds: 0,
  image_judging_enabled: false, image_understanding_mode: "caption", image_provider_id: "",
  image_caption_prompt: "describe the image", image_max_count: 4, image_max_bytes_per_image: 4194304,
  image_max_total_bytes: 8388608, image_fetch_timeout_seconds: 10,
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
    execution_mode: "auto", actions: [{type: "recall"},
      {type: "mute", duration_mode: "strict", severity_seconds: {low: 60}}]});
  rules.push({platform_id: "p", group_id: "other", rule_id: "rule-b", version: 1, enabled: true,
    text: "other group rule", priority: 0, execution_mode: "inherit", actions: []});
  settings.llm_provider_id = "model-a";
  settings.monitoring_groups = ["p/g"];
  settings.quick_start_completed = true;
}
if (scenario === "overview") {
  groups.push({...testGroup, rule_count: 2});
  groups.push({platform_id: "p2", group_id: "g", alias: "另一个群", rule_count: 1});
  settings.llm_provider_id = "model-a";
  settings.monitoring_groups = ["p/g", "p2/g"];
  settings.quick_start_completed = true;
}
if (scenario === "stale-group") { settings.monitoring_mode = "blocklist"; settings.llm_provider_id = "model-a"; }
if (scenario === "scope") groups.push({platform_id: "p", group_id: "other", alias: "另一个群", rule_count: 0});
if (scenario === "state-error") settings.quick_start_completed = null;
if (scenario === "model-missing") settings.llm_provider_id = "";
window.quickStartMock = {settings, groups, rules, requests: [], uiStateReturned: false, uiStateWrites: [],
  failLayoutRead: scenario === "layout-read-error", failLayoutSave: false, failPending: false, confirmationPosts: [], closePosts: [],
  ruleConfirmationPosts: [], ruleConfirmResult: null, failRuleConfirmation: false};
const layoutKey = `fixture-layout-${scenario}`;
const mockAction = (id, action = "mute", ruleId = "no-ads") => ({id, action, mode: "confirm", mode_reason: "rule", status: "pending_confirm",
  rule_id: ruleId, rule_version: 2, duration_seconds: action === "mute" ? 600 : null, expires_at: "2099-01-01T00:00:00Z",
  message_time: null, action_index: 0, reason_code: null, created_at: "2025-01-01T00:00:00Z",
  detail: JSON.stringify({proposal: {reason: "重复推广"}})});
const mockCheck = (id) => ({kind: "intent", intent_id: id, action: "mute", status: "unknown", rule_id: "no-ads",
  reason_code: null, updated_at: "2025-01-01T00:00:00Z", event_id: null});
window.quickStartMock.pending = scenario === "overview" ? Array.from({length: 23}, (_, index) => ({
  platform_id: "p", group_id: "g", message_id: index === 0 ? "same" : `m${index}`,
  cursor: 200 - index, content: index === 0 ? "<script>示例消息</script>" : null,
  content_truncated: false, execution_allowed: true,
  actions: index === 0 ? [mockAction(1), mockAction(2, "recall", "no-links")] : [mockAction(index + 2)],
  checks: index === 0 ? [mockCheck(500)] : [],
})) : [];
if (scenario === "overview") window.quickStartMock.pending.push({platform_id: "p2", group_id: "g", message_id: "same",
  cursor: 100, content: "另一个群的消息", content_truncated: false, execution_allowed: true, actions: [mockAction(99)], checks: []});
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
const notificationRecipients = [
  {target: "bot-a:FriendMessage:100", nickname: "小林", alias: "", platform: "bot-a", session_id: "100"},
  {target: "bot-b:FriendMessage:100", nickname: "小林", alias: "值班管理员", platform: "bot-b", session_id: "100"},
  {target: "bot-a:FriendMessage:101", nickname: "小张", alias: "", platform: "bot-a", session_id: "101"},
];
window.AstrBotPluginView = {
  async ready() { await ready; },
  async apiGet(endpoint, params) {
    window.quickStartMock.requests.push(endpoint);
    if (endpoint === "settings" && scenario === "settings-error") throw new Error("unavailable");
    if (endpoint === "notification-recipients") {
      if (window.quickStartMock.failRecipientLookup) throw new Error("unavailable");
      const search = params.search || "";
      const matches = notificationRecipients.filter(row => [row.nickname, row.alias, row.target].some(value => value.includes(search)));
      const page = Number(params.page || 1);
      return {recipients: matches.slice((page - 1) * 2, page * 2), has_more: matches.length > page * 2,
        selected: notificationRecipients.find(row => row.target === params.selected) || null};
    }
    if (endpoint === "groups" && scenario === "groups-error") throw new Error("unavailable");
    if (endpoint === "ui-state" && scenario === "ui-state-error") throw new Error("unavailable");
    if (endpoint === "overview-layout") {
      if (window.quickStartMock.failLayoutRead) throw new Error("unavailable");
      return JSON.parse(sessionStorage.getItem(layoutKey) || '{"cards":[]}');
    }
    if (endpoint === "pending-reviews") {
      if (window.quickStartMock.failPending) throw new Error("unavailable");
      const matching = window.quickStartMock.pending;
      const rows = matching.filter((item) => params.before_id === undefined || item.cursor < params.before_id);
      const limit = params.limit || 20;
      return structuredClone({messages: rows.slice(0, limit), total_messages: matching.length,
        total_actions: matching.reduce((sum, item) => sum + item.actions.length, 0),
        total_checks: matching.reduce((sum, item) => sum + item.checks.length, 0),
        oldest_at: matching.length ? "2025-01-01T00:00:00Z" : null,
        next_before_id: rows.length > limit ? rows[limit - 1].cursor : null});
    }
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
      "image-model-options": {providers: [{id: "model-a", model: "model-a", modalities: ["text", "image", "tool_use"],
        image_capability: "image", tool_capability: "tool_use"}], framework_caption_provider_id: ""},
      platforms: [{id: "p", type: "qq_official"}], groups, rules, messages: [],
      "available-groups": {groups: [{platform_id: "p", group_id: "g", name: "测试群"}], history_available: true},
      summary: {total_messages: 0, status_counts: {}, recent_24h: {messages: 0, proposed: 0, no_action: 0, failed: 0, review: 0}},
    };
    return structuredClone(data[endpoint] ?? []);
  },
  async apiPost(endpoint, body) {
    window.quickStartMock.requests.push(endpoint);
    if (endpoint === "notification-recipient-labels") {
      return {recipients: notificationRecipients.filter(row => body.targets.includes(row.target))};
    }
    if (endpoint === "ui-state") {
      window.quickStartMock.uiStateWrites.push(structuredClone(body));
      if (scenario === "slow-tab-save" && body.active_tab === "rules") {
        await new Promise((resolve) => setTimeout(resolve, 200));
      }
      uiState = {platform_id: "", group_id: "", active_tab: "status", ...uiState, ...body};
      sessionStorage.setItem(uiStateKey, JSON.stringify(uiState));
      return structuredClone(body);
    }
    if (endpoint === "overview-layout") {
      if (window.quickStartMock.failLayoutSave) throw new Error("unavailable");
      sessionStorage.setItem(layoutKey, JSON.stringify(body));
      return structuredClone(body);
    }
    if (endpoint === "execute-action") {
      window.quickStartMock.confirmationPosts.push(structuredClone(body));
      if (window.quickStartMock.failConfirmation) throw new Error("unavailable");
      for (const item of window.quickStartMock.pending) item.actions = item.actions.filter((action) => action.id !== body.intent_id);
      window.quickStartMock.pending = window.quickStartMock.pending.filter((item) => item.actions.length || item.checks.length);
      return structuredClone(window.quickStartMock.confirmationResult || {status: "succeeded", current_status: "succeeded", reason_code: null});
    }
    if (endpoint === "execute-rule") {
      window.quickStartMock.ruleConfirmationPosts.push(structuredClone(body));
      if (window.quickStartMock.failRuleConfirmation) throw new Error("unavailable");
      const target = window.quickStartMock.pending.find((item) => item.platform_id === body.platform_id
        && item.group_id === body.group_id && item.message_id === body.message_id);
      const ids = target ? target.actions.filter((action) => action.rule_id === body.rule_id).map((action) => action.id) : [];
      if (target) target.actions = target.actions.filter((action) => action.rule_id !== body.rule_id);
      window.quickStartMock.pending = window.quickStartMock.pending.filter((item) => item.actions.length || item.checks.length);
      return structuredClone(window.quickStartMock.ruleConfirmResult
        || {actions: ids.map((id) => ({intent_id: id, status: "succeeded"})), stopped: false, remaining: 0});
    }
    if (endpoint === "review-close") {
      window.quickStartMock.closePosts.push(structuredClone(body));
      if (window.quickStartMock.failClose) throw new Error("unavailable");
      let closed = 0;
      for (const item of window.quickStartMock.pending) {
        const keep = item.actions.filter((action) => body.target === "rule_actions"
          ? !(action.rule_id === body.rule_id && item.platform_id === body.platform_id
            && item.group_id === body.group_id && item.message_id === body.message_id)
          : !(body.target === "action" && action.id === body.intent_id));
        closed += item.actions.length - keep.length;
        item.actions = keep;
        item.checks = item.checks.filter((check) => !((body.target === "intent_check" && check.kind === "intent" && check.intent_id === body.intent_id)
          || (body.target === "event_check" && check.kind === "event" && check.event_id === body.event_id)));
      }
      window.quickStartMock.pending = window.quickStartMock.pending.filter((item) => item.actions.length || item.checks.length);
      if (body.target === "rule_actions") return {closed, already_closed: 0, closed_by: "admin", closed_at: "2026-01-01T00:00:00Z"};
      return {closed: true, already_closed: false, closed_by: "admin", closed_at: "2026-01-01T00:00:00Z"};
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
      const previous = settings.group_overrides[key] || {};
      const overrides = body.overrides ? {...previous, ...body.overrides} : {...previous};
      if (body.overrides) {
        const chatFields = ["chat_query_items", "chat_show_rules", "chat_show_mute_target"];
        const replacesList = chatFields.some(field => field in body.overrides);
        for (const property of Object.keys(previous)) {
          const preserveChat = property === "chat_query_inherit" || (!replacesList && chatFields.includes(property));
          if (!preserveChat && !property.startsWith("shared_violation_") && !(property in body.overrides)) delete overrides[property];
        }
        if (overrides.chat_query_inherit === true) delete overrides.chat_query_inherit;
      } else {
        delete overrides.shared_violation_rules;
        delete overrides.shared_violation_groups;
        if (body.shared_violation_groups.length) overrides.shared_violation_groups = body.shared_violation_groups;
      }
      if (Object.keys(overrides).length) settings.group_overrides[key] = overrides;
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
  async subscribeSSE(_endpoint, callbacks) { window.quickStartMock.stream = callbacks; return "test"; },
  async unsubscribeSSE() {},
};
