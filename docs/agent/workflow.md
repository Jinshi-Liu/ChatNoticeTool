# 处理流程

```text
read → 当前Agent理解正文 / 看图 / 读文档 → write --input -
```

工具负责授权、读取、持久化和去重，不调用模型。业务判断由当前 Agent 完成，不从空白 `AGENTS.md` 自动生成规则。

## 定位与读取

项目路径由调用方提供；`agent_locator.py` 返回项目 Python 和 `agent_cli.py` 的 `argv_prefix`。首次配置见[配置指南](configuration.md)，已有授权可直接读取。

```text
read
read --group KEY
read --batch B --cursor C
```

默认返回新消息、变化消息和未完成项，附本页已授权纯图片路径。分页继续同一批次，不重新解密。首次无游标默认取最近 `max_days` 窗口；更早历史用 `--since/--until`，近期读取不代表全部历史已读。

`content_ref` 是大正文分块入口，按 `next_offset` 读全。`raw_ref` 用于按需读取完整脱敏原文；默认精简视图不重复交付微信传输 XML。`context` 是已处理消息的只读上下文，不重复记账。

## 图片与文档

`prepared` 只表示资源可用。需要文档或其他媒体时：

```text
read --batch B --message K --attachments
```

此调用只返回该已交付消息的附件准备报告；不加 `--attachments` 则读正文分块。也可在页面读取时加 `--attachments` 准备本页所有已授权媒体。

缺图片密钥时，配置入口支持单次授权取钥：

```text
python -B -X utf8 agent_setup.py images --batch B --message K --pid PID --authorize-memory-read
```

工具不下载附件或执行内容。缺失、未知或无法理解的媒体仍是未完成，不由准备动作自动判定成功。

## 写入待办与结果

```json
{
  "batch_id":"B",
  "operations":[{
    "operation_id":"notice:task:1","action":"create","message_keys":["K"],
    "fields":{"title":"事项标题","note":"要求和已知说明"}
  }],
  "results":[{"message_key":"K","content_hash":"H","status":"done"}]
}
```

通过 `write --input -` 的标准输入一次提交。无待办变化只传结果；没有消息不需要写结果。独立待办用 `read --todos/--id`，write只传 `operations`。

- `done` 表示 Agent 确认正文及适用媒体已理解，不是工具自动判断；未完成用 `pending`，跳过用 `skipped` 并说明原因。
- 新待办自动关联成功操作的消息，已有待办可用 `task_ids` 关联。检查逐项结果，操作失败的依赖消息保留 pending。
- 重试复用相同 `operation_id` 和操作参数。待办库与处理库不是原子事务，幂等重放用于恢复中断。
- 更新字段用 `baseline`，普通更新保留用户编辑、完成和移除状态。

没有原始消息时间时，`timestamp_basis:imported` 只代表导入时刻，不能据此推断“明天”的日期。已知说明可直接写待办正文，无需补造来源字段。

## 其他入口

用户文件：`read --file ABS_FILE --group KEY --confirm`，同样返回处理页面，再接 write。格式见[导入](importing.md)。

配置、连接与授权由 `agent_setup.py` 管理；日常 CLI 只有 read/write。完整参数和 stdin 示例见[命令参考](commands.md)，诊断见[故障处理](troubleshooting.md)，计时见[性能验证](performance.md)。
