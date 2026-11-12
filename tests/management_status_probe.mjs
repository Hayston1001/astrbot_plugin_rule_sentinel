import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";

const source = await readFile(process.argv[2], "utf8");
const nameStart = source.indexOf("function groupKey(");
const nameEnd = source.indexOf("\nfunction updateGroupOptions(", nameStart);
assert.ok(nameStart >= 0 && nameEnd > nameStart);
const makeNames = new Function("knownGroups", `${source.slice(nameStart, nameEnd)}\nupdateReadableGroupNames(); return groupName;`);

const groups = [
  {platform_id: "p1", group_id: "long-one", alias: "讨论群"},
  {platform_id: "p2", group_id: "long-two", alias: "讨论群"},
  {platform_id: "p1", group_id: "long-three", alias: "", platform_name: ""},
  {platform_id: "p2", group_id: "long-four", alias: "", platform_name: ""},
  {platform_id: "p1", group_id: "long-five", alias: "备注", platform_name: "平台名称"},
];
const names = makeNames(groups);
assert.deepEqual(groups.map(names), ["讨论群 (1)", "讨论群 (2)", "未命名群 (1)", "未命名群 (2)", "备注"]);
const reversedNames = makeNames([...groups].reverse());
assert.deepEqual(groups.map(reversedNames), groups.map(names));
assert.equal(names({platform_id: "other", group_id: "unknown"}), "未命名群");

const stateStart = source.indexOf("function groupMonitoring(");
const stateEnd = source.indexOf("\nfunction chooseGroup(", stateStart);
assert.ok(stateStart >= 0 && stateEnd > stateStart);
const makeState = new Function("currentSettings", "runtimeStatus", `${source.slice(stateStart, stateEnd)}\nreturn groupMonitoringState;`);
const group = {platform_id: "p", group_id: "g"};
const settings = {monitoring_groups: ["p/g"], monitoring_mode: "allowlist"};
assert.deepEqual(makeState(null, null)(group), ["状态不可用", "muted"]);
assert.deepEqual(makeState({...settings, monitoring_groups: []}, {monitoring: "dry_run"})(group), ["已停用", "muted"]);
assert.deepEqual(makeState(settings, null)(group), ["状态不可用", "muted"]);
assert.deepEqual(makeState(settings, {monitoring: "disabled"})(group), ["监控未启动", "warning"]);
assert.deepEqual(makeState(settings, {monitoring: "dry_run"})(group), ["允许监控", "success"]);
