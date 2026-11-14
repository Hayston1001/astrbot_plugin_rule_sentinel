# 参与贡献

欢迎提交 issue 和 pull request. 本文说明本仓库的开发环境, 模块边界和提交要求, 目标是让改动在合入后仍然符合插件的安全假设: 消息, 模型输出和前端输入一律不可信, 真实管理动作必须先复核再执行.

## 提交 issue

- 新建 issue 时选择"问题反馈"模板, 它会要求填写 AstrBot 版本, 插件版本, 群类型, 复现步骤, 以及相关日志或截图
- issue 和其中的附件都是公开的, 提交前移除日志和截图里的隐私信息
- 安全漏洞不要公开提 issue, 请通过 GitHub [Report a vulnerability](https://github.com/Hayston1001/astrbot_plugin_rule_sentinel/security/advisories/new), 修复发布后由维护者公开细节

## 提交 PR

PR 使用模板 `.github/pull_request_template.md`. 测试由 CI 运行.

## 开发环境

- Python 3.12. AstrBot 自身要求 Python >=3.12, 插件跟随这个下限
- uv. 测试脚本用它创建隔离环境并从 PyPI 安装 AstrBot
- Node.js 22. 只用于检查管理页面的脚本语法
- 不需要预先 `pip install` 本仓库, 也不需要 QQ 凭据或真实群

```bash
git clone https://github.com/Hayston1001/astrbot_plugin_rule_sentinel.git
cd astrbot_plugin_rule_sentinel
python scripts/run_tests.py
```

## 仓库结构

| 路径 | 作用 |
| --- | --- |
| `main.py` | 唯一的 AstrBot 入口: 注册管理接口和唯一的群消息监听器, 把请求转给 `sentinel/`, 并负责插件生命周期 |
| `sentinel/` | 全部业务逻辑. 只放这里, 用包内相对 import, 不新增第二个入口 |
| `views/management/` | Dashboard 管理页面, 通过 `window.AstrBotPluginView` 桥接调用接口 |
| `tests/` | unittest 用例, 前端探针和 fixture |
| `scripts/` | 测试入口, 打包脚本, 平台与模型探针, 离线评估脚本 |
| `metadata.yaml` | 插件元数据. `version` 是版本号的唯一来源, `astrbot_version` 是版本下限 |
| `_conf_schema.json` | AstrBot 配置项定义 |

`sentinel/` 里的模块按职责分组:

| 分组 | 模块 |
| --- | --- |
| 消息入口与判定 | `event_intake`, `dry_run_queue`, `dry_run_worker`, `monitoring_policy`, `policy_guard`, `active_group_index`, `llm_decider`, `rule_selection`, `sentinel_core` |
| 模型调用 | `astrbot_llm`, `llm_protocol`, `prompts`, `circuit_breaker`, `decision_errors` |
| 规则与设置 | `rule_store`, `rule_management`, `settings_store`, `group_settings`, `plugin_settings`, `sentinel_defaults`, `action_config` |
| 执行与审计 | `mute_validation`, `mute_executor`, `mute_execution_store`, `qq_mute_platform`, `recall_validation`, `audit_safety` |
| 管理接口 | `management_service`, `main.py` 中的路由表 |
| 平台适配 | `qq_official_snapshot`, `group_discovery`, `main.py` 中的群消息监听器 |
| 运行支撑 | `rate_limiter`, `event_stream`, `notifications`, `message_retention`, `instance_lock`, `async_io` |

部分模块是原型, 例如 `platform_tap`, `platform_tap_manager`, `filter_probe`, `framework_session_gate`, `guarded_openai_invoker`, `provider_binding`. 它们的 docstring 标有 "not wired", "not registered" 或 "experimental", 表示尚未接入运行时或只被同类原型引用. 接入前先确认这个状态, 并同步删除标注.

## 记录状态

| 状态 | 含义 |
| :---: | --- |
| 未违反规则 | 模型判断消息没有违反任何已启用规则 |
| 拟执行 | 已生成动作意图, 但没有执行(关闭了真实执行) |
| 待确认 | 动作方式为人工确认, 等待管理员在记录详情中确认 |
| 全部成功 | 所有已配置的动作都执行成功 |
| 部分失败 | 部分动作成功, 部分失败或被平台拒绝. 请展开逐项查看 |
| 全部无法执行 | 平台或权限检查拒绝了所有动作, 例如目标是管理员或机器人, 或机器人权限不足 |
| 结果不明 | 平台返回无法确定的状态. 不会自动重试, 需要人工核查 |
| 跳过 | 消息进入了插件但没有进入判定, 例如被限流, 队列已满, 消息过长或消息类型不支持 |

## 测试

```bash
# 全部测试: 依次在 AstrBot 4.29.3 和最新版上运行, 与 CI 一致
python scripts/run_tests.py

# 只测指定 AstrBot
python scripts/run_tests.py --astrbot "astrbot==4.29.3"

# 没有 Node.js 时跳过页面脚本检查
python scripts/run_tests.py --skip-node

# 单个测试文件
python -B -m unittest discover -s tests -p "test_rule_store.py" -v
```

- `scripts/run_tests.py` 会创建隔离的 uv 环境, 从 PyPI 安装 AstrBot, 检查页面脚本语法, 再运行全部用例. 它不读你的本地 AstrBot 环境
- 测试不联网, 也不需要平台凭据: 使用模拟消息, 模拟模型响应和临时数据库, 不会写入真实插件数据目录
- `tests/` 没有 `__init__.py`, 所以单文件运行要用 `discover`, 不要写成 `python -m unittest tests.test_rule_store`
- 缺少 AstrBot 或 aiohttp 时, 相关用例会自动 skip; 没有安装 Node.js 时 `run_tests.py` 会报错, 需要加 `--skip-node`. 本地全绿不等于真实验证, 结论以 `run_tests.py` 的结果为准

### 新增测试

- 用例放 `tests/`, 文件名 `test_*.py`, 用标准库 `unittest`
- 需要加载 `main.py` 的测试参考 `tests/test_main_management.py`: 用 `sys.modules` 注入假的 `astrbot` 包, 不依赖真实框架
- 前端行为用 `tests/*.mjs` 探针和 `tests/fixtures/` 验证, 不要只靠肉眼看页面
- 改插件入口前先读 `tests/test_architecture_guard.py`: 它用 AST 断言守住边界, 包括只允许 `capture_group_message` 一个监听器, 监听器不得发送消息或调用 `stop_event`, 禁止出现 `PlatformTap` 和 `restrict_chat_setting`, 以及 `main.py` 与 `runtime_factory.py` 不得引用 `guarded_openai_invoker` 或 `provider_binding`

## 代码约定

- 代码, 标识符和注释用英文, 用户可见文档用中文. 行尾统一 LF, Windows 上也不要提交 CRLF
- Fail closed: 配置解析遇到非法值直接抛 `ValueError`, 不要静默回落到默认值
- 模型只提出动作, 不执行动作. 服务端复核群, 规则版本, 动作类型, 时长和目标发送者, 时长不超过 2592000 秒, 且必须来自已确认的规则
- 真实管理动作前先持久化审计意图. 持久化或权限核验失败就不执行; 平台结果不明时要求人工核查, 不自动重试
- 规则按"平台实例 + 群"隔离并带版本号. 修改走先预览再确认, 确认必须携带 `expected_version`, 版本冲突返回 409 并重新预览
- 判定后端只用管理员配置的动作和模板. 模型不提供发言内容, 目标或规则版本
- 同步 SQLite 调用走 `asyncio.to_thread`
- 持久状态只写到 `get_astrbot_plugin_data_path()/astrbot_plugin_rule_sentinel/`

### 管理接口

- 新增接口沿用 `main.py` 的 `_payload()`: 只接受 `application/json`, 限制 64 KiB, JSON 深度 64, 拒绝重复键和非有限数. 不要放宽这些检查
- 授权只认服务端 `request.username`: 允许非空的 Dashboard 用户名, 拒绝匿名和 `api_key:` 身份. 页面隐藏或按钮禁用不能当作授权依据
- 顶层 `json_response(None)` 经页面桥接后会变成空对象. 需要区分"未保存"和"读取失败"时返回明确的非空结构, 不能只在模拟 bridge 中验证

## 管理页面

- 页面文件是 `views/management/` 下的 `index.html`, `app.js`, `overview.js`, `motion.js`, `select.js` 和 `style.css`
- 页面通过 `window.AstrBotPluginView` 桥接调用接口, 不要自己 `fetch`
- 设计规范和交互选择维护在本地 `DESIGN.md`, 该文件不进入仓库. 调整界面前先读它, 改动后同步更新
- `run_tests.py` 会对四个 JS 文件执行 `node --check`, 语法错误会让测试失败
- 前端改动需要在真实 Dashboard 里打开确认, 模拟 bridge 无法覆盖桥接和流式渲染的真实行为

## 打包与发布

```bash
python -B scripts/package_plugin.py [--force]
```

- 默认输出 `dist/astrbot_plugin_rule_sentinel-<version>.zip`, 版本取自被写入包的 `metadata.yaml`
- 只收 `main.py`, `metadata.yaml`, `_conf_schema.json`, `README.md`, `CHANGELOG.md`, `LICENSE`, `logo.png`, `sentinel/`, `views/management/` 和两个离线评估脚本 `scripts/evaluate_dry_run.py`, `scripts/compare_dry_run.py`. 测试, 探针和本地文档不打包
- 包大小上限 16 MiB, 输出文件已存在时必须显式加 `--force`
- 新增运行时依赖的 Python 模块要放进 `sentinel/` 或 `views/management/`, 否则不会进入包

发布由维护者完成:

1. 在 `CHANGELOG.md` 增加 `## v<version>` 小节
2. 修改 `metadata.yaml` 的 `version`
3. 推送 `v<version>` tag

## 数据与安全

- **消息原文**: 成功进入队列的消息原文按平台实例和群单独保存, 默认保留 30 天. 保留期设为 0 时不保存这份原文, 但待处理队列仍需暂存判定所需的文本. 插件启动时, 保存设置时和之后每小时清理过期原文
- **模型数据**: 进入判定的群消息文本, 发送者标识和相关已启用规则会发送给所选模型服务商
- **密钥**: QQ 机器人凭据直接从 AstrBot 的 QQ 官方平台实例读取, 不写入插件数据库
- **备份**: 停止插件后复制整个插件数据目录即可备份. 恢复时保持目录结构不变

## 许可证

本项目使用 [MIT](https://github.com/Hayston1001/astrbot_plugin_rule_sentinel/blob/main/LICENSE) 协议. 提交贡献即表示同意该贡献以同样的协议发布, 请勿提交来源不明或授权不兼容的代码和资源.
