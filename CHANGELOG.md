# Changelog

All notable changes to this project will be documented in this file.

The format is based on "Keep a Changelog" and this project adheres to Semantic Versioning.

## [v0.1.0] - 2026-08-05
### Added
- 初始公开版本：`a_share_quant.py` 单文件脚本，包含数据下载、指标计算、回测、参数扫描、样本外验证和可视化功能。
- README 中加入安装、运行示例与常见问题说明。

### Fixed
- 修复“20日累计涨幅”计算错误：原先误用昨日收盘价，现改为与 20 个交易日前的收盘价比较，结果更准确。
- 修复参数扫描中 NaN 夏普值排序问题：在结果中移除 NaN，避免显示异常排序。
- 修复回退到新浪数据源时名称显示不一致的问题：去掉市场前缀（sh/sz）以便统一展示。

### Changed
- Matplotlib 后端处理改进：尝试使用 TkAgg（本地 GUI），若不可用则回退到 Agg 并在终端提示。
- 数据下载：增加带浏览器 UA 的会话构造与稳健重试逻辑。
- 错误处理：在两个数据源都失败时返回友好错误信息，不再输出冗长 traceback（便于终端用户阅读）。

### Notes
- 推荐在虚拟环境中安装 pandas、requests、matplotlib 后运行脚本。
- 在某些受管的 Python 发行版（例如 MSYS2）中，pip 可能不可用；请使用系统包管理器或安装官方 CPython 来运行脚本。


## Unreleased
- 计划项（待办/后续版本建议）：
  - 优先读取本地 `<ticker>.csv` 以便离线运行/测试
  - 为脚本添加 CLI 参数以控制是否保存图表、CSV 路径、与数据源优先级
  - 写单元测试以覆盖核心计算函数（metrics、回测、扫描）
