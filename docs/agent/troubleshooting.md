# 故障处理

只修复当前阻塞项，不反复跑全套诊断。不得将失败报告为“没有消息”。

| 错误/状态 | 应对 |
| --- | --- |
| `component_location_required` | 询问组件和Python绝对路径，运行agent_locator.py并按需--remember；不搜索整盘 |
| `groups_required` / `source_not_found` | 首次进入配置流程，或请求选择已保存来源/群 |
| `confirmation_required` | 向用户展示配置差异并确认后再保存，不自行补授权 |
| `config_conflict` | 重新inspect，合并用户希望的修改，再预览确认，不覆盖其他修改 |
| `config_busy` | 等待其他配置操作结束；若进程曾崩溃，请用户确认无写入者后再移除锁文件，不自动抢锁 |
| `credentials_unavailable` / `authorization_required` | source check定位凭据状态；说明用途并取得本次授权和本人PID后才connect，不静默扫描 |
| `key_acquisition_failed` | 核对本人账号路径、登录状态、主进程PID、Python位数及客户端版本；不关闭安全防护、不反复扫描 |
| `path_not_found` / `databases_not_found` | 询问真实保存位置，候选发现只查已知目录；不把路径错误当作格式升级 |
| `scope_denied` | 群已撤权、停用、时间越界或附件权限不足。明确说明；只有用户确认扩权后才能改配置，不改原读取请求绕过 |
| `stale_batch` / `message_changed` | 按当前配置重新read，不继续读旧磁盘文件。复用已成功的待办操作ID和关联避免重复 |
| `message_not_delivered` | 先用批次分页获取该消息，不能直接猜测消息ID提交 |
| `cache_incomplete` / `cache_account_unverified` / `cache_scope_invalid` | 拒绝历史复用，不自动回退到读取客户端。解释快照范围或账号来源不足，询问是否重新读 |
| `dependency_missing` | 核对项目解释器；取得安装同意后依README在项目虚拟环境安装requirements，不改全局Python |
| 附件 `pending` / `unsupported_or_no_attachment` | 保留实际阶段与原因。缺失资源不自动下载；WXGF需要用户安装可信FFmpeg；不会读图则告知当前Agent能力不足 |
| `attachment_pending` / `body_pending` / `processing_incomplete` | 不提交done；完成可完成的阶段，把未知或失败阶段记pending |
| 待办单项 `ok:false` | 其他项可能已成功，检查每项结果。按ID查当前值，自行判断修订方式；不盲重试覆盖用户编辑 |
| operation_id内容冲突 | 同一操作重试要完全同参；语义变化改用新ID，并明确patch已有事项，不重新create重复任务 |
| `config_version` / `state_version` / `unknown_database` | 停止写入，使用匹配版本或恢复正确备份，不重建覆盖原库 |
| `unsupported_os` / `unsupported_source` | 检查当前适配器能力；用户需要时可按[扩展指引](extending.md)新增可复用工具或适配，不靠填配置/删除校验伪装支持 |
| `unsupported_import_format` / `invalid_encoding` / `invalid_image` / `import_version` | 按[导入格式](importing.md)修正或添加明确格式适配，不执行文件、不猜未知编码 |
| `duplicate_message_id` | 同文件先去重或提供不同稳定ID；相同ID修订应在下一次导入，不靠随机ID重复建任务 |
| `hash_mismatch` / `file_changed` | 保留pending，核对用户选定文件和哈希；不静默接受被改动的附件 |
| `input_too_large` | 依明确大小/数量上限分批导入，不截断丢记录 |
| `operation_failed` | 先按错误上下文核对参数和版本，必要时运行现有doctor或合成测试；不输出正文、密钥或完整数据库调试 |

## 中断恢复

已读消息先保存在处理库，acquired_until不是完成进度。重新read会返回未完成消息。若仅需继续已知批次，可以read --batch，不宣称这是最新同步。

文件导入若在收件箱落库后、处理库登记前中断，按当前授权以同一文件和稳定ID再次import；也可显式read --since扫描已经导入的收件箱。日常read消费已登记队列，不重新读取外部原文件；详见[导入说明](importing.md)。

待办写入和处理记账属于两个库，不是跨库原子事务：先重用相同operation_id重放待办操作，检查结果，然后commit。即使期间用户改过待办，已成功操作的重放也不再次改写。

## 停用和清理

停用/撤销群后，旧批次和附件准备接口都要重新校验授权，不能通过缓存继续访问。授权不能撤回外部Agent已收到的内容，也不能阻止拥有不受限Shell的程序直接读取磁盘；需要强隔离时必须限制Agent权限。

cleanup.py的runtime模式也会删除state中的待办、完成进度和凭据，不是日常清理临时文件命令。只在用户明确需要时先预览，再单独确认执行。更新软件不运行cleanup。
