#!/usr/bin/env python3
"""
抖音账号图文原图抓取器

从抖音账号页抓取图文作品，按作品级数据下载高清原图。
只保留正文图片，排除缩略图、视频封面、推荐区图片、页面装饰图。

用法:
    python douyin_image_scraper.py <account_url> [--use-login] [--chrome-profile PATH] [--output-dir DIR] [--max-scroll N]
"""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
import csv
from pathlib import Path
from urllib.parse import urlparse

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("错误: 需要安装 playwright。请运行:")
    print("  pip install playwright && playwright install chromium")
    sys.exit(1)

try:
    from PIL import Image
except ImportError:
    Image = None


# ─── 常量 ───────────────────────────────────────────────────────────────────────

DOUYIN_API_PATTERN = re.compile(r"/aweme/v1/web/aweme/post/")
DEFAULT_OUTPUT_BASE = "downloads/account_images"
DEFAULT_MANIFEST_BASE = "output"
SCROLL_PAUSE = 2.5
MAX_SCROLL_DEFAULT = 50
REQUEST_TIMEOUT = 30000


# ─── 工具函数 ─────────────────────────────────────────────────────────────────────

def extract_account_slug(url: str) -> str:
    """从账号 URL 中提取用户 ID 或 sec_uid 作为目录名。"""
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) >= 2 and parts[0] == "user":
        return parts[1]
    # fallback: 用整段 path 生成 slug
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", parsed.path.strip("/"))
    return slug or "unknown_account"


def pick_best_image_url(image_obj: dict) -> tuple[str, int, int]:
    """
    按优先级选取高清图片 URL:
      1. watermark_free_download_url_list
      2. url_list
      3. download_url_list
    返回 (url, width, height)
    """
    width = image_obj.get("width", 0)
    height = image_obj.get("height", 0)

    for key in (
        "watermark_free_download_url_list",
        "url_list",
        "download_url_list",
    ):
        urls = image_obj.get(key)
        if urls and isinstance(urls, list):
            for u in urls:
                if u and isinstance(u, str) and u.startswith("http"):
                    return u, width, height

    # 有些结构嵌套在 download_addr / url_list 里
    download_addr = image_obj.get("download_addr", {})
    if isinstance(download_addr, dict):
        for key in ("url_list",):
            urls = download_addr.get(key)
            if urls and isinstance(urls, list):
                for u in urls:
                    if u and isinstance(u, str) and u.startswith("http"):
                        return u, width, height

    return "", width, height


def download_image(page, url: str, dest: Path, timeout: int = REQUEST_TIMEOUT) -> bool:
    """用 Playwright 的浏览器上下文下载图片，自动带上 cookie 和 referer。"""
    try:
        resp = page.request.get(url, timeout=timeout)
        if resp.ok:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(resp.body())
            return True
        else:
            print(f"  下载失败 HTTP {resp.status}: {url[:80]}...")
            return False
    except Exception as e:
        print(f"  下载异常: {e}")
        return False


def guess_extension(url: str) -> str:
    """从 URL 猜测文件扩展名，默认 .webp。"""
    path = urlparse(url).path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".heic", ".avif"):
        if ext in path:
            return ext
    return ".webp"


def verify_image_sample(download_dir: Path, records: list, sample_size: int = 5):
    """抽样检查下载图片的尺寸，判断是否为缩略图。"""
    if not Image:
        print("  (未安装 Pillow，跳过图片尺寸校验)")
        return []

    warnings = []
    files = [r for r in records if (download_dir / r["file"]).exists()]
    sample = files[:sample_size]

    for rec in sample:
        fpath = download_dir / rec["file"]
        try:
            with Image.open(fpath) as img:
                w, h = img.size
                if w < 200 and h < 200:
                    msg = f"疑似缩略图: {rec['file']} 实际尺寸 {w}x{h}"
                    warnings.append(msg)
                    print(f"  ⚠ {msg}")
                else:
                    print(f"  ✓ {rec['file']}: {w}x{h}")
        except Exception as e:
            msg = f"无法读取图片 {rec['file']}: {e}"
            warnings.append(msg)

    return warnings


