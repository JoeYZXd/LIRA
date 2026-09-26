"""LIRA 设备端包。

单进程 asyncio 编排器（Key Decisions）：audio/dialog/vision/appliances 模块按管线
阶段划分，经 HAL 抽象与硬件解耦。
"""
