# ChatNoticeTool

**Windows 本地群消息按需导出 + 可独立使用的桌面待办组件。**

On-demand local QQ / WeChat group-message export and a standalone desktop todo widget. No automatic collection, message sending, or built-in cloud AI.

> 实验性工具，不是腾讯官方接口。仅用于本人账号、本人有权处理的本地记录。只读内存扫描必须得到账号使用者的明确授权；不承诺支持所有客户端版本，不建议直接在唯一的数据副本上试验。

## 能做什么

- 按准确群ID和明确时间范围导出本机已有的 QQ / 微信消息，保留未知类型和解析缺口。
- 从已登录客户端进行有界、只读的密钥扫描；验证后用 Windows DPAPI 缓存，绝不打印明文密钥。
- 在临时副本上解密，校验数据库页面、WAL提交边界和 SQLite 一致性；正常关闭后删除临时数据库。
- 处理部分本地图片、PDF、DOC / DOCX、XLSX、文本和 ZIP；不自动下载附件、不执行文档中的脚本或宏。
- 独立待办组件：新增、直接编辑、完成 / 撤回、移除、置顶、多个详情窗口、自动保存、进度持久化。
- 外部脚本通过稳定ID新增或更新待办，保留已有完成 / 移除状态。

**尚未提供：** 自动AI提取待办、云端历史拉取、实时监控、自动发送、完整语音 / 视频 / 转发解析、通用一键安装器。导出消息不会自动生成待办；你可以自己整理，或者明确选择把特定内容交给AI助手分析。

## 环境与兼容性

| 项目 | 状态 |
| --- | --- |
| 操作系统 | Windows x64；密钥扫描和DPAPI依赖 Windows |
| Python | 推荐 Python 3.13 x64（已验证 3.13.11），需要 Tkinter |
| QQ | 已验证 9.9.21.38711；其他版本不保证 |
| 微信 | 已验证 4.1.15.13；其他版本不保证 |
| FFmpeg | 可选；WXGF / HEVC 图片需要本机 `ffmpeg` 可执行文件在 PATH 中 |

单元测试使用合成数据，不含真实账号、密钥或聊天附件。测试通过不代表你的客户端或所有消息类型都兼容。

## 一、安装与初始化