# ─── 核心抓取逻辑 ──────────────────────────────────────────────────────────────────

class DouyinImageScraper:
    """基于 Playwright 的抖音图文帖图片抓取器。"""

    def __init__(
        self,
        account_url: str,
        use_login: bool = False,
        chrome_profile: str | None = None,
        output_dir: str = DEFAULT_OUTPUT_BASE,
        manifest_dir: str = DEFAULT_MANIFEST_BASE,
        max_scroll: int = MAX_SCROLL_DEFAULT,
        headless: bool = True,
    ):
        self.account_url = account_url
        self.use_login = use_login
        self.chrome_profile = chrome_profile
        self.output_dir = Path(output_dir)
        self.manifest_dir = Path(manifest_dir)
        self.max_scroll = max_scroll
        self.headless = headless

        self.slug = extract_account_slug(account_url)
        self.download_dir = self.output_dir / self.slug
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_dir.mkdir(parents=True, exist_ok=True)

        # 收集到的作品数据
        self._raw_posts: list[dict] = []
        self._seen_aweme_ids: set[str] = set()

    def _prepare_browser_args(self) -> dict:
        """准备浏览器启动参数。"""
        launch_args = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        }

        if self.use_login and self.chrome_profile:
            # 复制 Chrome profile 到临时目录，避免锁文件冲突
            src = Path(self.chrome_profile).expanduser()
            if not src.exists():
                print(f"警告: Chrome profile 路径不存在: {src}")
                return launch_args

            tmp_dir = Path(tempfile.mkdtemp(prefix="douyin_profile_"))
            print(f"复制浏览器 profile 到临时目录: {tmp_dir}")
            # 只复制关键文件，避免复制过大缓存
            for item in ("Default", "Cookies", "Local State", "Profile 1"):
                s = src / item
                if s.exists():
                    d = tmp_dir / item
                    if s.is_dir():
                        shutil.copytree(s, d, ignore=shutil.ignore_patterns("Cache", "Code Cache", "GPUCache"))
                    else:
                        shutil.copy2(s, d)

            launch_args["user_data_dir"] = str(tmp_dir)
            launch_args["channel"] = "chrome"
            self._tmp_profile = tmp_dir
        else:
            self._tmp_profile = None

        return launch_args

    def _intercept_api(self, response):
        """拦截抖音作品列表接口响应。"""
        url = response.url
        if not DOUYIN_API_PATTERN.search(url):
            return
        if response.status != 200:
            return

        try:
            data = response.json()
        except Exception:
            return

        aweme_list = data.get("aweme_list", [])
        for post in aweme_list:
            aweme_id = post.get("aweme_id", "")
            if not aweme_id or aweme_id in self._seen_aweme_ids:
                continue
            self._seen_aweme_ids.add(aweme_id)
            self._raw_posts.append(post)

        has_more = data.get("has_more", 0)
        count = len(aweme_list)
        total = len(self._raw_posts)
        print(f"  接口返回 {count} 条作品 (累计 {total} 条, has_more={has_more})")

    def _scroll_to_load_all(self, page):
        """滚动页面触发更多作品加载。"""
        print("开始滚动加载作品...")
        prev_count = 0
        stale_rounds = 0

        for i in range(self.max_scroll):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(SCROLL_PAUSE)

            current_count = len(self._raw_posts)
            if current_count == prev_count:
                stale_rounds += 1
                if stale_rounds >= 5:
                    print(f"  连续 {stale_rounds} 次无新数据，停止滚动")
                    break
            else:
                stale_rounds = 0
            prev_count = current_count
            print(f"  滚动 #{i+1}: 累计 {current_count} 条作品")

    def _filter_image_posts(self) -> list[dict]:
        """从全部作品中筛选出图文帖。"""
        image_posts = []
        for post in self._raw_posts:
            images = post.get("images")
            if not images or not isinstance(images, list) or len(images) == 0:
                continue
            image_posts.append(post)
        return image_posts

    def _build_download_plan(self, image_posts: list[dict]) -> list[dict]:
        """为每张图片生成下载计划。"""
        records = []
        for post in image_posts:
            aweme_id = post.get("aweme_id", "unknown")
            desc = post.get("desc", "")
            images = post.get("images", [])

            for idx, img in enumerate(images):
                url, width, height = pick_best_image_url(img)
                if not url:
                    print(f"  跳过: {aweme_id} 图片 #{idx} 无有效 URL")
                    continue

                ext = guess_extension(url)
                filename = f"{aweme_id}_{idx:02d}{ext}"

                records.append({
                    "aweme_id": aweme_id,
                    "desc": desc,
                    "image_index": idx,
                    "width": width,
                    "height": height,
                    "uri": img.get("uri", ""),
                    "url": url,
                    "file": filename,
                })

        return records

    def _download_all(self, page, records: list[dict]) -> tuple[int, int]:
        """执行全部图片下载，返回 (成功数, 失败数)。"""
        ok, fail = 0, 0
        total = len(records)

        for i, rec in enumerate(records):
            dest = self.download_dir / rec["file"]
            if dest.exists() and dest.stat().st_size > 0:
                print(f"  [{i+1}/{total}] 已存在，跳过: {rec['file']}")
                ok += 1
                continue

            print(f"  [{i+1}/{total}] 下载: {rec['file']}")
            if download_image(page, rec["url"], dest):
                ok += 1
            else:
                fail += 1
            # 避免请求过快
            time.sleep(0.3)

        return ok, fail

    def _write_manifest(self, records: list[dict], image_post_count: int, warnings: list[str]):
        """写入 JSON 清单和 TSV 映射表。"""
        manifest = {
            "account_url": self.account_url,
            "account_slug": self.slug,
            "total_posts_found": len(self._raw_posts),
            "image_post_count": image_post_count,
            "image_count": len(records),
            "download_dir": str(self.download_dir),
            "warnings": warnings,
            "records": records,
        }

        json_path = self.manifest_dir / f"{self.slug}_image_manifest.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        print(f"清单已写入: {json_path}")

        tsv_path = self.manifest_dir / f"{self.slug}_image_downloads.tsv"
        with open(tsv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, delimiter="\t")
            writer.writerow(["aweme_id", "image_index", "width", "height", "file", "url", "desc"])
            for r in records:
                writer.writerow([
                    r["aweme_id"], r["image_index"], r["width"], r["height"],
                    r["file"], r["url"], r["desc"],
                ])
        print(f"TSV 已写入: {tsv_path}")

        return manifest

    def run(self) -> dict:
        """执行完整抓取流程，返回结果摘要。"""
        print(f"═══ 抖音图文原图抓取器 ═══")
        print(f"账号页: {self.account_url}")
        print(f"输出目录: {self.download_dir}")
        print()

        launch_args = self._prepare_browser_args()

        with sync_playwright() as p:
            if "user_data_dir" in launch_args:
                user_data_dir = launch_args.pop("user_data_dir")
                channel = launch_args.pop("channel", None)
                browser_context = p.chromium.launch_persistent_context(
                    user_data_dir,
                    channel=channel,
                    **launch_args,
                )
                page = browser_context.pages[0] if browser_context.pages else browser_context.new_page()
                browser = None
            else:
                browser = p.chromium.launch(**launch_args)
                browser_context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1920, "height": 1080},
                    locale="zh-CN",
                )
                page = browser_context.new_page()

            # 注册接口拦截
            page.on("response", self._intercept_api)

            # 打开账号页
            print("正在打开账号页...")
            try:
                page.goto(self.account_url, wait_until="networkidle", timeout=60000)
            except Exception:
                # networkidle 可能超时，但数据可能已开始加载
                print("  页面加载超时，继续尝试...")

            time.sleep(3)

            # 滚动加载更多
            self._scroll_to_load_all(page)

            # 筛选图文帖
            image_posts = self._filter_image_posts()
            video_count = len(self._raw_posts) - len(image_posts)

            print()
            print(f"═══ 作品统计 ═══")
            print(f"  作品总数: {len(self._raw_posts)}")
            print(f"  图文帖:   {len(image_posts)}")
            print(f"  视频帖:   {video_count}")
            print()

            if not image_posts:
                print("未找到图文帖，跳过下载。")
                result = {
                    "total_posts": len(self._raw_posts),
                    "image_posts": 0,
                    "images_downloaded": 0,
                    "warnings": ["未找到图文帖"],
                }
                if browser:
                    browser.close()
                else:
                    browser_context.close()
                return result

            # 生成下载计划
            records = self._build_download_plan(image_posts)
            print(f"共 {len(records)} 张图片待下载")
            print()

            # 下载图片
            print("═══ 开始下载 ═══")
            ok, fail = self._download_all(page, records)
            print(f"\n下载完成: 成功 {ok}, 失败 {fail}")

            # 抽样校验
            print("\n═══ 抽样校验 ═══")
            warnings = verify_image_sample(self.download_dir, records)

            # 写清单
            print("\n═══ 写入清单 ═══")
            manifest = self._write_manifest(records, len(image_posts), warnings)

            # 关闭浏览器
            if browser:
                browser.close()
            else:
                browser_context.close()

        # 清理临时 profile
        if self._tmp_profile and self._tmp_profile.exists():
            shutil.rmtree(self._tmp_profile, ignore_errors=True)

        print()
        print("═══ 抓取完成 ═══")
        print(f"  图文帖: {len(image_posts)} 条")
        print(f"  图片:   {ok} 张下载成功, {fail} 张失败")
        print(f"  输出:   {self.download_dir}")
        print(f"  清单:   {self.manifest_dir / self.slug}_image_manifest.json")

        return {
            "total_posts": len(self._raw_posts),
            "image_posts": len(image_posts),
            "images_downloaded": ok,
            "images_failed": fail,
            "download_dir": str(self.download_dir),
            "manifest": str(self.manifest_dir / f"{self.slug}_image_manifest.json"),
            "warnings": warnings,
        }


