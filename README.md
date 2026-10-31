# ChatNoticeTool

把群通知和文件，整理成桌面待办。

工具负责读取与保存，你正在使用的 Agent 负责理解与判断。
支持 QQ / 微信本地消息、文件导入，以及[钉钉 V3 实验来源](docs/agent/dingtalk.md)；待办窗口也可独立使用。

## 开始使用

Windows，推荐 Python 3.13，桌面窗口需要 Tkinter。

```powershell
git clone https://github.com/Jinshi-Liu/ChatNoticeTool.git
cd ChatNoticeTool
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -B -X utf8 local_setup.py
.\.venv\Scripts\python.exe -B -X utf8 todo_widget.py
```

以后双击 `todo-widget.cmd` 即可打开。只用待办时，可以跳过依赖安装。

## 桌面待办

- **清空已完成**：切到“完成”页，点击列表上方的按钮并确认，一次移除本次选定的已完成事项；空列表时按钮禁用。未完成事项、确认期间新完成的事项，以及已经恢复为待办的事项不受影响。有未保存的详情修改时先处理草稿，取消关闭则不批量移除。与单项移除一样，助手更新同一ID不会自动恢复已移除事项。

## 交给 Agent

将 [`skills/chatnotice`](skills/chatnotice) 文件夹复制到所用 Agent 支持的 skills 目录，再告知 ChatNoticeTool 项目路径。

告诉 Agent 工具所在目录和你的需求：

> 工具在 D:/ChatNoticeTool。帮我连接课程群，把新通知整理成待办。

首次连接见[配置指南](docs/agent/configuration.md)。配置好后，日常只有一条链路：

```text
read → 理解内容 / 看图 → write
```

```powershell
python -B -X utf8 agent_cli.py read
python -B -X utf8 agent_cli.py write --input -
```

`write` 从标准输入接收待办和处理结果；已有授权不需要反复检查、列群或确认。
完整参数与 JSON 示例见[读写接口](docs/agent/commands.md)。

## 管理规则与群聊

点击 **pin 左侧的文档图标**，在「规则」「群聊」之间切换。规则页以更大字号编辑 `AGENTS.md`（默认空白），保存图标或 **Ctrl+S** 仅保存规则。
群聊页是一张连续只读表，QQ / 微信 / 其他平台仅作行内标识，显示群名、群号及“最后读取”（MM-DD HH:MM，无记录为“尚未读取”，悬停可看完整时间和长 ID）。所有行与空白共用一个纵向滚动区域，底部“修改此配置请与 Agent 沟通”固定不滚走；每5秒仅刷新可见群页的本地元数据，保留位置与选择。新增、修改及移除群由 Agent 通过正式配置接口完成；已选启用群允许全部媒体，内存取钥仍须单次明确授权和 PID。
切换页签保留规则草稿。规则是否加载、何时重新加载由当前 Agent 宿主决定。旧配置迁移见[配置指南](docs/agent/configuration.md)。

更新源码时，保留自己的 `AGENTS.md`、`config.json`、`state/` 和 `exports/`。

## 贡献者

感谢所有反馈与贡献！

- [@ouzefeng113](https://github.com/ouzefeng113)：贡献“清空已完成”功能、测试和使用说明（[#2](https://github.com/Jinshi-Liu/ChatNoticeTool/issues/2)）。

---

[文件导入](docs/agent/importing.md) · [项目结构](ARCHITECTURE.md) · [贡献与测试](CONTRIBUTING.md) · [变更记录](CHANGELOG.md)

[Apache License 2.0](LICENSE) · [第三方说明](NOTICE.md)