从 [GitHub仓库](https://github.com/Jinshi-Liu/ChatNoticeTool) 下载 ZIP 并解压，或先执行：

```powershell
git clone https://github.com/Jinshi-Liu/ChatNoticeTool.git
cd ChatNoticeTool
```

下列命令在 **PowerShell、仓库根目录**执行：

```powershell
# 只为这个项目创建环境，不改变已有 Python 的全局依赖
py -3.13 -m venv .venv

# 仅使用待办组件：无须安装第三方依赖
.\.venv\Scripts\python.exe -B -X utf8 local_setup.py

# 如果还要读取聊天 / 处理附件，再安装依赖
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`local_setup.py` 只创建私有的 `config.json` 和清理所有权标记，不覆盖已有配置、待办或进度。它不会扫描内存，也不会创建桌面快捷方式、服务或开机启动项。

可以使用自己已有的 Python 代替 `py`，但建议仍创建独立环境。若缺少 Tkinter，安装带 Tcl/Tk 的官方 Windows Python。

## 二、只使用桌面待办

无须登录 QQ / 微信，也无须配置聊天路径。

```powershell
.\.venv\Scripts\python.exe -B -X utf8 todo_widget.py
```

初始化环境后也可以双击 `todo-widget.cmd`。首次启动是**空列表**，没有任何作者的个人事项。

- 所有窗口均可缩到220×160，但标题、时间、正文、页签字号始终不变；只调整布局留白和控件排列，不隐藏正文预览。内容显示不下可滚动查看。主窗口和各详情分别记住尺寸，内容与完成状态不受缩放影响。
- 顶部“待办、完成、pin、加号”同排居中对齐，不因窗口高度短而乱换行；宽度不足280时用两行对应列对齐，拉宽恢复单行，字号始终不变。
- 详情页标题、时间随正文一起滚动，向下可将标题区移出视野，向上滚可恢复；只有一个纵向滚动条，正文仍可直接编辑，字号不随滚动/缩放变化。
- **＋**：打开新草稿，填写标题、一个截止时间输入框和正文；点击保存图标或 Ctrl+S 创建。
- **对号 / 撤回箭头**：完成事项，或把已完成事项恢复为待办。
- **点击标题**：直接编辑；可同时打开多个不同事项。长标题在单行输入框中横向滚动。
- **垃圾桶**：确认后移除；不会改动原始聊天记录。
- **置顶图标**：蓝色表示置顶，灰色表示关闭。主窗口和每个详情窗口各自独立；详情时间旁有自己的pin，默认不置顶，按事项保存，主窗口pin不联动其他窗口。
- 没有“保存/已保存”文字按钮；新草稿点击右上手绘保存图标或Ctrl+S创建。已有事项停笔约1秒自动保存；有未保存修改时右上图标优先保存，不误标完成。日期非法或字段冲突时保留草稿，错误显示在正文前。
- 截止时间示例：`10/13`、`10月13日`、`2030-10-13`、`2030-10-13 09:00`、`2030-10-13 上课前`，也可留空。不写年份时沿用原截止日期的年份或本年；无时区待办按电脑本地时间展示，群消息查询默认 UTC+8。

数据保存在 `state/todos.sqlite3`。窗口关闭后程序退出，不驻留托盘、不监控聊天。重新打开保留进度；建议关闭组件后备份自己的待办数据库。

### 脚本 / AI助手添加事项

```powershell
.\.venv\Scripts\python.exe -B -X utf8 todo_store.py add `
  --id "demo:homework:2030-10-13" `
  --title "提交作业" --due "2030-10-13" `
  --due-label "10/13 上课前" --note "题目和提交要求" --source "自己填写的来源"

.\.venv\Scripts\python.exe -B -X utf8 todo_store.py list
.\.venv\Scripts\python.exe -B -X utf8 todo_store.py done "demo:homework:2030-10-13"
.\.venv\Scripts\python.exe -B -X utf8 todo_store.py done "demo:homework:2030-10-13" --undo
.\.venv\Scripts\python.exe -B -X utf8 todo_store.py remove "demo:homework:2030-10-13"
```

同一 `--id` 的 `add` 是更新而非重复创建，并保留 `done`、`done_at` 和 `removed`；明确恢复已移除项可用 `remove ID --restore`。没有固定ID时每次会创建新事项。组件约1.8秒检查**自己的待办库**并刷新，不因此读取 QQ / 微信。

## 三、配置聊天路径

编辑私有 `config.json`（已在 `.gitignore` 中）：

- `qq.database_dir`：当前账号的 `nt_qq\nt_db`。
- `qq.media_dir`：同一账号的 `nt_qq\nt_data`。
- `wechat.account_dir`：当前账号目录，通常名称类似 `wxid_...`。
- `wechat.database_dir`：上述账号的 `db_storage`。

以实际保存位置为准，不要填写别人的账号目录。JSON 中反斜杠要写成 `\\`，也可使用 `/`。可以只配置要使用的平台；版本字段仅是记录，填写版本号不会自动适配格式。

`attachments.extra_allowed_roots` 默认空；只有确实要读取客户端媒体目录外、由选定消息引用的资源时，才添加必要的绝对目录。不要把整个磁盘加入允许范围。

```powershell
# 仅检查文件元数据，不查询聊天正文、不扫描内存
.\.venv\Scripts\python.exe -B -X utf8 chatnotice.py doctor
.\.venv\Scripts\python.exe -B -X utf8 chatnotice.py status
```

`run.cmd` 也只做上述诊断，不启动自动采集。

## 四、首次获取密钥

1. 打开并登录**自己的**客户端，确保本机已经同步需要的消息。
2. 在任务管理器确认该客户端主进程的PID；PID在重启后会变。不要把辅助进程、陌生进程的PID填进去。
3. 在明确授权下运行对应命令，将示例PID替换为自己的PID：

```powershell
# 示例PID 1234 / 5678 不对应真实账号；必须替换
.\.venv\Scripts\python.exe -B -X utf8 memory_reader.py qq --pid 1234 --authorize-memory-read
.\.venv\Scripts\python.exe -B -X utf8 memory_reader.py wechat --pid 5678 --authorize-memory-read
```

内存读取默认禁用，`--authorize-memory-read` 仅为**本次命令**授权。程序核实进程名称，申请 `VM_READ | QUERY_INFORMATION`，不注入、不写内存、不调试、不重启或终止客户端。密钥须通过数据库页面校验才缓存；若部分数据库缺少密钥，后续读取会报错，不会假装成功。

缓存：`state/secrets.dpapi`，由当前Windows用户的DPAPI保护。不要打印、上传或分享缓存；它不是可在别人电脑通用的配置。不要为绕过权限错误关闭安全软件或修改客户端保护。

## 五、选群与按时间导出

先列出本地群记录；只查询群元数据，不读取私聊或群消息正文：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 chatnotice.py groups --platform qq
.\.venv\Scripts\python.exe -B -X utf8 chatnotice.py groups --platform wechat
# 可增加 --contains "群名片段" 过滤显示
```

本地群记录不保证目前仍是成员。QQ使用准确群号；微信使用带 `@chatroom` 的准确群ID。以下是**虚构示例**，运行前替换群ID和时间：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 chatnotice.py export `
  --platform qq --group-id "123456789" `
  --since "2030-10-01" --until "2030-10-07" --confirm

.\.venv\Scripts\python.exe -B -X utf8 chatnotice.py export `
  --platform wechat --group-id "123456789@chatroom" `
  --since "2030-10-01T09:00:00+08:00" --until "2030-10-07T18:00:00+08:00" --confirm
```

- 多个群重复传入 `--group-id`，同一次命令只处理一个平台。
- 没有 `--confirm` 不会读取消息。
- 默认按北京时间 UTC+8；仅日期的 `--until` 包含当天，带时间的 `--until` 是不包含该时刻的上界。
- `scope` 命令只登记范围，并不导出；真正读取使用 `export`。
- 输出在 `exports/<时间戳>/`：`messages.md`、`messages.json`、`summary.json` 和 DPAPI封存的选定原始行。所有这些都属于私有数据。
- `messages.md` 是正文与资源引用，不代表每个附件都已读完。检查 `body_status`、`parse_errors`、`attachment_content_status`；`all_attachment_contents_processed: false` 不能忽略。
- 输出默认以群ID作为群名；可以对照之前的群元数据文件，不影响查询范围。

### 一条命令供脚本 / AI助手调用

统一入口 `read_chats.py` 可直接返回解析后的指定范围消息，不必临时拼Python代码：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 read_chats.py `
  --platform qq --group-id "123456789" --since "2030-10-01" --until now --confirm --show
```

群ID为虚构示例，运行前替换。支持重复 `--group-id`；`--until now` 固定为本次调用时刻；省略 `--show` 只输出统计和路径，避免大段终端输出。默认不扫描内存、不处理附件、不访问网络，也不自动生成待办。缺少密钥时明确失败，需要另行授权取钥。

若已有导出完整覆盖目标群和时间，可以显式复用，避免再次复制 / 解密数据库：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 read_chats.py `
  --platform qq --group-id "123456789" `
  --since "2030-10-01" --until "2030-10-07T18:00:00+08:00" --confirm `
  --from-export "exports/已有批次目录" --show
```

工具校验缓存范围，并只返回本次指定群与时间的消息；不足则报错，不自动拓宽、不静默回退去读客户端。结果明确标注历史缓存的截止时刻，不把旧缓存当作最新。源导出文件可能包含原先其他已授权群，请使用返回的筛选消息，不全量读取源文件。没有长期缓存全账号明文数据库。

### 可选：处理最近一次导出的本地附件

```powershell
# 优先用缓存；不允许新的内存扫描
.\.venv\Scripts\python.exe -B -X utf8 attachments.py

# 确实缺少微信图片密钥时，才打开并登录自己的微信，再显式授权
.\.venv\Scripts\python.exe -B -X utf8 attachments.py --wx-pid 5678 --authorize-memory-read
```

只处理最近一次导出中引用的本地资源，不访问网页或远程下载。部分格式、缩略图、哈希不匹配和缺失资源会保留状态。WXGF / HEVC 需要你另行安装可信的 FFmpeg，本项目不自动下载可执行文件。读取附件可能包含不可信内容，解析器仍有风险。

## 六、客户端关闭与密钥变化

| 情况 | 是否需要客户端运行 |
| --- | --- |
| 第一次取密钥 / 缓存失效后重新取密钥 | 需要打开并登录 |
| 已缓存有效密钥，读取已落盘消息和可解码的本地附件 | 不依赖客户端进程 |
| 客户端关闭期间的新消息 | 本工具不能接收；需客户端重新同步 |
| 只使用待办组件 | 完全不需要客户端 |

普通关闭和重新打开通常不改变原数据库密钥，但内存地址、进程ID会变。清理 / 重建数据库、迁移账号、重装、升级或新增分片可能导致缓存失效或缺少密钥；每次解密都验证，不盲信缓存。不保证所有版本都符合上述行为；已验证的现场流程不等于已验证全部退出 / 重启组合。

## 七、隐私、安全与边界

- 程序不接入云端AI，不发消息、截图、注入、写客户端内存或修改原始聊天数据库。
- 只查询明确授权群的时间范围，保留未知类型；没有本地记录不代表群里没有通知。
- 多个会话可能共享加密数据库：**本地临时副本可能包含其他会话页面**，但查询和导出限制在选定群；这不是只解密某个群的数据库页。
- 解密副本正常退出后删除；进程崩溃、断电可能留下 `runtime` 文件。删除不等于安全擦除，DPAPI也不能防住已控制你Windows账户的攻击者。
- 导出正文、路径、群名、发言者和待办数据库含隐私，虽然部分媒体密钥会被打码，仍不可直接公开。
- 如把导出交给在线AI，所选内容会离开本机；需自行确认授权与服务隐私政策。与本工具没有网络上传功能是两回事。
- 不创建后台服务、计划任务、开机启动项或自动快捷方式。

报告问题前请阅读 [SECURITY.md](SECURITY.md)，不要在Issue、日志或截图里发密钥、数据库、真实账号路径和聊天正文。

## 八、清理与卸载

先关闭组件，备份自己需要的导出 / 待办。下面第一条只预览：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 cleanup.py runtime
# 删除缓存、导出、任务和全部勾选进度；需要输入 DELETE 确认
.\.venv\Scripts\python.exe -B -X utf8 cleanup.py runtime --apply
# 预览 / 删除已登记的项目文件；原始 QQ / 微信文件不属于清理目标
.\.venv\Scripts\python.exe -B -X utf8 cleanup.py all
.\.venv\Scripts\python.exe -B -X utf8 cleanup.py all --apply
```

清理拒绝目录联接 / 符号链接，保留未知顶层文件。不会删除 `.git` 或 `.venv`；完整卸载后可自行删除剩余仓库目录。移动项目目录后重新核对所有权标记，清理不会自动绕过路径检查。删除密钥缓存后，聊天读取需要重新取密钥。

## 开发与测试

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m unittest discover -s tests -q
```

Windows上的测试覆盖页面认证、WAL、群 / 时间限制、安全清理、持久化、并发编辑、单行标题、图标与高DPI布局。CI仅使用合成数据，不连接真实客户端；没有正式跨版本兼容承诺。

主要文件：

| 文件 | 职责 |
| --- | --- |
| `read_chats.py` | 一次调用的范围读取 / 显式缓存复用工具 |
| `chatnotice.py` | 配置、诊断、准确群 / 时间范围、导出入口 |
| `memory_reader.py` / `cipher.py` | 授权取钥、DPAPI、认证解密 |
| `database_reader.py` | 临时快照、WAL、一致性校验 |
| `groups.py` / `messages.py` | 群元数据、指定范围正文解析 |
| `attachments.py` / `document_formats.py` | 受限的本地资源解析 |
| `todo_store.py` | 独立待办数据库和命令行接口 |
| `todo_widget.py` / `widget_ui.py` / `todo_detail.py` | 桌面UI、事件绑定、模型无关的数据刷新 |
| `local_setup.py` / `cleanup.py` | 私有配置初始化与可逆清理 |

欢迎用脱敏或合成数据贡献适配，见 [CONTRIBUTING.md](CONTRIBUTING.md)。技术参考与依赖见 [NOTICE.md](NOTICE.md)。

## 许可证

Copyright 2026 ChatNoticeTool contributors.

本项目以 **[Apache License 2.0](LICENSE)** 开源。第三方参考项目与依赖保留各自许可证，来源说明见 [NOTICE.md](NOTICE.md)；参考第三方格式不代表获得转载其全部源码的许可。
