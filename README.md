# Douyin Image Downloader

一个专注于抖音账号图文作品的原图下载工具。它从账号页产生的作品接口响应中提取图集，只下载正文图片，不处理视频、直播、评论、推荐流或页面装饰图。

项目默认采用隐私最小化设计：不复制个人浏览器 Profile，不在清单中保存账号链接、作品原始 ID、文案或带签名的 CDN URL，文件名使用不直接包含源 ID 的伪名摘要。

## 核心能力

- 只接受 HTTPS 抖音账号页，并把接口响应绑定到目标域名和目标账号
- 只处理作品数据里的图集，排除视频封面、头像和页面图片
- 按无水印地址、常规地址、下载地址的顺序保留多个候选，失败时自动降级
- 记录分页 cursor、`has_more` 和明确的停止原因，不把部分抓取冒充完整抓取
- 在 JSON 解析前核对接口真实响应字节，并限制单响应、累计响应字节和响应次数
- 对 429、5xx 和网络错误做有限指数退避重试
- 先写同目录临时文件，完整解码成功后原子替换
- 校验 MIME、文件长度、图片格式、实际尺寸和 SHA-256
- 仅复用已重新校验的现有文件；损坏文件不会被计为成功
- 输出计数可核对的 JSON 清单；在 POSIX 系统上，生成目录默认为 `0700`、文件默认为 `0600`

## 安装

需要 Python 3.10 或更高版本。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Windows 激活虚拟环境：

```powershell
.venv\Scripts\Activate.ps1
```

## 使用

游客模式：

```bash
python -m scraper "https://www.douyin.com/user/ACCOUNT_ID"
```

机器可读输出：

```bash
python -m scraper "https://www.douyin.com/user/ACCOUNT_ID" --json
```

已安装为命令行工具时，也可以使用：

```bash
douyin-image "https://www.douyin.com/user/ACCOUNT_ID"
```

### 需要登录时

使用本工具专用的 Playwright 会话目录，不要把 `--session-dir` 指向个人 Chrome、Edge 或其他浏览器 Profile：

```bash
python -m scraper "https://www.douyin.com/user/ACCOUNT_ID" \
  --session-dir .douyin-session \
  --no-headless \
  --login-wait 60
```

首次运行可在预留时间内手动登录。该目录可能包含登录凭据，已被 `.gitignore` 排除；不要上传、分享或打包它。

工具会在新会话目录中创建专用标记，并拒绝打开未带标记的非空目录，以降低误用个人浏览器 Profile 的风险。

## 常用参数

| 参数 | 作用 | 默认值 |
|---|---|---:|
| `--max-scroll` | 最大滚动轮数 | `50` |
| `--stall-rounds` | 连续无新作品后停止 | `5` |
| `--max-items` | 最多收集多少条作品 | `1000` |
| `--max-images` | 单次最多计划多少张图片 | `5000` |
| `--scroll-pause` | 每轮滚动等待秒数 | `2.0` |
| `--retries` | 每个候选地址的重试次数 | `3` |
| `--request-timeout` | 单次图片请求超时秒数 | `30` |
| `--max-file-mb` | 单张图片大小上限 | `50` |
| `--max-total-mb` | 单次运行累计图片字节上限 | `5120` |
| `--redownload` | 忽略已验证文件并重新下载 | 关闭 |
| `--output-dir` | 图片输出根目录 | `downloads/account_images` |
| `--manifest-dir` | 清单输出根目录 | `output` |
| `--json` | stdout 只输出结果 JSON | 关闭 |

## 输出

```text
downloads/account_images/
└── account_<摘要>/
    ├── post_<摘要>_00.webp
    └── post_<摘要>_01.jpg

output/
└── account_<摘要>_manifest.json
```

清单包含：

- 抓取是否完整及停止原因
- 接口响应、作品、图文帖和计划图片数量
- 每张图片的下载状态、实际格式、尺寸、字节数、SHA-256 和尝试次数
- 下载、已验证复用、失败三类可核对计数
- 本次运行使用的作品、图片、单文件和累计字节上限

清单默认不包含账号 URL、原始账号 ID、原始作品 ID、作品文案、图片 URI 或完整 CDN URL。

摘要标识与图片 SHA-256 仍可能用于关联同一账号、作品或文件；下载的原图也可能保留平台或拍摄设备写入的元数据。因此，下载目录和清单都应视为私密本地数据，不要公开提交或分享。

## 退出码

| 退出码 | 含义 |
|---:|---|
| `0` | 成功、按用户限制完成，或已确认没有图文帖 |
| `2` | 输入参数不受支持 |
| `3` | 未捕获到有效作品接口响应 |
| `4` | 抓取可能不完整 |
| `5` | 至少一张图片下载失败 |
| `6` | 运行依赖缺失 |
| `7` | 未分类的运行时错误 |
| `130` | 用户中断运行 |

## 完整性边界

抖音网页接口可能因游客权限、验证码、风控或账号可见性而只返回部分内容。工具不会绕过访问控制，也不会宣称网页链路之外的内容已被抓取。接口响应安全预算为单响应 `10 MiB`、单次累计 `100 MiB`、最多 `128` 个匹配响应；达到任一上限时会停止捕获并将结果标记为不完整。清单中的 `capture.complete` 和 `completion_reason` 是判断结果范围的依据。

## 安全与合规

- 仅下载你有权访问、保存和使用的内容。
- 遵守平台条款、版权、隐私和适用法律。
- 工具不会绕过登录、验证码、付费或其他访问控制。
- 本项目为非官方工具，与平台无隶属或背书关系。
- 不要提交下载内容、会话目录、Cookie、凭据、清单或本地状态文件。

## 开发验证

```bash
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m pytest
python -m pip_audit -r requirements-dev.txt
```

## License

MIT
