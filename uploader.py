"""
视频号批量上传核心引擎
基于 Playwright Python 异步 API
"""
import asyncio
import sys
import os
from pathlib import Path
from datetime import datetime
from typing import Optional

# Windows 控制台 UTF-8
sys.stdout.reconfigure(encoding='utf-8')

from playwright.async_api import async_playwright, Page, BrowserContext

CREATE_URL = "https://channels.weixin.qq.com/platform/post/create"


class WeChatUploader:
    """
    视频号上传器
    每个实例绑定一个账号 profile 目录
    """

    def __init__(self, profile_dir: Path, headless: bool = False):
        self.profile_dir = Path(profile_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self._context: Optional[BrowserContext] = None

    async def start(self):
        """启动浏览器，加载持久化 profile"""
        pw = await async_playwright().start()
        self._context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            channel="chrome",
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        return self

    async def close(self):
        if self._context:
            await self._context.close()
            self._context = None

    async def ensure_login(self, timeout_seconds: int = 120) -> bool:
        """
        确保已登录。如果未登录，打开浏览器等待扫码。
        返回 True 表示已登录。
        """
        page = await self._context.new_page()
        await page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)

        if "login" not in page.url:
            print("[登录] 已登录")
            await page.close()
            return True

        print(f"[登录] 请在浏览器中扫码登录（{timeout_seconds}秒超时）...")
        for i in range(timeout_seconds):
            url = page.url
            if "login" not in url:
                print("[登录] 扫码成功！")
                await page.close()
                return True
            await page.wait_for_timeout(1000)
            if i % 15 == 14:
                print(f"  已等待 {i + 1} 秒...")

        await page.close()
        print("[登录] 超时，未检测到登录成功")
        return False

    async def upload_single(
        self,
        video_path: str,
        title: str,
        description: str = "",
        cover_path: str = "",
        short_drama_name: str = "",
        publish_time: str = "",
        location: str = "none",
        declared_original: bool = False,
    ) -> dict:
        """
        上传单个视频，返回结果字典 {status, title, error}

        参数:
            video_path: 视频文件路径
            title: 短标题 (6-16字)
            description: 视频描述
            cover_path: 封面图片路径，空则自动匹配同目录同名 jpg/png
            short_drama_name: 短剧名称（用于搜索挂载链接）
            publish_time: 定时发布时间，格式 "2026-04-28 10:30"，空则立即发布
            location: 位置，"none" 表示不显示
            declared_original: 是否声明原创
        """
        result = {
            "video_path": video_path,
            "title": title,
            "status": "unknown",
            "error": "",
        }

        page = await self._context.new_page()

        try:
            print(f"\n[上传] {title}")

            # 1. 导航到创作页
            await page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)

            if "login" in page.url:
                result["status"] = "failed"
                result["error"] = "未登录"
                return result

            # 2. 上传视频文件
            print("  [1/7] 上传视频文件...")
            # 找隐藏的 file input (.ant-upload-btn 内的 input[type=file])
            file_input = page.locator("input[type=file]").first
            await file_input.set_input_files(video_path)

            # 等待上传完成 —— 检测封面预览区域出现
            await self._wait_for_upload_complete(page)
            print("  上传完成")

            # 3. 设置封面
            await self._set_cover(page, cover_path, video_path)

            # 4. 填写短标题
            print(f"  [3/7] 填写标题: {title}")
            title_box = page.get_by_role("textbox", name="概括视频主要内容")
            await title_box.wait_for(state="visible", timeout=15000)
            await title_box.fill(title)

            # 5. 填写描述
            if description:
                print(f"  [4/7] 填写描述...")
                await self._fill_description(page, description)

            # 6. 设置位置为"不显示"
            print(f"  [5/7] 设置位置...")
            await self._set_location_none(page)

            # 7. 选择短剧链接
            if short_drama_name:
                print(f"  [6/7] 选择短剧: {short_drama_name}")
                await self._select_short_drama(page, short_drama_name)
            else:
                print("  [6/7] 跳过短剧链接")

            # 8. 定时发表 / 立即发表
            if publish_time:
                print(f"  [7/7] 设置定时发表: {publish_time}")
                await self._set_scheduled_time(page, publish_time)

            print(f"  [7/7] 点击发表...")
            await self._click_publish(page)

            # 验证发布结果
            publish_ok = await self._verify_publish(page)
            if publish_ok:
                result["status"] = "published"
                print(f"  ✓ 发表成功: {title}")
            else:
                result["status"] = "uncertain"
                result["error"] = "未能确认发表状态"
                print(f"  ? 发表状态不确定: {title}")

        except Exception as e:
            result["status"] = "failed"
            result["error"] = str(e)
            print(f"  ✗ 失败: {e}")

        finally:
            await page.close()

        return result

    async def _wait_for_upload_complete(self, page: Page, timeout_ms: int = 120000):
        """等待视频上传完成——发表按钮 class 中 weui-desktop-btn_disabled 消失即为上传完成"""
        print("    等待上传完成...")
        publish_btn = page.get_by_role("button", name="发表")
        for i in range(timeout_ms // 500):
            cls = await publish_btn.get_attribute("class") or ""
            if "weui-desktop-btn_disabled" not in cls:
                print("    上传完成，发表按钮已启用")
                return
            await page.wait_for_timeout(500)
        print("    等待超时，发表按钮仍为禁用状态")

    async def _set_cover(self, page: Page, cover_path: str, video_path: str):
        """设置封面图片——个人主页卡片(3:4) + 分享卡片(4:3)"""
        print("  [2/7] 设置封面...")

        actual_cover = self._resolve_cover_path(cover_path, video_path)
        if not actual_cover:
            print("    无自定义封面，使用平台自动封面")
            return

        # 获取所有封面"编辑"按钮：第一个=个人主页卡片(3:4)，第二个=分享卡片(4:3)
        edit_btns = page.get_by_text("编辑", exact=True)
        edit_count = await edit_btns.count()
        if edit_count == 0:
            print("    未找到封面编辑按钮，跳过")
            return

        # 依次为每个封面卡片上传同一张图片
        for idx in range(min(edit_count, 2)):
            label = "个人主页卡片(3:4)" if idx == 0 else "分享卡片(4:3)"
            try:
                await edit_btns.nth(idx).click(force=True, timeout=5000)
                await page.wait_for_timeout(1000)

                # 在弹窗中上传封面（第一张必须上传，第二张可能已自动填充）
                await self._upload_cover_in_dialog(page, actual_cover)

                # 确认（force=true 穿透遮罩层如 .setting-cover-mask）
                confirm_btn = page.get_by_role("button", name="确认")
                if await confirm_btn.count() > 0:
                    await confirm_btn.click(force=True, timeout=5000)
                    await page.wait_for_timeout(1000)
                    print(f"    {label} 封面已设置")
                else:
                    cancel_btn = page.get_by_role("button", name="取消")
                    if await cancel_btn.count() > 0:
                        await cancel_btn.click()
                        await page.wait_for_timeout(500)
            except Exception as e:
                print(f"    {label} 封面设置: {e}")

    async def _upload_cover_in_dialog(self, page: Page, image_path: str):
        """在封面编辑弹窗中上传图片（不可见时跳过，封面可能已存在）"""
        # 点击"上传封面"（可能不可见，如封面已自动生成）
        upload_text = page.get_by_text("上传封面")
        if await upload_text.count() > 0:
            try:
                await upload_text.first.click(timeout=3000)
                await page.wait_for_timeout(500)
            except Exception:
                return  # "上传封面"不可见，封面已自动设置，跳过

        # 找弹窗内的 file input 上传
        file_inputs = page.locator("input[type=file]")
        fi_count = await file_inputs.count()
        if fi_count >= 2:
            await file_inputs.nth(fi_count - 1).set_input_files(image_path)
        elif fi_count >= 1:
            await file_inputs.first.set_input_files(image_path)
        await page.wait_for_timeout(2000)

    def _resolve_cover_path(self, cover_path: str, video_path: str) -> Optional[str]:
        """解析封面路径：优先用 cover_path，否则在同目录找同名图片"""
        if cover_path and Path(cover_path).is_file():
            return cover_path

        video = Path(video_path)
        for ext in (".jpg", ".jpeg", ".png"):
            candidate = video.with_suffix(ext)
            if candidate.is_file():
                return str(candidate)

        return None

    async def _fill_description(self, page: Page, description: str):
        """填写视频描述 (contenteditable div)"""
        editor = page.locator(".input-editor")
        try:
            await editor.wait_for(state="visible", timeout=5000)
            await editor.click()
            # 清空已有内容
            await editor.evaluate("el => { el.textContent = ''; }")
            await page.keyboard.type(description)
        except Exception as e:
            print(f"    描述填写失败: {e}")

    async def _set_location_none(self, page: Page):
        """将位置设置为'不显示'"""
        try:
            # 1. 点击位置行内的城市名，打开位置搜索面板
            # HTML: .form-item > .label(位置) + .form-item-body > .post-position-wrap > .position-display > .position-display-wrap
            form_item = page.locator(".form-item", has=page.get_by_text("位置", exact=True))
            clickable = form_item.locator(".position-display-wrap")
            await clickable.click()
            await page.wait_for_timeout(500)
        except Exception as e:
            print(f"    位置点击失败: {e}")
            return

        # 2. 在弹出面板中点击"不显示位置"
        try:
            no_show = page.get_by_text("不显示位置", exact=True)
            await no_show.wait_for(state="visible", timeout=5000)
            await no_show.click()
            await page.wait_for_timeout(500)
            print("    位置已设为不显示")
        except Exception as e:
            print(f"    不显示位置选项点击失败: {e}")
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

    async def _select_short_drama(self, page: Page, drama_name: str):
        """选择短剧链接"""
        # 点击"选择链接"
        select_link = page.get_by_text("选择链接")
        if await select_link.count() == 0:
            print("    未找到选择链接按钮")
            return

        await select_link.first.click()
        await page.wait_for_timeout(500)

        # 点击"视频号剧集"
        drama_tab = page.get_by_text("视频号剧集", exact=True)
        try:
            await drama_tab.wait_for(state="visible", timeout=5000)
            await drama_tab.click()
        except Exception:
            print("    未找到视频号剧集标签")
            await page.keyboard.press("Escape")
            return

        # 点击选择器打开搜索弹窗
        drama_selector = page.get_by_text("选择需要添加的视频号剧集")
        try:
            await drama_selector.wait_for(state="visible", timeout=5000)
            await drama_selector.click()
        except Exception:
            print("    未找到剧集选择器")
            return

        # 搜索短剧
        search_box = page.get_by_role("textbox", name="搜索内容")
        try:
            await search_box.wait_for(state="visible", timeout=5000)
            await search_box.fill(drama_name)
            await page.keyboard.press("Enter")
        except Exception:
            print("    未找到搜索框")
            await page.keyboard.press("Escape")
            return

        # 等待搜索结果出现（.drama-row 是搜索结果行的精确 class）
        try:
            first_row = page.locator(".drama-row").first
            await first_row.wait_for(state="visible", timeout=5000)
            await first_row.click()
            print(f"    已选择短剧: {drama_name}")
            clicked = True
        except Exception:
            clicked = False

        if not clicked:
            print(f"    未找到短剧: {drama_name}")
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)

    async def _set_scheduled_time(self, page: Page, time_str: str):
        """设置定时发表时间"""
        # 点击"定时" radio
        scheduled_radio = page.get_by_role("radio", name="定时")
        try:
            await scheduled_radio.click()
            await page.wait_for_timeout(500)
        except Exception:
            print("    无法切换到定时模式")
            return

        # 找到时间输入框并填写
        # 寻找 datetime-local 或日期时间 input
        datetime_inputs = page.locator('input[type="datetime-local"]')
        if await datetime_inputs.count() > 0:
            # 转换时间格式 "2026-04-28 10:30" → "2026-04-28T10:30"
            formatted = time_str.replace(" ", "T")
            await datetime_inputs.first.fill(formatted)
            print(f"    定时发表已设置: {time_str}")

    async def _click_publish(self, page: Page):
        """点击发表按钮"""
        publish_btn = page.get_by_role("button", name="发表")
        await publish_btn.wait_for(state="visible", timeout=10000)
        await publish_btn.click()

    async def _verify_publish(self, page: Page, timeout_ms: int = 15000) -> bool:
        """验证发表是否成功"""
        # 策略1: URL 跳离 create 页面
        try:
            await page.wait_for_url(
                lambda url: "/post/create" not in url,
                timeout=timeout_ms,
            )
            return True
        except Exception:
            pass

        # 策略2: 成功提示
        try:
            await page.wait_for_selector(
                'text=/已发表|发表成功|success/i',
                timeout=5000,
            )
            return True
        except Exception:
            pass

        return False


async def test_upload():
    """快速测试：上传 test_video.mp4"""
    profile_dir = Path(__file__).parent / "accounts" / "test" / "profile"
    uploader = WeChatUploader(profile_dir)
    await uploader.start()

    if not await uploader.ensure_login():
        print("登录失败，退出")
        return

    result = await uploader.upload_single(
        video_path=str(Path(__file__).parent / "test_video.mp4"),
        title="自动化上传测试",
        description="#测试 #自动化",
        cover_path="",  # 自动匹配或使用平台封面
        short_drama_name="",  # 不挂短剧
        publish_time="",
        location="none",
    )
    print(f"\n结果: {result}")


if __name__ == "__main__":
    asyncio.run(test_upload())
