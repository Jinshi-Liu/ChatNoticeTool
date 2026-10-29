# 命令参考（schema_version=1）

日常入口是 `python -B -X utf8 agent_cli.py read/write`；配置入口是 `python -B -X utf8 agent_setup.py`。
示例中的路径、ID、批次和哈希使用实际返回值替换。

`agent_locator.py` 返回的 `argv_prefix` 指向 `agent_cli.py`。调用配置工具时，保留同一个 Python 和运行参数，将脚本换成项目内的 `agent_setup.py`。

成功：`{"schema_version":1,"ok":true,"data":{...}}`，退出码0。
失败：`{"schema_version":1,"ok":false,"error":{"code":"...","message":"...","next_actions":[]}}`，退出码2。

## read：消息、文件与待办

下列参数位于 `agent_cli.py` 后：

```text
read [--group KEY ...] [--since START] [--until now] [--limit 50] [--timings]
read --batch B [--cursor C] [--limit 50]
read --batch B --message K [--offset 0] [--chars 8000] [--raw]
read [--group KEY ...] --since START --until END [--reprocess]
read --group KEY --since START --until END --from-export ABS_EXPORT_DIR
read --file ABS_FILE --group KEY --confirm [--format auto] [--timestamp ISO_TIME]
read --todos [--cursor 0] [--limit 50] [--all]
read --id TODO_ID [--id TODO_ID ...]
```

### 读取与分页

- 默认读取当前已授权群的新消息、变化消息和未完成项，返回精简正文及本页已授权纯图片路径。不会扫描内存或调用模型。
- `--group` 可重复，使用准确的配置 key，不接受模糊群名。时间范围受已保存授权及 `max_days` 限制。
- 无游标且未指定 `--since` 时，从最近 `max_days` 窗口开始；`history_available_before/history_notice` 表示更早历史未扫描。显式 `--since` 用于历史回补。
- 后续增量读取使用重叠窗口和身份、内容哈希去重。获取游标仅表示已接收，不表示已理解；重叠窗口也不保证发现任意晚到历史消息。
- 返回 `message_key/content_hash`、处理阶段、已有 `task_ids` 和配置说明。`context` 是附近已处理消息的只读上下文，不重复提交结果。
- `next_cursor` 非空时用同一批次续读，不重新解密。`limit` 为1–200，实际页还受内容大小预算限制。
- `content_ref` 对应大正文分块，按 `next_offset` 读到结束。默认分块是精简视图；`--raw` 才读取完整脱敏原文，不是原始数据库行。
- `--reprocess` 重置实际读到消息的分析标记，不清除待办关联。`--from-export` 校验历史导出覆盖与来源，失败不自动回退客户端，不推进客户端获取游标。

### 附件

```text
read [--group KEY ...] --attachments
read --batch B --cursor C --attachments
read --batch B --message K --attachments
read [--group KEY ...] --no-prepare-images
```

`--attachments` 准备本页所有已授权媒体，包含文档；默认仅准备纯图片。`--no-prepare-images` 关闭默认图片准备。

`--batch B --message K --attachments` **只返回这个已交付消息的附件准备报告**，不返回正文分块；不带 `--attachments` 时，同样的 `--message` 用于读正文。

报告包含资源路径及 `prepared/pending/scope_denied/unsupported_or_no_attachment` 等状态。准备只访问消息引用的本地资源，不下载、不执行内容；图片与文档权限分别校验，混合或未知附件可能同时需要两种权限。
`prepared` 表示资源可用，不是 Agent 已理解。缓存复用仍检查权限和文件指纹。

缺少微信图片密钥时，在单次内存读取授权后使用配置工具：

```text
python -B -X utf8 agent_setup.py images --batch B --message K --message K2 --pid PID --authorize-memory-read [--timings]
```

### 文件和待办

文件读取只用于具有 `file_import` 能力的已授权收件箱。支持 JSON、UTF-8文本、图片与文档；`--format` 和 `--timestamp` 只用于所选文件，详细限制见[导入格式](importing.md)。返回首个处理页面，后续按批次分页，不必再次读取原文件。

`read --todos` 列待办，`--all` 包含已移除项；`read --id` 可批量准确查询，包含已移除记录。待办读取不查询聊天。

## write：待办操作与处理结果

```text
write --input - [--timings]
```

UTF-8 JSON 从标准输入传入；也可用 `--input ABS_JSON_FILE`。输入上限1MiB。

```json
{
  "batch_id":"B",
  "operations":[{
    "operation_id":"notice:task:1",
    "action":"create",
    "message_keys":["K"],
    "fields":{"title":"提交练习","due":"2030-10-13","due_label":"10/13 上课前","note":"题目和提交要求","optional":false}
  }],
  "results":[{"message_key":"K","content_hash":"H","status":"done"}]
}
```

独立管理待办时只传 `operations`，不带 `batch_id/results/message_keys`；无待办变化可只传 `batch_id/results`。

### 操作 schema

`operations` 每批最多200项，返回逐项结果。新增可省略 `id`，由 `operation_id` 派生稳定ID。相同操作ID和完全相同参数视为重放；同ID不同参数拒绝。

```json
{"operation_id":"notice:patch:1","action":"patch","id":"TODO_ID","message_keys":["K"],"fields":{"due":"2030-10-15"},"baseline":{"due":"2030-10-13"}}
```

