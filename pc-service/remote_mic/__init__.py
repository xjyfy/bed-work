"""手机遥控麦 电脑端服务。

版本号在这里定义，设置页「本机」卡片直接读它。
**发版时要与 `installer/RemoteMic.iss` 的 `#define AppVersion` 一起改**，
`build_installer.ps1` 会校验两者一致，不一致直接中止打包。
"""

__version__ = "1.1.0"

REPO_URL = "https://github.com/xjyfy/bed-work"