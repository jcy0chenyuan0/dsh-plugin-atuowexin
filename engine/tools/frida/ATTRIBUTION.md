# 第三方脚本来源

本目录下的 scan_lea.py / frida_hook.py / validate.py 移植自：

- 仓库：https://github.com/AyaReiOwO/wechat-4.1.15-key-capture
- 许可：见上游仓库 LICENSE（MIT）
- 用途：定位 Weixin.dll 的 codec 配置函数（MMV1 特征串 -> lea 引用 -> .pdata 函数边界）

本插件只调用它们做本地只读扫描/校验；不含发送消息、不联网。
如上游许可有更新，以上游为准。
