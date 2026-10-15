# 按需扩展

不限定Agent只能使用现有工具。用户需要其他平台、系统或能力时，先调查环境，再添加最小可复用实现；不建设大型插件框架，不因添加工具自动扩大权限。

## 先选最小接入方式

1. 用户已有文件：优先[通用导入](importing.md)，不强行走QQ/微信取钥流程。
2. 另一种导出格式：复制`examples/adapters/jsonl_adapter.py`，修改platform、formats、load_rows映射，继承ImportSource的校验、存储、资源准备及去重流程。脚本可直接运行并提供完整CLI，样例不代表已连接某厂商服务。
3. 客户端/API读取：明确账号、群、时间和授权，区分机器人接收、新消息与完整历史，再实现下述来源接口。优先官方API或已授权本地来源，不能假定可拉任意群全部历史。

真实来源信息可缺失；有说明直接写通知或待办正文。不新增必填来源字段。内部来源命名空间、稳定分组/消息ID仍用于范围隔离、权限、去重与恢复，不等同真实账号或群。

## 显式注册的来源接口

`agent_sources.py`的SOURCE_FACTORIES是受信源码注册表；get_source统一创建来源，AgentConfig、AgentService和附件准备按此分派。新增可信工厂使用`register_source('new_name', Factory)`，同名拒绝覆盖；platform须与注册名一致。没有从配置/消息加载模块、自动发现插件或执行下载脚本。

每个适配器提供：

| 接口 | 约定 |
| --- | --- |
| platform / capabilities | 真实能力和系统范围；不支持实时/远程下载就如实声明false |
| configure(payload) | 返回account_id、paths及必要非敏感连接设置；身份稳定且账号隔离，不保存明文密钥 |
| validate_scope(group_id,since,until) | 校验目标分组与明确时间范围，不删除旧的QQ/微信校验 |
| context(root,source) | 创建仅属于该来源的运行配置；不借其他账号媒体根、权限或凭据 |
| check(config) | 不查消息正文的检查 |
| connect(config,pids,authorized) | 仅适用时执行显式授权取钥；不适用则明确unsupported_operation |
| groups(config) | 返回groups元数据，单项platform/group_id/name/remark，不返回正文 |
| read(config,request) | 返回messages、snapshots及可选timings/parse_errors；只读request.scopes中的platform/group_ids/since/until |
| attachment_grants(message) | 该消息所需images_to_agent/documents_to_agent集合；未知类型不可默认放行 |
| asset_allowed(config,path) | 检查准备资源的源路径属于当前来源；配合私有输出路径与缓存验证 |
| prepare(config,messages,output,pid) | 仅处理选定消息，返回items资源清单；不自动下载/执行；source_id用于消息内资源关联 |

file_import:true仅用于继承/完成ingest接口的**文件收件箱**：ingest(config,group,file,format,timestamp)返回messages、counts、snapshots及可选notice，并先持久保存导入数据；统一入口随后持久登记待分析批次，不推进获取游标。默认read仅消费其已登记队列，不再扫描外部文件；显式--since可扫描已导入库。真正实时/API来源不能错误声明此能力，否则日常read不会拉新消息。

统一消息至少包含platform、group_id、稳定source_id/message_id（source_id在同来源各群之间也须唯一，避免附件混淆）、整数timestamp、text、body_status及attachments。读取器必须遵守授权范围，统一处理库还会复验。无法确认原时间时标明timestamp_basis:imported或其他明确状态，不让Agent把接收时间当作发送时间。未知/损坏正文与附件如实保留pending。

QQ/微信仍由LocalSource实现，调用既有ReadScope、messages.read_selected和attachments.collect，不改变旧命令。agent_config.PLATFORMS和chatnotice.ReadScope仍是**旧客户端能力集合**，不是新注册表；新来源通过自身validate_scope校验，不必向旧客户端解密流程塞新平台名。

## 需要一起检查的代码

- `agent_config.py`：来源配置、命名空间身份、群范围、版本及并发校验。
- `agent_sources.py` / `agent_imports.py`：工厂、能力和上述接口；文件格式优先复用ImportSource。
- `agent_processing.py`：消息身份、正文/附件哈希、解析状态、处理阶段和幂等关联；不能绕过获取与分析分离。
- `agent_assets.py`：附件授权、批量准备、路径限制和缓存失效；prepared仅表示资源可读，不是已分析。
- `agent_cli.py` / `docs/agent/commands.md`：需要新参数时保留版本化JSON和有界错误，不破坏旧调用。
- `tests/test_imports.py`：标准库、无真实客户端的导入/适配器测试；真实连接另做明确授权验证。

独立工具可先通过todos apply管理待办，但不会自动获得统一授权、去重和处理记账；接入这些能力需实际集成并测试。不要用另起一套直接写JSON/SQLite的脚本绕过校验。

## 跨平台与验证

文件导入、配置、SQLite处理与待办存储不依赖Windows API；CI为导入合成测试配置Windows/macOS/Linux矩阵。桌面窗口需单独实测，客户端读取仍依赖Windows QQ/微信；其他系统需实际来源/凭据保护适配，不能删除平台检查或退回明文密钥后宣称支持。

用合成数据验证范围、账号隔离、重复调用、冲突和中断恢复；不为开发读取真实聊天或扫描客户端内存。临时材料、个人凭据和消息只放私有位置，不进范例/源码。新增访问、上传、内存读取、下载、依赖安装均按用户授权执行；保留旧状态，迁移先备份。仅报告已实现验证的能力，没有相应客户端/权限就明确尚未验证。
