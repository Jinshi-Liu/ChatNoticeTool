# 轻量工作流

日常只用两个基础入口：**read读取结构化数据，write批量写入数据。**不新增编排器、审批步骤或模型调用；业务判断、日期解释和看图由当前Agent完成。

## 一次定位，之后不重复配置

项目目录由AGENTS.md确定。首次运行agent_locator.py并使用返回的argv_prefix；只有未配置或用户要求修改时，才读[配置指南](configuration.md)。已有授权直接read，不先inspect/doctor/check/列群/取钥，不遍历私有目录。其他目录调用须明确组件位置，不全盘搜索。

## 常规单页：read → Agent理解/看图 → write

1. **read**默认批量返回未完成/变化消息、精简内容和本页已授权图片的本地路径。附件只准备本页，复用有效缓存；不会读取客户端内存、下载或处理所有历史附件。正文、图片、文档权限独立，未授权图片不会准备。
2. Agent读返回内容，按需要使用自己的工具批量看图，结合已有待办自行判断。prepared只代表文件可读，不代表理解成功。文档/混合附件不是默认图片流程，需要时才用兼容附件准备入口；未知内容保留pending。
3. **write --input -**通过标准输入一次传operations和results。不先另开工具调用写请求JSON文件。工具内检查批次/版本/分析状态，再批量写待办、逐项检查并批量记账；失败操作对应的消息留pending，其他消息仍可完成。
4. next_cursor非空时read --batch ... --cursor ...继续同一批次，默认只准备这一页图片，不重新解密。只有more_history为真才读下一历史窗口。最后简短报告变化和待确认项。

首次没有获取游标时，默认只取授权范围内最近max_days窗口；返回history_notice时，明确更早历史尚未扫描。全部历史授权不等于要从授权起点自动逐周追赶；用户指定历史时用显式--since/--until。后续有游标按增量重叠读取，已有历史追赶不被改成近期窗口。

无待办变化时，write只传results；零消息不调用write。独立管理待办用read --todos（可重复--id准确选取）和仅含operations的write，不需要聊天批次。字段更新带baseline，普通更新不能复活完成/移除项。

## 最小写入

```json
{
  "batch_id":"read返回的批次",
  "operations":[{
    "operation_id":"稳定的逻辑操作ID",
    "message_keys":["read返回的消息key"],
    "action":"create",
    "fields":{"title":"事项标题","note":"要求和已知说明直接写正文"}
  }],
  "results":[{
    "message_key":"read返回的消息key",
    "content_hash":"read返回的内容版本",
    "status":"done"
  }]
}
```

status:done是**Agent明确确认正文及适用媒体都已理解**，不是工具替Agent判断。工具仍拒绝未准备图片、未知正文的虚假成功。未完成用status:pending并写reason；只有用户明确允许不处理时才用skipped并说明原因。需要细分阶段时可保留text_state/attachment_state。新待办ID由成功operations自动关联，不必第二次查询再提交。

重试使用同一operation_id及完全相同操作参数。两个库不是原子事务，写待办后中断也可幂等重放；失败项不得通过漏填operation_ids伪装成功，依赖由工具从message_keys推导。成功部分的待办关联会保留，避免下一轮重复建任务。

## 精简内容与完整原文

- 微信图片默认不返回CDN/XML传输属性，返回类型、时间、图片引用及必要元数据。引用卡片返回完整当前标题和被引用消息标识，不重复塞入整段引用/XML。
- raw_ref是**可选**原文入口，有必要时read --batch ... --message ... --raw；不是每条都要另读一遍。原文为已有脱敏完整消息，不是未经保护的数据库行。
- content_ref表示当前视图过大，必须按分块读全；默认分块仍是精简视图，不静默截断正文。
- 纯文字引用没有附件，不制造永久pending；图片引用只关联当前授权同群已接收图片或引用自身包含的图片资源，找不到/不支持仍pending。不为引用扩大群、时间或客户端查询范围。
- context是已处理的只读上下文，不重新建任务、不提交进度。只读返回路径，不通过原文件绕过撤权/过期批次。

截止时间基于原时间与已知上下文，不按运行当天猜年份。timestamp_basis:imported仅代表导入时刻，不能当作发送时间解释“明天”；不确定就询问或待确认。有来源说明直接写note，无来源不强制填source。

## 例外才查详细工具

用户文件使用read --file <绝对路径> --group <已授权收件箱key> --confirm，同样返回消息/本页图片并接write；不虚构来源，不用客户端取钥。首次配置、扩展来源、诊断、内存取钥和破坏性清理，按需读[导入](importing.md)、[配置](configuration.md)、[扩展](extending.md)及[故障处理](troubleshooting.md)。旧分步命令只作兼容，不作为常规调用链。

可选--timings区分快照/解密、SQL查询、正文处理、附件定位/转换和写入记账；快照进一步拆分复制校验、主库解密、WAL和SQLite检查，微信图片报告直接命中及每群回退遍历次数。可复现的纯合成基准与性能边界见[性能验证](performance.md)。Agent看图/推理和工具往返耗时由调用端测量，不算在组件内部计时中。不为提速取消认证、WAL或完整性校验。

备份/升级保留config.json、state及必要exports；不运行cleanup来升级。清理先预览并另行确认，不强杀客户端或待办窗口。只在用户要求时打开桌面窗口。
