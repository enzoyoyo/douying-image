# douyin-image — 抖音账号图文原图抓取器

抓取抖音账号中可访问的图文作品，按作品级数据下载高清原图。只保留正文图片，排除缩略图、视频封面、推荐区图片、页面装饰图。

可作为 **Claude Code** / **Codex** 的开源技能直接使用，也可以作为独立命令行工具运行。

## 特性

- 基于抖音作品接口数据抓取，不是扫 `<img>` 标签
- 自动区分图文帖和视频帖，只下载图文帖正文图片
- 高清图优先级选择：无水印 > url_list > download_url_list
- 支持游客模式和复用浏览器登录态两种方式
- 复制浏览器 profile 到临时目录，不干扰用户正在使用的浏览器
- 输出结构化 JSON 清单 + TSV 映射表，方便核查
- 抽样校验图片尺寸，防止误下载缩略图
- 按 `<aweme_id>_<序号>` 命名，文件名稳定可追溯

## 快速开始

### 安装依赖

```bash
pip install -r requirements.txt
playwright install chromium
```

### 基本用法

```bash
# 游客模式抓取
python scraper/douyin_image_scraper.py "https://www.douyin.com/user/MS4wLjABAAAAxxxxxx"

# 复用 Chrome 登录态抓取更多
python scraper/douyin_image_scraper.py "https://www.douyin.com/user/MS4wLjABAAAAxxxxxx" \
    --use-login

# 指定 Chrome profile 路径
python scraper/douyin_image_scraper.py "https://www.douyin.com/user/MS4wLjABAAAAxxxxxx" \
    --use-login --chrome-profile ~/.config/google-chrome

# 显示浏览器窗口（调试）
python scraper/douyin_image_scraper.py "https://www.douyin.com/user/MS4wLjABAAAAxxxxxx" \
    --no-headless
```

### 命令行参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `url` | 抖音账号页 URL（必填） | — |
| `--use-login` | 复用本机浏览器登录态 | 否 |
| `--chrome-profile` | Chrome 用户数据目录路径 | 自动检测 |
| `--output-dir` | 图片下载根目录 | `downloads/account_images` |
| `--manifest-dir` | 清单输出目录 | `output` |
| `--max-scroll` | 最大滚动次数 | 50 |
| `--no-headless` | 显示浏览器窗口 | 否 |

## 输出结构

```
downloads/account_images/<account-slug>/
  ├── 7312345678901234_00.webp
  ├── 7312345678901234_01.webp
  ├── 7398765432109876_00.webp
  └── ...

output/
  ├── <account-slug>_image_manifest.json
  └── <account-slug>_image_downloads.tsv
```

### manifest.json 字段

```json
{
  "account_url": "https://www.douyin.com/user/...",
  "account_slug": "MS4wLjABAAAAxxxxxx",
  "total_posts_found": 120,
  "image_post_count": 15,
  "image_count": 47,
  "download_dir": "downloads/account_images/MS4wLjABAAAAxxxxxx",
  "warnings": [],
  "records": [
    {
      "aweme_id": "7312345678901234",
      "desc": "作品标题",
      "image_index": 0,
      "width": 1080,
      "height": 1440,
      "uri": "tos-cn-i-xxx/xxx",
      "url": "https://...",
      "file": "7312345678901234_00.webp"
    }
  ]
}
```

## 作为 Claude Code 技能使用

本项目包含 `.claude/skills/douyin-account-images.md` 技能文件。在 Claude Code 中：

1. 将此仓库克隆到本地
2. 在项目目录中启动 Claude Code
3. 告诉 Claude "抓取这个抖音账号的图文图片" 并提供链接
4. Claude 会自动调用脚本完成抓取

## 工作原理

1. 使用 Playwright 打开抖音账号页
2. 拦截 `/aweme/v1/web/aweme/post/` 接口响应，获取作品级数据
3. 从全部作品中筛选含 `images` 数组的图文帖
4. 对每张图片按优先级选取高清 URL 下载
5. 下载后抽样校验图片尺寸
6. 输出 JSON 清单和 TSV 映射表

## 限制

- 抖音网页接口不保证暴露账号全部内容
- 图文帖分页可能只开放首屏数据
- 登录态只能提高可见性，不保证拿到全部图文帖
- 目标是"尽可能完整抓取网页链路下可访问的图文原图"

## License

MIT
