# 贡献指南

1. 先阅读README、SECURITY和根目录AGENTS.md。完整客户端/桌面测试使用Windows x64与独立虚拟环境；文件导入核心测试仅需Python标准库，可在Windows/macOS/Linux运行。
2. 先用合成数据添加测试，不把真实账号、群ID、路径、密钥、消息或附件提交到Git。
3. Windows完整测试：`python -B -X utf8 -m unittest discover -s tests -q`；无客户端依赖的导入/适配器测试：`python -B -X utf8 -m unittest discover -s tests -p test_imports.py -q`；真实子进程/stdin/图片/撤权烟测：`python -B -X utf8 -m unittest discover -s tests -p test_cli_process.py -q`（同样仅标准库）。新增格式优先复用ImportSource和`examples/adapters/jsonl_adapter.py`，同时补范围、幂等及失败恢复测试。性能改动附合成基准工作量或计时，不以模拟结果承诺真实聊天解密和模型速度，见[性能验证](docs/agent/performance.md)。
4. 修改客户端适配时，说明版本、字段来源、失败行为和只读边界；不要加入发送、注入、写内存、自动监听或远程下载。
5. 修改待办时，保持稳定ID更新不覆盖完成 / 移除状态；字段冲突不得静默覆盖用户草稿。
6. 标注第三方代码来源和许可证；不要把未授权的研究源码、宏、可执行文件或整个依赖目录提交到仓库。
7. 提交前检查 `git diff --cached` 和 `git ls-files`。`.gitignore` 不代替审查。

欢迎改进客户端兼容性、未知类型提示、文档和可复现的UI测试。测试通过不代表真实客户端全部兼容；不能在文档中把“本地没有记录”写成“没有通知”。
