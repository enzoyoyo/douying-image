---
name: douyin-account-images
description: 当用户要求抓取抖音账号中的图文作品图片、批量下载高清原图、排除缩略图或无关页面图片时使用。适用于抖音账号链接、图文帖原图下载、批量提图、去除视频封面与推荐图区干扰等场景。
---

# 抖音账号图文原图抓取

抓取抖音账号中可访问的图文作品，按作品级数据下载高清原图，只保留正文图片，不下载缩略图、视频封面、推荐区图片或页面装饰图。

## 适用场景

- 用户提供抖音账号链接，例如 `https://www.douyin.com/user/...`
- 用户要批量下载某个账号的图文帖图片
- 用户明确要求高清图、原图、无关图片不要
- 用户希望输出图片目录和可核对的下载清单

## 核心目标

1. 只抓账号"作品"里的图文帖
2. 只下载图文帖正文图片
3. 尽量拿高清版本
4. 排除缩略图、视频封面、推荐流图片、SEO 落地页图片、页面 UI 图片
5. 输出可复查的结构化清单

## 使用方法

本技能提供了一个 Python 脚本 `scraper/douyin_image_scraper.py`。执行前需要确保环境就绪。

### 环境准备

```bash
# 在项目根目录下
pip install -r requirements.txt
playwright install chromium
```

### 运行方式

```bash
# 基本用法 — 游客模式
python scraper/douyin_image_scraper.py "https://www.douyin.com/user/MS4wLjABAAAAxxxxxx"

# 复用浏览器登录态抓取更多内容
python scraper/douyin_image_scraper.py "https://www.douyin.com/user/MS4wLjABAAAAxxxxxx" \
    --use-login --chrome-profile ~/.config/google-chrome

# 指定输出目录
python scraper/douyin_image_scraper.py "https://www.douyin.com/user/MS4wLjABAAAAxxxxxx" \
    --output-dir ./my_downloads --manifest-dir ./my_output

# 显示浏览器窗口（调试用）
python scraper/douyin_image_scraper.py "https://www.douyin.com/user/MS4wLjABAAAAxxxxxx" \
    --no-headless
```

### 当作为 Claude Code / Codex 技能使用时

当用户提供抖音账号链接并要求抓取图文图片时，按以下步骤执行：

1. **确认环境**：检查 `playwright` 和 `Pillow` 是否已安装，如未安装则先安装
2. **运行脚本**：使用 Bash 工具执行 `python scraper/douyin_image_scraper.py <url>`
3. **检查结果**：读取输出的 manifest.json 确认抓取结果
4. **汇报结果**：向用户说明抓到多少图文帖、多少张图片、输出目录在哪

如果游客态抓取数据偏少，提示用户可加 `--use-login` 复用登录态。

## 抓取原则

- **必须**优先基于抖音作品级接口数据抓取，不要基于页面上的 `img` 标签直接扫图
- **必须**只处理作品数据里带 `images` 数组的条目
- **必须**把纯视频作品排除掉
- **必须**把推荐区、相关帖子、搜索结果、Baiduspider 落地页等非账号正文内容排除掉
- **必须**记录每张图对应的作品 ID、序号、尺寸、来源 URL、本地文件名
- **不要**下载头像、封面、缩略图、页面背景图、分享图标等非正文素材

## 高清图选择规则

对每张图片，按以下优先级选择下载地址：

1. `watermark_free_download_url_list`
2. `url_list`
3. `download_url_list`

补充规则：

- 如果无水印下载地址为空，不要报错，直接退回到下一优先级
- 如果多个地址可用，优先保留尺寸更大的版本
- 如果字段里已有 `width` 和 `height`，应一并写入清单
- 如果拿到的是明显缩略图尺寸，应视为异常并在结果里说明

## 登录态处理

- 游客态先抓一次，确认公开可访问的图文帖数量
- 如果账号页显示作品数明显更多，但接口只返回首屏，允许复用本机浏览器登录态继续抓
- **必须**通过"复制浏览器 profile 到临时目录后再启动"的方式复用登录态
- **不要**直接占用用户正在运行的浏览器 profile，避免锁文件冲突或污染原环境
- 如果复用登录态后仍然没有更多图文帖，必须明确告知用户：当前网页链路下图文内容已抓尽，剩余作品不是图文帖或不对网页开放

## 输出要求

输出目录结构：

```
downloads/account_images/<account-slug>/    # 图片文件
output/<account-slug>_image_manifest.json   # JSON 清单
output/<account-slug>_image_downloads.tsv   # TSV 映射表
```

`manifest.json` 包含：

- `account_url` — 原始账号链接
- `account_slug` — 账号标识
- `total_posts_found` — 接口返回的作品总数
- `image_post_count` — 图文帖数量
- `image_count` — 图片总数
- `records` — 每张图片的详细信息

每条 `record` 包含：

- `aweme_id` — 作品 ID
- `desc` — 作品描述
- `image_index` — 图片在作品中的序号
- `width` / `height` — 图片尺寸
- `uri` — 图片 URI
- `url` — 下载地址
- `file` — 本地文件名

## 命名规则

- 文件名格式：`<aweme_id>_<两位序号>.<扩展名>`
- 不要把标题直接拼进文件名，避免特殊字符和超长路径
- 标题保存在清单字段里即可

## 校验规则

- 抽样验证至少数张图片尺寸
- 如果下载后尺寸集中在很小范围，例如明显低于正文图常见尺寸，应重新检查是否误用了缩略图链接
- 如果账号主页显示作品数很多，但图文帖只有少量，必须区分"作品总数"和"图文帖总数"，不要混淆
- 如果新增可见作品都是视频，不要误报"漏抓图片"

## 最终汇报格式

最终回复应明确说明：

- 抓到多少条图文帖
- 共下载多少张图片
- 图片是否为高清原图
- 已排除哪些类型的无关图片（缩略图、视频封面、推荐区图片、UI 图片）
- 输出文件在哪里
- 是否存在网页权限限制

## 关键限制

- 抖音网页接口不保证暴露账号全部内容
- 即使账号页显示更多作品，图文帖分页也可能只开放首屏
- 登录态只能提高可见性，不保证一定拿到更多图文帖
- 该技能目标是"尽可能完整抓取当前网页链路下可访问的图文原图"，不是绕过平台权限限制
