# Agent 命令参考（schema_version=1）

使用定位脚本返回的 `argv_prefix`，其后追加下列命令。示例群ID、路径均为占位/虚构，必须用实际授权选择的输出替换。

成功：`{"schema_version":1,"ok":true,"data":{...}}`，退出码0。
失败：`{"schema_version":1,"ok":false,"error":{"code":"...","message":"...","next_actions":[]}}`，退出码2。

结构化写入使用 `--input <私有UTF-8 JSON绝对路径>`，或 `--input -` 通过标准输入；上限1MiB，拒绝静默截断。不要把密钥放入任何输入。

## 安装/来源

```text
inspect
setup
source candidates --platform qq|wechat [--base <明确绝对目录>]
source configure --input <请求文件>
source check --source <来源ID>
source connect --source <来源ID> --pid <本人客户端PID> --authorize-memory-read
```

`inspect` 只检查本项目状态，不验证密钥、不读客户端。对QQ/微信，`source check` 只验证数据库首页凭据，不查询正文，不必每轮运行。`source connect` 的授权只用于此次命令，可以重复 `--pid`；不得从持久配置自动生成内存授权。

来源配置输入：

```json
{
  "expected_revision": 0,
  "confirmed": true,
  "platform": "qq",
  "name": "我的QQ",
  "paths": {
    "database_dir": "D:/example/nt_qq/nt_db",
    "media_dir": "D:/example/nt_qq/nt_data"
  }
}
```

微信的 `paths` 为 `database_dir` 和 `account_dir`。目录必须存在且为绝对路径。`expected_revision` 使用最近的inspect/写入返回值。账号ID由平台和规范化数据库目录派生；换账号目录产生新来源，不继承原群授权。新入口不覆盖旧 `config.json` 的客户端路径；单独保存每个来源，兼容原手工入口。媒体访问不继承旧的 `extra_allowed_roots`，首版只访问该来源媒体目录。

文件收件箱来源配置使用`platform:"import"`、可选`collection:"inbox"`和name，不需要客户端paths、账号或取钥。其check只统计本地已导入条数，groups只列已导入分组元数据，首次可自行命名inbox分组；详见[通用导入](importing.md)。

## 群配置

```text
groups list --source <来源ID> [--contains <字面片段>] [--offset 0] [--limit 50]
groups plan --input <请求文件>
groups apply --input <同一请求文件>
groups remove --input <撤销请求文件>
```

`list` 查询元数据，按返回的 `next_offset` 继续，不查询正文。`plan` 不写配置、不读正文；`apply` 必须在用户确认后执行。

```json
{
  "expected_revision": 1,
  "confirmed": true,
  "groups": [{
    "source_id": "从source configure输出复制",
    "group_id": "123456789",
    "name": "示例课程群",
    "since": "2030-10-01T00:00:00+08:00",
    "until": null,
    "max_days": 7,
    "enabled": true,
    "instructions": "图片优先，结合相邻文字整理作业、截止时间和提交方式。",
    "recipient": "用户确认的当前Agent/服务",
    "grant": {
      "text_to_agent": true,
      "images_to_agent": true,
      "documents_to_agent": false
    }
  }]
}
```

每次1–100个群；`group_key` 由工具生成，不手算。`since` 必填且需用户确认。可选 `until` 为授权结束边界，不填则允许后续按需读取；日期结束包含当天，带时间的结束上界不包含该时刻。单次跨度 `max_days` 为1–31天，默认7天。未列出的群拒绝访问。

修改时提供完整群对象（准确保留来源、时间范围、交付授权等），以免意外改变已有授权。停用可设置 `enabled:false`；彻底撤销：

```json
{"expected_revision":2,"confirmed":true,"group_keys":["从输出复制准确key"]}
```

改规则或撤权后旧批次不可继续访问，已写入的用户待办不会被删除。

## 通用文件导入

```text
import --group <已授权group_key> --file <绝对路径> --confirm [--format auto] [--timestamp <ISO时间>] [--limit 50] [--timings]
```

仅适用于声明file_import能力的来源，不能借QQ/微信授权导入任意文件。不要求原始来源、发言者或时间；支持JSON、UTF-8文本、图片和文档，规范及上限见[导入格式](importing.md)。import直接返回现有处理批次，不必再read。日常read从导入队列取未完成内容，不补扫多年空历史；显式--since才重新扫描收件箱。

## 增量读取/分页

```text
read [--group <group_key> ...] [--until now] [--limit 50] [--timings]
read --batch <批次ID> --cursor <next_cursor> [--limit 50]
read --batch <批次ID> --message <message_key> --offset <字符偏移> [--chars 8000]
read --group <key> --since <起点> --until <终点> [--reprocess]
read --group <key> --since <起点> --until <终点> --from-export <项目exports中的已有批次>
```