# ─── CLI 入口 ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="抖音账号图文原图抓取器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 游客模式抓取
  python douyin_image_scraper.py https://www.douyin.com/user/MS4wLjABAAAAxxxxxx

  # 复用 Chrome 登录态抓取更多
  python douyin_image_scraper.py https://www.douyin.com/user/MS4wLjABAAAAxxxxxx \\
      --use-login --chrome-profile ~/.config/google-chrome

  # 指定输出目录
  python douyin_image_scraper.py https://www.douyin.com/user/MS4wLjABAAAAxxxxxx \\
      --output-dir ./my_images --manifest-dir ./my_manifests
        """,
    )
    parser.add_argument("url", help="抖音账号页 URL")
    parser.add_argument("--use-login", action="store_true", help="复用本机浏览器登录态")
    parser.add_argument(
        "--chrome-profile",
        default=None,
        help="Chrome 用户数据目录路径 (默认自动检测)",
    )
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_BASE, help="图片下载根目录")
    parser.add_argument("--manifest-dir", default=DEFAULT_MANIFEST_BASE, help="清单输出目录")
    parser.add_argument("--max-scroll", type=int, default=MAX_SCROLL_DEFAULT, help="最大滚动次数")
    parser.add_argument("--no-headless", action="store_true", help="显示浏览器窗口 (调试用)")

    args = parser.parse_args()

    # 自动检测 Chrome profile 路径
    if args.use_login and not args.chrome_profile:
        candidates = [
            Path.home() / ".config" / "google-chrome",
            Path.home() / ".config" / "chromium",
            Path.home() / "Library" / "Application Support" / "Google" / "Chrome",
            Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data",
        ]
        for c in candidates:
            if c.exists():
                args.chrome_profile = str(c)
                print(f"自动检测到 Chrome profile: {c}")
                break
        if not args.chrome_profile:
            print("错误: 未找到 Chrome profile，请用 --chrome-profile 指定路径")
            sys.exit(1)

    scraper = DouyinImageScraper(
        account_url=args.url,
        use_login=args.use_login,
        chrome_profile=args.chrome_profile,
        output_dir=args.output_dir,
        manifest_dir=args.manifest_dir,
        max_scroll=args.max_scroll,
        headless=not args.no_headless,
    )

    result = scraper.run()
    print("\n结果摘要 (JSON):")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
