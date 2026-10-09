"""MURAL Presenter 重构版的共享运行时核心。

``config`` 保存原始运行输入和已确认画布合同；``session`` 负责两种 Provider API
格式；``tools`` 负责结构化工具事务；``recovery`` 负责安全重试与 segment 恢复。
这里不主动导入子模块，避免仅导入包名时读取配置或创建外部客户端。
"""
