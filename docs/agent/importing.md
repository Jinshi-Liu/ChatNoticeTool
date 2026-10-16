# 通用导入与适配器范例

导入用户明确提供的JSON、UTF-8文本、图片或文档，复用现有分析、去重、附件、分页和待办接口。不连接客户端，不取钥，不下载远程附件，不额外接入模型；图片仍由当前Agent看。

**不要求真实来源、账号、发言者或原始时间。**已知来源说明直接写在待办`note`正文即可，不必填`source`字段，也不新增来源追溯功能。工具里的collection、group_id、message_key仅是权限和去重的技术命名空间，不能据此声称知道原始群。

## 1. 配置本地收件箱

先inspect获取当前revision，再通过source configure保存：

```json
{"expected_revision":0,"confirmed":true,"platform":"import","collection":"inbox","name":"本地收件箱"}
```

collection默认inbox，不需要客户端paths。不同collection相互隔离，不会因相同消息ID串号。以下数字、时间和key仅示例，使用工具真实返回值和用户确认的时间范围，不照抄：

```json
{
  "expected_revision":1,
  "confirmed":true,
  "groups":[{
    "source_id":"source configure返回的ID",
    "group_id":"inbox",
    "name":"待整理素材",
    "since":"2020-01-01T00:00:00+08:00",
    "max_days":7,
    "recipient":"用户确认的当前Agent/服务",
    "grant":{"text_to_agent":true,"images_to_agent":true,"documents_to_agent":true},
    "instructions":"自行整理；有来源说明直接写进待办正文。"
  }]
}
```

仍先groups plan展示差异，再经用户确认groups apply。首次起点通常建议最近三天；处理旧文件时单独确认相应历史范围。分组可自行命名，不必先groups list发现真实群，inbox不是聊天平台群号。

## 2. 导入明确选择的文件

在定位脚本返回的argv_prefix后调用：

```text
read --group <已授权group_key> --file <绝对文件路径> --confirm
```

可选`--format auto|json|text|image|document`、`--timestamp <缺省消息ISO时间>`、`--limit 50`、`--timings`。其他格式由对应适配器声明。不加confirm不会读取文件正文；`--group`只选择一个已授权的本地分组，多条消息可一次导入。

- `.json`：规范化消息数组，或下述包含messages的对象。
- `.txt/.md`：整个UTF-8文件作为一条消息，不猜测发言者和时间分隔，不按行丢弃内容；换行统一为LF。
- PNG/JPEG/GIF/WebP/BMP：作为图片附件，按文件签名检查并复制到私有收件箱，不需Pillow或OCR。
- PDF、DOC/DOCX、XLSX、TXT/MD/CSV、HTML、ZIP：可作为文档附件复制，供当前Agent按其能力读取。prepare不等于已分析；不执行脚本/宏，不自动展开ZIP。文本若需作为附件可用`--format document`或JSON attachments。

日常仍只用read/write。文件read返回第一页面、batch_id、created/updated/unchanged统计、精简消息和本页已授权图片，后续直接走：

```text
Agent理解/看图 → write --input -（待办操作和处理结果一次提交）
```

旧import命令仅保留兼容（可用--prepare-images），不加入日常链路。不必再read一次获取刚导入的内容；继续分页用`read --batch ... --cursor ...`。日常read直接消费导入后的待处理队列，不按客户端模式补扫多年空历史，不重复解析原文件。用户显式要求重新扫描收件箱时可用`read --since ... --until ...`，仍按max_days分段。

## 3. 最小JSON格式

```json
{
  "schema_version":1,
  "messages":[
    {"text":"没有原始来源或时间的通知，也可以整理。"},
    {"id":"notice:2","text":"通知见图片","attachments":["images/notice.png"]}
  ]
}
```

每条只支持`id`、`timestamp`、`text`、`attachments`；必须有正文或附件。可直接传消息数组，或单个消息对象。其他软件的任意JSON不能保证直接识别，按下述范例做格式映射。

- id可省略，省略时根据规范化内容生成稳定ID；同内容重复导入不重复分析。相同文字的两次真实通知需要不同ID或明确原时间，不能仅凭内容区分。
- timestamp可省略，省略时使用首次导入时间，重试沿用首次时间。返回timestamp_basis为imported（仅导入时刻）或provided（明确提供的时间）。它不是推断出的原始发送时间；Agent不能用它冒充原时间解释“明天”，无法确定时待确认。可提供整数秒或ISO时间，文件内字段优先于命令缺省时间。
- 显式同ID新内容视作修订，交给Agent自行决定怎样修改待办，不自动复活完成/移除项。
- attachments可为相对路径字符串，或`{"path":"images/notice.png","kind":"image","sha256":"可选完整哈希"}`；kind为image/document，必须匹配实际类型。
- 附件只访问所选JSON文件目录内明确引用的文件；不扫描文件夹，不接受`..`、绝对路径、URL、符号链接或目录联接。图片/文档交付分别检查，未知封装或ZIP可能需同时授权。
- 文件缺失、编码错误、非法类型、权限不足、消息ID重复或范围不足均明确失败，不静默跳过部分记录。

上限：单文件JSON32MiB、纯文本4MiB、每批10000条、单条50个附件、每批200个附件、单附件32MiB、总附件128MiB；消息时间跨度须在分组max_days内，不能早于授权起点、达到授权终点或晚于导入时刻。超过上限分批处理，不截断。

## 4. 私有存储与中断重试

`state/imports/<命名空间哈希>/messages.sqlite3`存规范化消息，assets子目录存按SHA256命名的选定附件；`state/processing.sqlite3`仍记录分析状态。原文件不会被修改。成功导入后可删除外部原文件，已复制资源仍可按授权使用。

导入库与处理库不做跨库原子事务。若导入进程在两步之间中断，用同一文件和稳定ID重试，内容相同不会重复建消息；必要时显式按时间扫描已导入收件箱。导入不推进客户端获取游标，也不冒充已同步最新聊天。

失败的附件复制或进程中断可能留下未被消息引用的私有资源；不在失败时自动运行cleanup（它会删除待办/密钥）。这些文件仍属于本项目state备份/清理范围，不可提交或分享。

## 5. 可运行JSONL适配器

`examples/adapters/jsonl_adapter.py`将另一种字段结构转换到同一流程：

```json
{"event_id":"synthetic:1","sent_at":"2020-01-02T09:00:00+08:00","body":"合成通知","files":[]}
```

event_id、sent_at、files都可省略；它只是本地格式范例，不是某聊天平台的官方接口。样例文件`examples/adapters/messages.example.jsonl`仅含合成数据，日期是历史示例，试用时需确认对应范围。

试用入口：

```text
python -B -X utf8 examples/adapters/jsonl_adapter.py --help
python -B -X utf8 examples/adapters/jsonl_adapter.py inspect
```

通过此入口配置`platform:"example_jsonl"`，其余collection/分组流程相同，再用同一入口`read --group ... --file ... --format jsonl --confirm`。**试用阶段所有命令都使用此入口**，普通agent_cli未注册example_jsonl；正式集成需明确将工厂加入agent_sources的注册表。

开发时复制该类，修改platform、formats和load_rows映射，继承通用导入的范围检查、附件限制、私有存储、去重与批次流程。不是另起一套直接写待办/SQLite的脚本。客户端/API接入的更完整接口见[扩展指引](extending.md)。
