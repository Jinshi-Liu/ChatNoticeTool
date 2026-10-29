# 项目结构

## 数据流

```text
QQ / 微信本地数据库 ── 快照、认证解密、WAL校验 ─┐
钉钉 V3 本地群文本 ── 实验来源、快照解密 ─────┤
用户选择的文件 ── 导入收件箱 ─────────────────┤
                                            ↓
agent_setup.py ── 来源、群与内容交付授权 → agent_cli.py read
                                            ↓
                              处理库、分页、正文与附件路径
                                            ↓
                                    外部Agent理解 / 看图
                                            ↓
                                  agent_cli.py write
                                     ↓             ↓
                               待办与操作ID     处理结果与关联
                                     ↓
                                  桌面待办
```

组件不调用模型。`AGENTS.md` 是用户自行填写的规则文件，默认空白；UI“文档”窗口只负责编辑和保存，加载方式由 Agent 宿主决定。

## 入口与职责

| 层 | 文件 | 职责 |
| --- | --- | --- |
| 日常接口 | `agent_cli.py` | 仅 `read/write`，输出版本化 JSON；消息、文件、附件与独立待办共用入口 |
| 配置 | `agent_setup.py`, `agent_config.py` | 检查、初始化、来源连接、群授权预览与保存；单次授权取钥 |
| 定位 | `agent_locator.py` | 定位项目 Python，返回指向 `agent_cli.py` 的 `argv_prefix` |
| 服务与来源 | `agent_service.py`, `agent_sources.py`, `agent_imports.py` | 请求门面、显式工厂注册、客户端读取与文件收件箱 |
| 钉钉实验来源 | `dingtalk_export.py`, `dingtalk_probe.py` | V3 本地群文本、UID 配置与快照解密，复用同一 read/write |
| 公共基础 | `project_core.py` | 公共配置、范围及路径等基础函数 |
| 密钥与数据库 | `memory_reader.py`, `cipher.py`, `database_reader.py` | 有界只读取钥、DPAPI、临时快照、页面认证、WAL与一致性检查 |
| 消息 | `groups.py`, `messages.py`, `wechat_content.py` | 群元数据、范围读取、解析与精简视图 |
| 附件 | `attachments.py`, `document_formats.py`, `agent_assets.py` | 本地资源定位、转换、授权检查与缓存 |
| 处理进度 | `agent_processing.py` | 消息版本、获取游标、批次、分页、处理阶段和待办关联 |
| 待办 | `todo_store.py` | SQLite待办、稳定ID、幂等操作和字段基线；不是独立CLI |
| 窗口 | `todo_widget.py`, `widget_ui.py`, `todo_detail.py`, `glass_style.py` | 列表与编辑、规则文本窗口、独立置顶及窗口偏好 |
| 生命周期 | `local_setup.py`, `cleanup.py`, `*.cmd` | 补齐私有配置、所有权限定清理、本地启动 |
| 验证与范例 | `tests/`, `examples/adapters/` | 合成测试、read/write基准、受信代码适配器范例 |

`groups/messages/attachments/memory_reader` 是内部模块，不提供旧的独立CLI包装。详细命令集中在[命令参考](docs/agent/commands.md)。

## 状态与恢复

- `read` 按来源账号合批，同批分页复用消息，不重复解密。默认只准备本页已授权纯图片，`--attachments` 扩展为本页所有授权媒体。
- 获取游标表示消息已持久接收，不是分析完成。首次默认取最近 `max_days` 窗口，更早历史显式回补。
- 消息身份与内容哈希用于去重；未完成项继续返回。`context` 是已处理消息的只读上下文。
- `write` 合并待办操作与结果提交。待办库和处理库不提供跨库原子事务，通过稳定 `operation_id` 重放恢复；字段 `baseline` 保留用户编辑、完成与移除状态。
- 批次、分块与附件访问复验当前授权和配置版本；改配置可能使旧批次失效。手写规则文件不替代来源权限。
- `read --from-export` 校验并筛选已有历史导出，不把历史快照当作当前同步，也不在失败时自动回退读取客户端。

## 私有资料

`config.json`、`state/`、`exports/`、`runtime/` 与所有权标记留在本机。
`state/imports/` 保存导入内容及附件副本，处理库保存选定消息明文，待办库保存事项与偏好；DPAPI只保护相应凭据，不加密这些消息库。

更新保留个人资料和用户填写的 `AGENTS.md`；初始化不覆盖已有内容。清理先预览再确认，不作为升级步骤。

[使用说明](README.md) · [扩展接口](docs/agent/extending.md) · [安全说明](SECURITY.md)
