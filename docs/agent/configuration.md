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
- 授权起点、可选终点、单次跨度 `max_days`（默认7天）。首次默认读近期窗口，更早历史可显式回补。
- 接收 Agent/服务，以及正文、图片、文档分别交付的权限。在线 Agent 会接收选定内容。
- 可选的分群 `instructions`，只保存用户提供的处理说明。

参数和授权已确定时可直接 `apply`；`plan` 仅用于需要预览差异的情况，不是必经步骤。已明确的同一范围不重复询问。JSON schema、版本号和撤销请求见[命令参考](commands.md)。来源权限保存在配置中，手写 `AGENTS.md` 不会改变这些权限。

## 4. 日常与规则编辑

已有授权后直接 `agent_cli.py read`，理解内容后 `write --input -`；不需要反复检查或列群。

UI 顶部“文档”打开根 `AGENTS.md` 的简单文本窗口，Ctrl+S 保存，初始0字，不自动补充规则。宿主是否加载、何时重新加载由当前 Agent 决定；保存不代表所有模型立即采用。

## 私有数据与迁移

| 位置 | 用途 |
| --- | --- |
| `config.json` | 本地组件配置 |
| `state/agent-config.json` | 来源、群授权与分群说明 |
| `state/secrets.dpapi` | 当前 Windows 用户绑定的凭据 |
| `state/imports/` | 导入消息与选定附件副本 |
| `state/processing.sqlite3` | 消息明文、批次、处理阶段、关联及缓存索引 |
| `state/todos.sqlite3` | 待办、完成/移除状态、偏好与幂等记录 |
| `exports/agent-assets/` | 准备给 Agent 的附件 |
| `state/component-location.json` | 可选的项目与 Python 位置记录 |

消息和待办库不由 DPAPI 加密。撤权阻止后续工具访问，不收回 Agent 已接收内容，也不自动删除待办。

升级前关闭窗口，备份并保留 `config.json/state/exports` 和自己的 `AGENTS.md`，再更新源码；不运行清理来升级。
移动目录后重新定位；DPAPI凭据不能迁移到其他 Windows 用户。更改已授权来源路径时，先处理相关群授权，再重新配置。
