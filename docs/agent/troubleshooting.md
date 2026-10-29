# 故障处理

工具用法用 `agent_cli.py --help` 查看；配置与取钥用 `agent_setup.py --help`。读取失败与“没有消息”是不同结果。

| 错误或状态 | 排查方式 |
| --- | --- |
| `component_location_required` | 提供组件和 Python 绝对路径，运行 `agent_locator.py` 重新定位 |
| `groups_required/source_not_found` | 用 setup 的 inspect 查看配置，按[配置指南](configuration.md)选择来源与群 |
| `confirmation_required` | 预览配置差异并确认后保存 |
| `config_conflict` | 重新 `agent_setup.py inspect`，基于最新 revision 合并修改 |
| `config_busy` | 等待其他写入者；崩溃遗留锁需确认没有写入者后再处理 |
| `credentials_unavailable/authorization_required` | setup 的 source check 检查凭据；需要取钥时用 source connect，提供本人 PID 和本次内存授权 |
| `key_acquisition_failed` | 核对账号路径、登录状态、主进程 PID、Python位数及客户端版本 |
| `path_not_found/databases_not_found` | 核对保存位置，使用 source candidates 的有界候选 |
| `scope_denied` | 检查群是否停用、撤权、时间越界或缺少媒体权限；改授权使用 setup |
| `stale_batch/message_changed` | 按当前配置重新 read，保留已成功操作ID和待办关联 |
| `message_not_delivered` | 先用批次分页取得消息，再分块、准备附件或提交结果 |
| `cache_incomplete/cache_account_unverified/cache_scope_invalid` | 历史导出不能覆盖或验证当前请求；不会自动回退客户端 |
| `dependency_missing` | 核对项目解释器，聊天/转换依赖见 README 的 requirements 安装命令 |
| 附件 `pending/unsupported_or_no_attachment` | 查看缺失或格式原因；WXGF可能需要FFmpeg，准备成功也不等于理解完成 |
| `attachment_pending/body_pending/processing_incomplete` | 用 pending 记录未完成阶段，而非 done |
| 待办单项 `ok:false` | 检查逐项结果，使用 read --id 查询当前值；其他项可能已经成功 |
| operation_id 内容冲突 | 完全同参重试才复用ID；新修订使用新操作ID并 patch 已有待办 |
| `config_version/state_version/unknown_database` | 使用匹配版本或正确备份，不重建覆盖原库 |
| `unsupported_os/unsupported_source` | 核对来源能力；新平台需实际[适配](extending.md)，不是只改配置 |
| `unsupported_import_format/invalid_encoding/invalid_image/import_version` | 按[文件格式](importing.md)修正输入或实现格式映射 |
| `duplicate_message_id` | 同文件区分稳定ID；同ID修订在后续导入提交 |
| `hash_mismatch/file_changed` | 核对选定文件和哈希，附件变化会使缓存失效 |
| `input_too_large` | 按大小或数量上限分批，不截断正文 |
| `operation_failed` | 核对参数与版本，按需 inspect 或运行合成测试 |

## 图片取钥

普通 read 不扫描内存。确认缺少微信图片密钥后，用配置入口对已交付消息处理：

```text
python -B -X utf8 agent_setup.py images --batch B --message K --pid PID --authorize-memory-read
```

`--message` 可重复，授权仅本次有效。文档及普通本地资源准备使用 `read --attachments`，不需要因此授权内存扫描。

## 中断恢复

消息先持久接收，获取位置不等于完成进度。重新 read 会返回未完成项；`read --batch B` 可继续已知批次，但不表示同步了最新消息。

文件导入中断，用相同文件和稳定ID重试 `read --file ABS_FILE --group KEY --confirm`；也可用显式时间范围扫描已导入内容。日常 read 不重新读取外部原文件。

待办与处理库不是原子事务。重试原 write，成功操作通过同一 `operation_id` 重放，不重复改写，失败项继续 pending。字段冲突通过 baseline 和当前值处理。

## 升级与清理

升级保留 `config.json`、`state/`、`exports/` 和自己的 `AGENTS.md`。初始化不覆盖已有内容，规则文件不替代来源授权。

`cleanup.py runtime` 也会删除待办、进度与凭据，不是仅删除临时文件。清理先预览，再确认执行；不用于升级或失败重试。

问题反馈使用合成数据和错误类别，私有资料范围见 [SECURITY.md](../../SECURITY.md)。