- 默认按当前授权群读取，一次调用覆盖多个平台/来源，每个来源共用数据库快照。不自动取钥，不处理附件，不调用模型。
- 不接受模糊群名或未配置的群；范围不能越过授权起点/终点。较长历史按 `max_days` 分段，并返回 `more_history` 和 `read_windows`。
- 新调用固定本次 `now`。默认以已接收位置为基础重叠24小时（单次跨度很小时缩短，以确保向前推进），按身份＋内容哈希去重。
- 返回消息的 `message_key`、`content_hash`、群规则、当前阶段、已有 `task_ids`。已完成且内容不变的历史不再作为待处理消息返回。`context` 最多附带各页相关群前24小时内附近两条已处理消息，受同一授权和大小预算约束；它们是只读上下文，不提交进度、不重新建事项，大内容同样可分块。不扩大范围拉取私聊。
- `next_cursor` 不为null时继续同一批次。分页不会重新解密。数量limit为1–200，实际页可能因约32KiB内容预算更小，须以续读标识为准。
- 超大单条提供 `content_ref`，正文用分块返回，直到 `next_offset:null`。分块是原始消息JSON的连续片段，合并后理解，不能当作缺失正文。
- `--reprocess` 只在用户要求重分析时使用，重置本次实际读到消息的分析完成标记，不清除待办关联；仍用已有待办/幂等操作避免重复创建。
- `--from-export` 是显式历史路径，只能选择同一来源账号的群；校验原范围、快照数据库来源及每条消息。无法验证、范围不足或文件缺失直接失败，不回退读客户端；不推进客户端获取游标。输出明确标记历史快照。
- `--timings` 返回每来源读取、快照/解密、查询、正文解码、去重落库的耗时，以及快照数、行数及总耗时。`attachments prepare`、`todos apply`、`processing commit` 也支持 `--timings`，分别报告资源准备/缓存命中、待办写入和记账耗时。默认不输出诊断。不承诺固定秒数，数据库仍须安全快照、认证和WAL校验。

## 附件

```text
attachments prepare --input <请求文件>
attachments prepare --input <请求文件> --pid <本人微信PID> --authorize-memory-read
```

```json
{"batch_id":"批次ID","message_keys":["本页返回的message_key"]}
```

每批1–200个已返回的消息ID；各消息返回 `prepared/pending/scope_denied/unsupported_or_no_attachment` 以及本地资源路径。只访问选定消息引用的本地资源，不下载、不执行附件，不改变原始客户端文件。

本地缓存可复用，不重复转换未变化图片；文件元数据变化/丢失、授权变化、转换器版本变化使缓存失效。缓存不是对同用户恶意程序的安全隔离。`prepared` 不是OCR完成，也不是AI已分析。

图片权限与文档权限分别检查。混合附件/未知类型/ZIP可能同时需要图片和文档交付授权，不自动扩大。图片密钥缺少时默认保留pending；另行说明、确认后才带PID和单次内存授权重试。

## 待办（独立于聊天）

```text
todos list [--offset 0] [--limit 50] [--all]
todos get --id <待办ID>
todos apply --input <请求文件>
```

```json
{
  "batch_id": "批次ID",
  "operations": [{
    "operation_id": "message-key:task:0",
    "action": "create",
    "message_keys": ["message-key"],
    "fields": {
      "title": "提交练习",
      "due": "2030-10-13",
      "due_label": "10/13 上课前",
      "note": "题目和提交要求；必要原文及已知说明直接写正文",
      "optional": false
    }
  }]
}
```

`operations` 每批1–200项。新增可省略 `id`，由operation_id派生稳定ID。返回逐项 `ok/id/replayed` 或错误；**顶层ok只代表接口成功，必须检查每项结果。**相同operation_id且内容完全相同为重放，不再次修改；同ID用于不同内容拒绝。失败项不回滚其他成功项。

冲突返回 `current_id` 和查询建议，不附带整条大正文；按需 `todos get --id ...` 获取当前值再决定。

修改示例：

```json
{"operation_id":"notice-revision:patch:0","action":"patch","id":"已有待办ID","message_keys":["message-key"],"fields":{"due":"2030-10-15"},"baseline":{"due":"2030-10-13"}}
```

source是保留的兼容可选字段，不要求填写；无来源信息也可新增，有说明直接写note正文即可。可改 `title/due/due_label/note/source/optional`，只传实际要改的字段及对应baseline。日期使用YYYY-MM-DD或YYYY-MM-DD HH:MM，可为null；时间含义由Agent结合消息解释。

显式完成/撤回：`action:"mark",value:true/false,baseline:{"done":0/1}`。
显式移除/恢复：`action:"remove",value:true/false,baseline:{"removed":0/1}`。

普通create/patch不重置完成或移除状态；create不覆盖已有ID（包括已移除项），patch不修改已移除项。状态动作由Agent根据用户意图决定，不自动触发。纯待办管理可不传batch_id，但也不得伪造message_keys。

## 处理记账

```text
processing pending [--group <key> ...]
processing commit --input <请求文件>
```

```json
{
  "batch_id":"批次ID",
  "results":[{
    "message_key":"从本批次输出复制",
    "content_hash":"从本批次输出复制",
    "status":"done",
    "text_state":"analyzed",
    "attachment_state":"not_applicable",
    "reason":"",
    "task_ids":["成功写入的待办ID"]
  }]
}
```

- `status`: `done/pending/skipped`。
- `text_state`: `analyzed/pending/failed/skipped`。
- `attachment_state`: `analyzed/not_applicable/pending/failed/skipped`。
- 无事项时 `task_ids:[]`，仍可done。有媒体不能用not_applicable；未成功prepare的媒体不能标记analyzed。任一skip须非空reason，失败不可伪装为skip。
- unresolved用pending；已完成正文但附件缺失时如实保留附件pending。用户明确允许不处理时才能记录跳过原因。
- 一条消息可关联多待办，多条消息可关联同一待办；关联ID须真实存在。commit前先检查todos apply每项结果。
- 每次1–200项，批量登记。获取与分析分离：读到消息时先持久接收，只有done或显式skipped才不再返回；中断、失败、未知类型不会被游标跳过。
- 待办成功后commit前崩溃，重用原operation_id重试，再提交结果。规则变更、消息版本变化或撤权会拒绝旧批次继续处理，不能直接读原文件绕过。
