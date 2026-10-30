# 配置指南

工具用法见 [README](../../README.md) 和 `--help`。根目录 `AGENTS.md` 默认空白，由用户自行填写；它不是安装说明，也不替代来源授权。

## 1. 定位环境

在已知项目位置运行：

```text
python -B -X utf8 <项目>/agent_locator.py
```

返回的 `argv_prefix` 使用项目 Python 调用 `agent_cli.py`。配置工具使用同一个 Python，脚本换为 `<项目>/agent_setup.py`：

```text
<项目Python> -B -X utf8 <项目>/agent_setup.py inspect
<项目Python> -B -X utf8 <项目>/agent_setup.py init
```

`init` 补齐缺失配置，不覆盖已有配置、待办或用户填写的 `AGENTS.md`。桌面初始化入口 `local_setup.py` 也保留。

## 2. 选择来源

QQ / 微信来源需要本人账号的数据库和媒体目录。可提供绝对路径，或用有界候选发现：

```text
source candidates --platform wechat [--base ABS_DIR]
source configure --input -
source check --source SOURCE_ID
```

以上均为 `agent_setup.py` 子命令。候选只帮助找位置，不表示账号或格式已验证。来源按平台和数据库路径隔离，换账号目录不会继承旧授权。

缺少凭据时，打开并登录自己的客户端，确认主进程 PID，再对本次取钥授权：

```text
source connect --source SOURCE_ID --pid PID --authorize-memory-read
```

保存消息读取权限不会授权扫描内存。有效凭据可复用，不需要每次重新取钥。

仅处理用户文件时，配置 `platform:"import"`、可选 `collection:"inbox"` 和名称，不需要客户端路径、真实账号或取钥。见[文件导入](importing.md)。

钉钉实验来源使用 `platform:"dingtalk"`、本人 `uid` 和 `paths.account_dir`，复用相同 read/write；配置示例与当前能力见[钉钉 V3 实验读取](dingtalk.md)。

## 3. 选群与保存授权

```text
groups list --source SOURCE_ID [--contains 群名片段]
groups plan --input -
groups apply --input -
```

`list` 只查元数据，按 `next_offset` 翻页。同名群用来源账号和准确群ID区分。

群配置包含：

- 来源和准确群ID；文件导入可自行命名本地分组。
- 群名称和启停状态；已选启用群允许全部媒体，在线 Agent 会接收选定内容。
- 不保存时间范围、交付对象、分群补充说明或媒体权限。首次默认读最近7天，更早历史由每次读取的 `--since/--until` 显式指定；后端按7天操作窗口回补，不是权限限制。

参数和授权已确定时可直接 `apply`；`plan` 仅用于需要预览差异的情况，不是必经步骤。已明确的同一范围不重复询问。JSON schema、版本号和撤销请求见[命令参考](commands.md)。来源权限保存在配置中，手写 `AGENTS.md` 不会改变这些权限。

## 4. 日常与规则编辑

已有授权后直接 `agent_cli.py read`，理解内容后 `write --input -`；不需要反复检查或列群。

UI 顶部“文档”打开管理窗口：规则页以大字号编辑 `AGENTS.md`（初始0字），保存图标或 Ctrl+S 只保存规则，切页保留草稿。

群聊页只有一张连续只读表，列为平台标识（QQ / 微信 / 其他）、群名、群号 / 群 ID、最后读取。不同平台不分表、不嵌套滚动；悬停行或空白都滚动同一张表，Windows 滚轮及小 delta 累积使用统一速度。底部“修改此配置请与 Agent 沟通”固定不滚走。常规宽度不显示多余横向滚动条，窄窗确实溢出时才显示；长 ID 可悬停看全。

“最后读取”显示 MM-DD HH:MM，悬停显示完整日期、秒与时区。群页打开期间每5秒只查询本地配置及读取成功记录；切回群页也会刷新，不读取客户端、不写配置，不丢失滚动位置或选择。关闭会清理刷新、悬停回调及私有滚轮绑定，不影响其他窗口。群页没有新增、编辑、删除或保存入口。

最后读取按来源账号和群 ID 独立保存，不是全局时间；读取一个群不会更新其他群。同批实际读取的多个群可以显示相同时间。最后成功读取时间独立于获取游标和处理完成时间：仅在实际读取或显式文件导入成功并持久登记后更新；空结果也算成功。失败、历史缓存复用、仅消费导入队列、批次翻页、附件准备和 write 均不更新。旧状态没有此记录时显示“尚未读取”，不拿游标或旧批次时间冒充。

Agent 修改配置须确认及最新版本号；移除群不会删除历史消息或待办。宿主是否加载、何时重新加载规则由当前 Agent 决定。

## 私有数据与迁移

| 位置 | 用途 |
| --- | --- |
| `config.json` | 本地组件配置 |
| `state/agent-config.json` | 来源、选群及启停（schema v2） |
| `state/secrets.dpapi` | 当前 Windows 用户绑定的凭据 |
| `state/imports/` | 导入消息与选定附件副本 |
| `state/processing.sqlite3` | 消息明文、批次、处理阶段、关联及缓存索引 |
| `state/todos.sqlite3` | 待办、完成/移除状态、偏好与幂等记录 |
| `exports/agent-assets/` | 准备给 Agent 的附件 |
| `state/component-location.json` | 可选的项目与 Python 位置记录 |

消息和待办库不由 DPAPI 加密。撤权阻止后续工具访问，不收回 Agent 已接收内容，也不自动删除待办。

旧 schema v1 可只读兼容，inspect/群页不会改写磁盘。`inspect` 返回 `migration_required`；确认移除旧限制并允许已选群全部媒体后，用正式接口迁移：

```text
python agent_setup.py migrate --input -
```

stdin 为 `{"expected_revision":当前版本号,"confirmed":true}`。迁移保留来源、群 ID、名称及启停，删除 `since/until/max_days/recipient/instructions/grant`；确认的其他配置写入也会保存为 v2，新群请求不再接受这些旧字段。旧批次快照失效，重新 read 后保留去重、pending、获取游标和待办，不自动重分析。旧处理库在下一次正常后端操作时增加读取成功记录表，不从历史数据推断时间；只读群页不迁移处理库。

升级前关闭窗口，备份并保留 `config.json/state/exports` 和自己的 `AGENTS.md`，再更新源码；不运行清理来升级。
移动目录后重新定位；DPAPI凭据不能迁移到其他 Windows 用户。更改已授权来源路径时，先处理相关群授权，再重新配置。
