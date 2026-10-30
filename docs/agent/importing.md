# 文件导入与适配器

将用户选定的 JSON、UTF-8文本、图片或文档接入 read/write，复用分页、去重、附件和待办关联。不需要客户端、真实账号、发言者或原始来源；已知说明可直接写待办正文。

## 配置收件箱

通过 `agent_setup.py inspect` 获取版本，再用 `source configure --input -`：

```json
{"expected_revision":0,"confirmed":true,"platform":"import","collection":"inbox","name":"本地收件箱"}
```

`collection` 默认 inbox，是本地命名空间，不代表真实平台群。不同 collection 相互隔离。

再通过 `groups plan/apply --input -` 配置分组；版本号使用当前返回值：

```json
{
  "expected_revision":1,"confirmed":true,
  "groups":[{
    "source_id":"SOURCE_ID","group_id":"inbox","name":"待整理素材"
  }]
}
```

本地分组可自行命名，无需先从客户端发现群。已选分组允许全部媒体，不另配时间或交付权限。

## 导入与处理

在 `agent_cli.py` 后调用：

```text
read --file ABS_FILE --group KEY --confirm [--format auto] [--timestamp ISO_TIME]
```

可加 `--limit/--timings/--attachments`。每次只选一个已授权文件收件箱分组。

| 输入 | 处理方式 |
| --- | --- |
| JSON | 下述规范化消息 |
| TXT / MD | 整个 UTF-8 文件作为一条正文，换行统一为 LF |
| PNG / JPEG / GIF / WebP / BMP | 按签名检查并复制为图片附件，无需 OCR |
| PDF、DOC / DOCX、XLSX、TXT / MD / CSV、HTML、ZIP | 作为文档附件；文本可用 `--format document` 明确选择 |

返回首个页面、批次和 `created/updated/unchanged` 统计。默认只准备本页已授权纯图片；`--attachments` 准备所有授权媒体，包括文档。准备不等于理解，不执行脚本或宏。

后续用 `read --batch B --cursor C` 分页，由当前 Agent 理解后 `write --input -`。单条文档可用 `read --batch B --message K --attachments` 单独准备。
日常 read 消费已登记的未完成队列，不重复解析原文件，也不补扫多年空历史；显式 `--since/--until` 才扫描已导入收件箱。

## JSON 格式

```json
{
  "schema_version":1,
  "messages":[
    {"text":"没有来源或原始时间的通知，也可以整理。"},
    {"id":"notice:2","text":"通知见图片","attachments":["images/notice.png"]}
  ]
}
```

也接受消息数组或单个消息对象。每条只支持 `id/timestamp/text/attachments`，至少有正文或附件。

- `id` 可省略，按规范化内容生成稳定ID；重复导入不重复分析。不同通知若内容相同，需要不同ID或明确原时间才能区分。显式同ID新内容视为修订。
- `timestamp` 可省略，首次导入时刻会被保留供重试；`timestamp_basis:imported` 表示导入时间，不是发送时间。无法据此解释“明天”等相对日期。明确提供的时间标为 `provided`，接受整数秒或 ISO 时间，文件字段优先于 `--timestamp`。
- `attachments` 为相对路径字符串，或 `{"path":"images/notice.png","kind":"image","sha256":"完整哈希"}`。kind 为 image/document，需匹配实际类型；sha256可省略。
- 附件只访问所选 JSON 目录内明确引用的文件，不接受 `..`、绝对路径、URL、符号链接或目录联接。图片/文档交付分别检查。
- 文件缺失、非法类型、重复消息ID或越界等明确失败，不静默丢弃记录。

上限：JSON 32MiB、纯文本4MiB、每批10000条、单条50个附件、每批200个附件、单附件32MiB、总附件128MiB。不限制历史跨度；消息时间须非负且不晚于导入时刻。

## 存储与恢复

`state/imports/<命名空间哈希>/messages.sqlite3` 保存规范化消息，assets 保存附件副本；处理库另记批次与阶段。原文件不修改，成功导入后无需长期保留外部原文件供后续读取。

两库不是原子事务，中断时用同一文件和稳定ID重试；也可显式按时间扫描已导入内容。导入不推进客户端获取游标。
失败复制可能留下未引用的私有资源，随 state 备份和清理，不用 cleanup 作为重试手段。

## JSONL 适配器范例

`examples/adapters/jsonl_adapter.py` 提供 `ExampleJSONLSource`，把下面的字段映射到通用导入：

```json
{"event_id":"synthetic:1","sent_at":"2020-01-02T09:00:00+08:00","body":"合成通知","files":[]}
```

`event_id/sent_at/files` 可省略；样例 `messages.example.jsonl` 只有合成数据。

```text
python -B -X utf8 examples/adapters/jsonl_adapter.py --help
python -B -X utf8 examples/adapters/jsonl_adapter.py read --group KEY --file ABS_JSONL --format jsonl --confirm
```

这个启动器注册 `example_jsonl` 后转发到 `agent_cli.main`，因此用于 read/write，不提供配置子命令。试用时，在项目根目录用下面的启动方式给 setup 注册同一工厂：

```text
python -B -X utf8 -c "from examples.adapters.jsonl_adapter import register; register(); from agent_setup import main; raise SystemExit(main())" inspect
```

将末尾 `inspect` 换成 `source configure --input -`、`groups plan --input -` 或 `groups apply --input -`，按上文配置流程保存 `platform:"example_jsonl"` 的来源与分组；其他字段相同。配置完成后再运行上面的示例 read。

普通 setup/CLI 不会自动发现此示例。正式接入可将工厂显式加入 `agent_sources.py` 的注册表。
开发另一种格式时可复用该类的 `platform/formats/load_rows` 结构；完整来源接口见[扩展指引](extending.md)。
