# dsh-plugin-atuowexin 的 profile 补丁层（bundle）
#
# 这个文件由安装器 install.ps1 从本模板生成：DSH 的 bundle 补丁层是按**绝对路径**
# 挂载插件的（裸包名解析不到 profile 的 node_modules），而每台机器解压位置不同，
# 所以路径必须现场渲染。
#
# 与 qqchat / qqsend **完全独立**：三个 bundle 各自启停。
#
# 占位符：
#   {{LIB}}      插件入口的绝对路径（<插件目录>\lib\index.js）
#   {{WORKDIR}}  摘录产物与导出的落点（<插件目录>\data）
#   {{CONFIG}}   配置文件路径（<插件目录>\config.json）
- insert:
    - id: plugin-atuowexin-readonly-1
      name: '{{LIB}}'
      config:
        # 摘录产物（归一化库 + 会话工作区）与导出都落在这里
        workdir: '{{WORKDIR}}'
        # 不含任何密钥的本机配置：只声明读取后端与脱敏开关
        configPath: '{{CONFIG}}'
        backend: auto