可改字段：`title/due/due_label/note/source/optional`，只传变更字段及相应 `baseline`。`source` 是可选字段；来源说明可直接写 `note`。日期可为 `YYYY-MM-DD`、`YYYY-MM-DD HH:MM` 或 `null`。

- 完成/撤回：`action:"mark",value:true/false,baseline:{"done":0/1}`。
- 移除/恢复：`action:"remove",value:true/false,baseline:{"removed":0/1}`。
- 普通 create/patch 不重置完成或移除状态；create 不覆盖已有ID，patch 不修改已移除项。
- 冲突时按返回的 `current_id` 用 `read --id` 获取当前值，再决定修订方式。

### 结果 schema

```json
{
  "batch_id":"B",
  "results":[{
    "message_key":"K","content_hash":"H","status":"pending",
    "text_state":"analyzed","attachment_state":"pending",
    "reason":"图片资源缺失","task_ids":[]
  }]
}
```

- `results` 每批1–200项，只引用本批已交付消息及对应内容版本。
- `status` 为 `done/pending/skipped`；`text_state` 为 `analyzed/pending/failed/skipped`；`attachment_state` 为 `analyzed/not_applicable/pending/failed/skipped`。
- `done` 表示 Agent 确认正文及适用媒体已理解，缺少阶段字段时会据此补全；附件准备成功本身不等于 done。工具仍检查正文、附件和版本状态。
- 未完成用 `pending`；跳过用 `skipped` 并提供原因。无附件才用 `not_applicable`。没有待办也可完成消息。
- 已有待办可用 `task_ids` 关联，新建待办从成功操作自动关联。单条消息的已有与候选关联合计最多100个ID（去重）。
- 操作只能关联本次结果中的消息。依赖操作失败时，对应消息保留 pending，成功关联仍保留；检查 `has_failures` 及逐项结果，顶层 `ok` 不表示全部成功。
- 两库不是原子事务。中断后用相同操作ID与参数重放；字段语义变化使用新ID，修订已有事项用 patch。

### stdin 示例

支持 here-document 的 shell：

```sh
python -B -X utf8 agent_cli.py write --input - <<'JSON'
{"batch_id":"B","results":[{"message_key":"K","content_hash":"H","status":"done"}]}
JSON
```

PowerShell：

```powershell
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
@'
{"operations":[{"operation_id":"manual:1","action":"create","fields":{"title":"手动事项"}}]}
'@ | .\.venv\Scripts\python.exe -B -X utf8 agent_cli.py write --input -
```

宿主也可直接传子进程 stdin。

## agent_setup.py：配置与连接

以下命令位于 `agent_setup.py` 后，不是 `agent_cli.py` 子命令：

```text
inspect
init
source candidates --platform qq|wechat|dingtalk [--base ABS_DIR]
source configure --input -
source check --source SOURCE_ID
source connect --source SOURCE_ID --pid PID [--pid PID2] --authorize-memory-read
groups list --source SOURCE_ID [--contains TEXT] [--offset 0] [--limit 50]
groups plan --input -
groups apply --input -
groups remove --input -
images --batch B --message K [--message K2] --pid PID --authorize-memory-read [--timings]
```

配置输入也支持私有 UTF-8 JSON 文件。`inspect` 检查项目状态；`init` 补齐初始化，不覆盖用户规则。`source check` 检查凭据，不查询聊天正文；`connect` 的内存授权仅用于本次调用。

来源配置：

```json
{"expected_revision":0,"confirmed":true,"platform":"qq","name":"我的QQ","paths":{"database_dir":"D:/example/nt_qq/nt_db","media_dir":"D:/example/nt_qq/nt_data"}}
```

微信使用 `database_dir/account_dir`。目录为已确认的绝对路径。文件收件箱使用 `platform:"import",collection:"inbox",name:"本地收件箱"`，不需要 paths。

钉钉实验来源使用 `platform:"dingtalk"`、明确的 `uid` 与 `paths.account_dir`；`source check` 仅验证首页结构，不需要内存扫描。见[钉钉 V3 实验读取](dingtalk.md)。

群配置可用 `groups plan` 预览；参数与授权已确定时直接 `groups apply`：

```json
{
  "expected_revision":1,"confirmed":true,
  "groups":[{
    "source_id":"SOURCE_ID","group_id":"123456789","name":"课程群",
    "since":"2030-10-01T00:00:00+08:00","until":null,"max_days":7,"enabled":true,
    "recipient":"当前Agent/服务",
    "grant":{"text_to_agent":true,"images_to_agent":true,"documents_to_agent":false}
  }]
}
```

可选 `instructions` 保存用户给该群的说明，不生成统一行为模板。`group_key` 使用工具返回值。每次1–100个群，`max_days` 为1–31天；日期结束包含当天，带时间结束为不含该时刻的上界。

修改提供完整群对象及最新 `expected_revision`，保留未改的范围与授权。撤销输入：

```json
{"expected_revision":2,"confirmed":true,"group_keys":["KEY"]}
```

改配置或撤权可使旧批次失效，不删除已写入待办。编辑 `AGENTS.md` 不会修改这些权限。

## 计时

`read/write --timings` 返回组件内部耗时；读取可细分快照复制、认证解密、WAL、SQLite检查、查询、落库与附件准备。Agent推理、看图和工具往返由调用端另测。详见[性能验证](performance.md)。
