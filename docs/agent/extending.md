# 扩展来源与格式

工具支持受信代码适配器，不内置模型。文件、客户端或 API 接入复用现有授权、处理批次和 write，不另建直接写待办库的流程。

## 接入方式

1. 已有文件：使用[通用导入](importing.md)。
2. 其他导出格式：参考 `examples/adapters/jsonl_adapter.py`，继承 `ImportSource`，修改 `platform/formats/load_rows`。
3. 客户端或 API：实现下述来源接口，并明确账号、群、时间、历史能力和凭据保存方式。

来源信息可缺失；内部命名空间与稳定消息ID用于隔离和去重，不要求虚构真实账号。新增来源不自动获得现有来源的权限。

## 工厂注册

`agent_sources.py` 的 `SOURCE_FACTORIES` 是显式受信注册表，`get_source` 供配置、读取和附件准备统一分派。
`register_source('new_name', Factory)` 拒绝覆盖同名注册，工厂 platform 与注册名一致。不会从配置、消息或附件自动加载模块。

JSONL 范例启动器在配置和读取进程中注册同一工厂。日常直接使用 read/write，配置加 `--setup`：

```text
python examples/adapters/jsonl_adapter.py --setup source configure --input -
python examples/adapters/jsonl_adapter.py --setup groups apply --input -
python examples/adapters/jsonl_adapter.py read --file ABS_FILE --group KEY --confirm
```

配置 JSON 与内置收件箱相同，platform 改为 `example_jsonl`；文件字段见 `examples/adapters/messages.example.jsonl`。

## 来源接口

| 接口 | 约定 |
| --- | --- |
| `platform/capabilities` | 声明实际能力与系统范围 |
| `configure(payload)` | 返回稳定 account_id、paths 和必要非敏感设置 |
| `validate_scope(group_id,since,until)` | 校验分组和时间范围 |
| `context(root,source)` | 生成仅属于当前来源的运行配置 |
| `check(config)` | 不查消息正文的检查 |
| `connect(config,pids,authorized)` | 显式授权取钥，不适用时返回 unsupported_operation |
| `groups(config)` | 群元数据，单项包含 platform/group_id/name/remark |
| `read(config,request)` | 仅读取 request.scopes，返回 messages、snapshots 和可选 timings/parse_errors |
| `attachment_kinds(message)` | 返回 image/document 媒体类别，无法处理时为 unsupported（不是权限） |
| `asset_allowed(config,path)` | 校验源资源路径属于当前来源 |
| `prepare(config,messages,output,pid)` | 只准备选定消息，返回 items 资源清单 |

文件收件箱声明 `file_import:true`，实现 `ingest(config,group,file,format,timestamp)`，返回 messages、counts、snapshots 及可选 notice，并先持久保存导入内容。统一入口登记处理批次，不推进获取游标；日常 read 消费已登记队列，显式时间范围才扫描导入库。实时/API来源不能错误声明此能力，否则日常读取不会拉新消息。

统一消息至少包含 platform、group_id、稳定 source_id/message_id、整数 timestamp、text、body_status 和 attachments。消息内 source_id 在同来源各群间也须唯一，避免资源关联混淆；缺少原时间时用明确的 timestamp_basis 标记，不能把接收时刻伪装成发送时刻。

QQ / 微信由 `LocalSource` 调用 `project_core.ReadScope`、`messages.read_selected` 和 `attachments.collect`。客户端能力集合与来源注册表分离，新来源通过自身 `validate_scope` 校验，不向客户端解密流程塞入新平台名。

## 相关模块与验证

- `agent_config.py`：来源身份、选群、配置版本与并发。
- `agent_sources.py/agent_imports.py`：工厂和导入格式。
- `agent_processing.py`：消息版本、获取与分析阶段、关联和恢复。
- `agent_assets.py`：媒体分类、路径和缓存失效。
- `agent_cli.py`：只保留 read/write；配置参数位于 `agent_setup.py`。
- `docs/agent/commands.md`：版本化 JSON 与参数参考。

独立工具也可用只含 operations 的 write 管理待办，但不会因此自动拥有来源授权或消息处理进度。

文件导入核心不依赖 Windows API，客户端读取仍依赖 Windows QQ / 微信。其他系统或服务需实际实现与验证，不能仅删除平台校验后声称支持。

使用合成数据覆盖范围、账号隔离、重复调用、字段冲突和中断恢复：

```text
python -B -X utf8 -m unittest discover -s tests -q
```

真实客户端测试另需对应环境和授权。性能改动用[合成基准](performance.md)记录，不把模拟结果当作真实聊天或模型速度。
