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

## 交给 Agent

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

## 写自己的规则

点击主窗口 **pin 左侧的文档图标**，直接编辑 `AGENTS.md`，按 **Ctrl+S** 保存。
规则默认空白，按你的习惯填写。新增待办、完成、编辑和置顶都在窗口内操作。

更新源码时，保留自己的 `AGENTS.md`、`config.json`、`state/` 和 `exports/`。

---

[文件导入](docs/agent/importing.md) · [项目结构](ARCHITECTURE.md) · [贡献与测试](CONTRIBUTING.md) · [变更记录](CHANGELOG.md)

[Apache License 2.0](LICENSE) · [第三方说明](NOTICE.md)
