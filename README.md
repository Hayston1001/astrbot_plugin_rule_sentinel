# Rule Sentinel

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.29.3-24292f?style=flat-square)](https://github.com/AstrBotDevs/AstrBot)
[![QQ 官方机器人](https://img.shields.io/badge/QQ-official%20bot-12B7F5?style=flat-square&logo=qq&logoColor=white)](https://q.qq.com/)
[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-3DA639?style=flat-square)](LICENSE)

Rule Sentinel 是面向 QQ 官方群的 AstrBot 群规则管理插件. 管理员用自然语言编写群规并配置处理动作, 由模型判断群消息是否违规. 命中规则后, 插件自动执行预设的 actions, 也可以设为需人工确认. 判定/执行意图和平台结果都可追溯.

## 功能一览

- **自然语言规则** - 每条规则是一段管理员编写的群规, 不需要正则或关键词列表. 规则可以启用, 停用, 设置优先级, 并保留每次保存的历史版本
- **动作编排** - 一条规则可以配置发言, 禁言和撤回. 动作可排序, 每项可设为自动/人工确认
- **管理页面** - 在 AstrBot Dashboard 中查看运行状态, 判定记录, 规则, 群配置和监控设置, 不必编辑配置文件
- **可追溯结果** - 每次判定记录结果, 命中的规则和版本, 模型延迟, 动作执行状态和平台返回

## 运行要求

| 项目 | 要求 |
| :---: | :---: |
| AstrBot 版本 | >=4.29.3 |
| 消息平台 | QQ 官方机器人, 仅处理群消息 |
| 机器人权限 | 全量接收&主动发言&群管理员 |
| 判定模型 | AstrBot 中已配置的 LLM 提供方 |
| 网络 | 可访问 QQ 开放平台接口 |

## 管理页面

| 页面 | 用途 |
| :---: | --- |
| 运行状态 | 查看监控是否运行, 默认判定方式, 真实执行开关, 已知群和允许监控的群; 按群查看最近 24 小时的消息数, 拟执行数, 未违反规则数, P95 延迟和最近活动 |
| 判定记录 | 按时间查看消息判定, 筛选结果和执行状态, 搜索已加载记录, 展开详情查看理由, 原文, 动作和平台结果, 并对人工确认的动作执行确认 |
| 群规则 | 按群查看规则卡片, 新建, 编辑, 启用或停用规则, 调整动作顺序, 查看历史版本 |
| 群管理 | 添加群, 设置群备注, 读取 QQ 群名, 开关群监控, 覆盖本群模型, 策略, 执行方式和受保护目标提示 |
| 监控设置 | 配置真实执行总开关, 群名单模式, 全局默认, 判定模型, 理由语言, 消息记录, 运行限制和通知 |

### 判定流程

1. AstrBot 先完成白名单, 会话, 限流和内容安全检查, 通过后的群消息才进入插件
2. 插件检查群名单, 群监控开关, 是否有已启用规则, 以及接收缓冲和限流
3. 消息进入每群有界队列, 由判定模型得出结果和动作提议
4. 服务端复核群, 规则版本, 动作类型, 时长和目标发送者, 再持久化执行意图
5. 自动动作调用 QQ 官方接口执行; 人工确认动作等待确认
6. 判定, 提议, 执行结果和失败原因写入审计, 页面和实时流读取这些记录

> [!NOTE]
> 以下消息不会进入判定:
> - 机器人自己的消息
> - 已命中命令处理器的消息
> - 非文本消息
> - 没有启用规则的群消息
> - 超长消息
> - 被限流或队列已满的消息

## Star History

> [!TIP]
> 如果你喜欢这个插件, 请为我点个 Star

<a href="https://www.star-history.com/?repos=hayston1001%2Fastrbot_plugin_rule_sentinel&type=date&releases=&legend=bottom-right">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=hayston1001/astrbot_plugin_rule_sentinel&type=date&theme=dark&legend=bottom-right" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=hayston1001/astrbot_plugin_rule_sentinel&type=date&legend=bottom-right" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=hayston1001/astrbot_plugin_rule_sentinel&type=date&legend=bottom-right" />
 </picture>
</a>

## License

[MIT](https://github.com/Hayston1001/astrbot_plugin_rule_sentinel/blob/main/LICENSE)
