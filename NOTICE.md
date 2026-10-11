# 技术参考与第三方组件

ChatNoticeTool — Copyright 2026 ChatNoticeTool contributors.
本项目的新增实现与修改按 Apache License 2.0 提供，见 LICENSE。

## 技术参考

开发过程中查阅了以下公开项目的格式 / 字段与方法资料。发布目录不包含下载的研究脚本，也不安装或调用这些项目的完整自动化包。

- [fanyuantaier/wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica)：Apache-2.0。微信 Config.Cipher 布局、本地媒体容器与图片密钥方法的技术参考；相关实现已改为本项目的有界读取、显式授权、页认证、临时快照与范围限制，不沿用其默认临时目录、全账号扫描或自动化包入口。保留此来源说明。
- [QQBackup/nt_msg_db_util](https://github.com/QQBackup/nt_msg_db_util)：GPL-3.0。NTQQ前缀、SQLCipher参数及消息Protobuf字段资料的参考；没有分发或调用它的 `sqlcipher3` 导出脚本。
- [QQBackup/QQDecrypt](https://github.com/QQBackup/QQDecrypt) 与 [QQBackup/qq-win-db-key](https://github.com/QQBackup/qq-win-db-key)：查阅客户端格式 / 密钥方法说明。GitHub未识别出标准许可证，本仓库不分发其源码，也不运行其调试器、断点或进程终止脚本。
- [SQLCipher](https://github.com/sqlcipher/sqlcipher)：页面布局、AES / HMAC / KDF概念参考。项目不打包SQLCipher二进制。

格式和方法参考不等于允许复制第三方源码。若贡献包含直接改编代码，必须单独保留其版权和许可并确认兼容性；不能用本项目许可证覆盖第三方权利。许可证识别可能随上游更改，使用者应查看各项目的实际LICENSE。

## 运行依赖

第三方包通过 `requirements.txt` 在用户自己的环境中安装，源码 / wheel不随仓库打包。请查看安装版本的许可证元数据：

- cryptography：Apache-2.0 / BSD-3-Clause 双许可。
- Pillow：HPND相关许可。
- zstandard：BSD许可；底层Zstandard有其独立许可。
- pypdf：BSD-3-Clause。
- olefile：BSD-2-Clause。
- Python / Tk：各自运行时许可。

FFmpeg仅作为可选的本地命令使用，本项目不自动下载或分发它；不同编译配置涉及不同许可证，请自行选择合法来源。

手绘UI图标由项目绘制，不包含Apple或腾讯品牌资产。项目与腾讯无关联，也不提供其账号登录或服务器API。
