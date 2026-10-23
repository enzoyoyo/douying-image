# Changelog

本项目所有显著变更都记录在本文件中。格式参考 Keep a Changelog，版本号遵循语义化版本。

## [1.1.0] - 2026-10-23

### 新增

- **API 主动翻页模式（`--pagination`）**：新增 `auto`（默认）/ `api` / `scroll` 三种作品翻页模式。`api` 模式在页面加载完成后，直接在页面上下文中按 cursor 请求作品列表接口，无需逐轮滚动；请求所需的会话与签名参数由页面自身补齐，业务参数从页面真实请求中自动采样，不手工拼装签名。`auto` 模式在采样失败或翻页中断时自动回退到滚动模式，行为不会比旧版更差。结果 JSON 新增 `pagination_mode` 字段，回退时在警告中标注。
- **并发图片下载（`--concurrency`）**：可指定并行下载线程数（默认 `1` 保持串行）。多线程共享累计字节预算，任一图片超过剩余预算即停止后续下载；结果与清单仍按计划顺序输出，计数与串行模式一致。
- **可配置下载 User-Agent（`--download-user-agent`）**：图片下载默认改用桌面 Chrome User-Agent，降低 CDN 对非常规 UA 的拒绝率；需要时可自定义。
- **新增参数**：`--page-size`（API 每页作品数，默认 20）、`--page-interval`（页间隔秒数，默认 1.0）。

### 变更

- 下载器 `ImageDownloader` 新增 `user_agent` 参数；默认请求头从工具标识 UA 改为桌面 Chrome UA。

### 测试

- 新增 `tests/test_upgrade.py`：覆盖翻页参数采样与过滤、API 翻页终止条件（`has_more`、cursor 停滞）、回退路径、并发下载顺序与预算、下载 UA 配置。

[1.1.0]: https://github.com/enzoyoyo/douying-image/releases/tag/v1.1.0
