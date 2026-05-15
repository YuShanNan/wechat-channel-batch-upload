"""
视频号批量上传核心引擎
基于 Playwright Python 异步 API
"""
from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import os
from logger import get_logger
from pathlib import Path
from datetime import datetime
from typing import Optional, TYPE_CHECKING

# Windows 控制台 UTF-8
sys.stdout.reconfigure(encoding='utf-8') if sys.stdout else None

log = get_logger("uploader")

if TYPE_CHECKING:
    from playwright.async_api import Page, BrowserContext, Frame

CREATE_URL = "https://channels.weixin.qq.com/platform/post/create"


class WeChatUploader:
    """
    视频号上传器
    每个实例绑定一个账号 profile 目录
    """

    def __init__(self, profile_dir: Path, headless: bool = True):
        self.profile_dir = Path(profile_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self._context: Optional[BrowserContext] = None
        self._upload_progress = 0

    async def start(self):
        """启动浏览器。headless=True 时用 --headless=new 避免任务栏图标。"""
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        args = [
            "--mute-audio",
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        ]
        if not self.headless:
            args.extend(["--window-position=100,100", "--window-size=1200,800"])
            self._reset_window_state()
        self._context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            args=args,
        )
        await self._context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            window.chrome = {runtime: {}};
        """)
        return self

    def _reset_window_state(self):
        """删除上次窗口位置缓存，避免 --window-position 被覆盖"""
        default = self.profile_dir / "Default"
        for name in ["Preferences", "Sessions"]:
            p = default / name
            try:
                if p.is_dir():
                    import shutil
                    shutil.rmtree(p, ignore_errors=True)
                elif p.exists():
                    p.unlink()
            except Exception:
                pass

    async def close(self):
        if not self._context:
            return
        log.info("关闭浏览器...")
        # 1. 尝试正常关闭 context
        try:
            await self._context.close()
            log.info("浏览器已关闭 (context)")
        except Exception:
            pass
        # 2. 备用：browser.close() 直接终止进程
        browser = self._context.browser if self._context else None
        if browser:
            try:
                await browser.close()
                log.info("浏览器已关闭 (browser)")
            except Exception:
                pass
        self._context = None
        # 3. 兜底：系统级杀死该 profile 的 Chrome 进程
        self._kill_chrome_process()

    def _kill_chrome_process(self):
        """通过 PowerShell 找到并终止使用此 profile 的 Chrome 进程"""
        profile_str = str(self.profile_dir.resolve())
        cmd = (
            f'powershell -Command "'
            f'Get-CimInstance Win32_Process -Filter \\"Name=\'chrome.exe\'\\" | '
            f'Where-Object {{ $_.CommandLine -like \'*{profile_str}*\' }} | '
            f'ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"'
        )
        try:
            subprocess.run(cmd, capture_output=True, timeout=10,
                         creationflags=0x08000000)
        except Exception:
            pass

    async def ensure_login(self, timeout_seconds: int = 120) -> bool:
        """确保已登录。返回 True 表示已登录。"""
        page = await self._context.new_page()
        await page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(5000)

        # 正向检测：已登录时页面左侧有 .account-info，避免 URL 重定向延迟导致的误判
        if await page.locator(".account-info").count() > 0:
            log.info("[登录] 已登录")
            await page.close()
            return True

        log.info(f"[登录] 请在浏览器中扫码登录（{timeout_seconds}秒超时）...")
        for i in range(timeout_seconds):
            if await page.locator(".account-info").count() > 0:
                log.info("[登录] 扫码成功！")
                await page.close()
                return True
            await page.wait_for_timeout(1000)
            if i % 15 == 14:
                log.info(f"已等待 {i + 1} 秒...")

        await page.close()
        log.warning("[登录] 超时，未检测到登录成功")
        return False

    async def scrape_nickname(self, page: Page) -> str:
        """
        从登录后的页面抓取当前用户的视频号昵称。
        昵称位于左侧边栏 .account-info .name 元素中。
        """
        el = page.locator(".account-info .name").first
        if await el.count() > 0:
            text = (await el.text_content()).strip()
            if text:
                return text
        raise RuntimeError("无法获取昵称")

    # ==================== QR 码 ====================

    async def _find_qrcode_frame(self, page: Page) -> "Frame":
        """找到包含二维码的最内层 iframe（reverse 从内到外）"""
        for frame in reversed(page.frames):
            if await frame.locator('text=微信扫码登录').count() > 0:
                return frame
        raise RuntimeError("未找到二维码 iframe")

    async def capture_qrcode(self, page: Page) -> str:
        """截取登录页二维码，返回 base64 data URL"""
        frame = await self._find_qrcode_frame(page)
        # 找到二维码所在的容器，截图整个二维码区域
        qr_area = frame.locator("img").first
        await qr_area.wait_for(state="visible", timeout=30000)
        screenshot = await qr_area.screenshot(type="png")
        import base64
        return "data:image/png;base64," + base64.b64encode(screenshot).decode()

    async def check_qrcode_expired(self, page: Page) -> bool:
        """检测二维码是否已过期（仅匹配可见元素）"""
        frame = await self._find_qrcode_frame(page)
        expired = frame.locator('text=二维码已过期').locator(':visible')
        if await expired.count() > 0:
            return True
        return False

    async def check_qrcode_scanned(self, page: Page) -> str:
        """检测二维码扫描状态，返回 'waiting'|'scanned'|'confirming'"""
        frame = await self._find_qrcode_frame(page)
        if await frame.locator('text=需在手机上进行确认').locator(':visible').count() > 0:
            return "confirming"
        if await frame.locator('text=已扫码').locator(':visible').count() > 0:
            return "scanned"
        return "waiting"

    async def wait_for_scan(self, page: Page, timeout_seconds: int = 180) -> str:
        """
        等待用户扫码登录。
        返回 'logged_in' 表示登录成功，'expired' 表示二维码过期需刷新。
        """
        log.info(f"[QR] 等待扫码登录（{timeout_seconds}秒超时）...")
        for i in range(timeout_seconds * 2):
            # 优先检测登录成功
            if await page.locator(".account-info").count() > 0:
                log.info("[QR] 登录成功！")
                return "logged_in"

            # 检测二维码过期
            try:
                if await self.check_qrcode_expired(page):
                    log.info("[QR] 二维码已过期")
                    return "expired"
            except Exception:
                pass  # iframe 可能暂时不可用

            # 检测扫码状态
            try:
                status = await self.check_qrcode_scanned(page)
                if status == "scanned" and i % 20 == 0:
                    log.info("[QR] 用户已扫码，等待确认...")
                elif status == "confirming" and i % 20 == 0:
                    log.info("[QR] 用户已扫码确认，等待登录...")
            except Exception:
                pass

            await page.wait_for_timeout(500)
        log.warning("[QR] 扫码等待超时")
        return "timeout"

    # ==================== 上传 ====================

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
            log.info(f"[上传] {title}")

            # 1. 导航到创作页
            await page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=60000)
            # 先快速检查是否被重定向到登录页
            await page.wait_for_timeout(2000)
            if "login" in page.url:
                result["status"] = "failed"
                result["error"] = "未登录"
                return result
            # 等待表单渲染——标题输入框出现即表示页面就绪
            # 等待视频标题输入框可见（避免匹配到隐藏的合集标题框）
            title_box = page.get_by_role("textbox", name="概括视频主要内容")
            # 容错：如果 WeChat 改了 placeholder，回退到 CSS 选择器
            if await title_box.count() == 0:
                title_box = page.locator('input[placeholder^="概括"]').first
                if await title_box.count() == 0:
                    title_box = page.get_by_role("textbox").first
            await title_box.wait_for(state="visible", timeout=60000)
            await page.wait_for_timeout(2000)

            # 2. 上传视频文件
            log.info("[1/7] 上传视频文件...")
            # 无头模式下 ant-upload 的隐藏 input 可能不挂载，改用 file chooser 机制
            async with page.expect_file_chooser() as fc_info:
                # 点击上传拖拽区触发文件选择器
                upload_zone = page.locator(".ant-upload-btn").first
                await upload_zone.click()
                await page.wait_for_timeout(500)
            file_chooser = await fc_info.value
            await file_chooser.set_files(video_path)

            # 等待上传完成 —— 检测封面预览区域出现
            await self._wait_for_upload_complete(page)
            log.info("上传完成")

            # 3. 设置封面
            await self._set_cover(page, cover_path, video_path)

            # 4. 填写短标题
            log.info(f"[3/7] 填写标题: {title}")
            await title_box.fill(title)

            # 5. 填写描述
            if description:
                log.info(f"[4/7] 填写描述...")
                await self._fill_description(page, description)

            # 6. 设置位置为"不显示"
            log.info(f"[5/7] 设置位置...")
            await self._set_location_none(page)

            # 7. 选择短剧链接
            if short_drama_name:
                log.info(f"[6/7] 选择短剧: {short_drama_name}")
                await self._select_short_drama(page, short_drama_name)
            else:
                log.info("[6/7] 跳过短剧链接")

            # 8. 定时发表 / 立即发表
            if publish_time:
                log.info(f"[7/7] 设置定时发表: {publish_time}")
                await self._set_scheduled_time(page, publish_time)

            log.info(f"[7/7] 点击发表...")
            await self._click_publish(page)

            # 验证发布结果
            publish_ok = await self._verify_publish(page)
            if publish_ok:
                result["status"] = "published"
                log.info(f"发表成功: {title}")
            else:
                result["status"] = "uncertain"
                result["error"] = "未能确认发表状态"
                log.warning(f"发表状态不确定: {title}")

        except Exception as e:
            result["status"] = "failed"
            result["error"] = str(e)
            log.error(f"失败: {e}", exc_info=True)

        finally:
            await page.close()

        return result

    async def _wait_for_upload_complete(self, page: Page, timeout_ms: int = 600000):
        """等待视频上传完成——发表按钮 class 中 weui-desktop-btn_disabled 消失即为上传完成"""
        log.info("等待上传完成...")
        publish_btn = page.get_by_role("button", name="发表")
        for i in range(timeout_ms // 500):
            cls = await publish_btn.get_attribute("class") or ""
            if "weui-desktop-btn_disabled" not in cls:
                log.info("上传完成，发表按钮已启用")
                self._upload_progress = 99
                return
            # 读取 WeChat 原生进度条
            try:
                el = page.locator(".ant-progress-bg").first
                style = await el.get_attribute("style") or ""
                m = re.search(r"width:\s*([\d.]+)%", style)
                if m:
                    self._upload_progress = float(m.group(1))
            except Exception:
                pass
            await page.wait_for_timeout(500)
        log.info("等待超时，发表按钮仍为禁用状态")

    async def _set_cover(self, page: Page, cover_path: str, video_path: str):
        """设置封面图片——个人主页卡片(3:4) + 分享卡片(4:3)"""
        log.info("[2/7] 设置封面...")

        actual_cover = self._resolve_cover_path(cover_path, video_path)
        if not actual_cover:
            log.info("无自定义封面，使用平台自动封面")
            return

        # 获取所有封面"编辑"按钮：第一个=个人主页卡片(3:4)，第二个=分享卡片(4:3)
        edit_btns = page.get_by_text("编辑", exact=True)
        edit_count = await edit_btns.count()
        if edit_count == 0:
            log.info("未找到封面编辑按钮，跳过")
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
                    log.info(f"{label} 封面已设置")
                else:
                    cancel_btn = page.get_by_role("button", name="取消")
                    if await cancel_btn.count() > 0:
                        await cancel_btn.click()
                        await page.wait_for_timeout(500)
            except Exception as e:
                log.info(f"{label} 封面设置: {e}")

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
            log.info(f"描述填写失败: {e}")

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
            log.info(f"位置点击失败: {e}")
            return

        # 2. 在弹出面板中点击"不显示位置"
        try:
            no_show = page.get_by_text("不显示位置", exact=True)
            await no_show.wait_for(state="visible", timeout=5000)
            await no_show.click()
            await page.wait_for_timeout(500)
            log.info("位置已设为不显示")
        except Exception as e:
            log.info(f"不显示位置选项点击失败: {e}")
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

    async def _select_short_drama(self, page: Page, drama_name: str):
        """选择短剧链接"""
        # 点击"选择链接"
        select_link = page.get_by_text("选择链接")
        if await select_link.count() == 0:
            log.info("未找到选择链接按钮")
            return

        await select_link.first.click()
        await page.wait_for_timeout(500)

        # 点击"视频号剧集"
        drama_tab = page.get_by_text("视频号剧集", exact=True)
        try:
            await drama_tab.wait_for(state="visible", timeout=5000)
            await drama_tab.click()
        except Exception:
            log.info("未找到视频号剧集标签")
            await page.keyboard.press("Escape")
            return

        # 点击选择器打开搜索弹窗（WeChat 可能用"添加"或"关联"）
        drama_selector = page.get_by_text("选择需要添加的视频号剧集")
        if await drama_selector.count() == 0:
            drama_selector = page.get_by_text("选择需要关联的视频号剧集")
        try:
            if await drama_selector.count() > 0:
                await drama_selector.click()
        except Exception:
            pass  # 可能已经打开了，继续搜索

        # 搜索短剧 — 找到可见的剧集搜索框（有同名隐藏输入框在 display:none 面板中）
        all_inputs = page.locator('input[placeholder="搜索内容"]')
        search_box = None
        for j in range(await all_inputs.count()):
            inp = all_inputs.nth(j)
            if await inp.is_visible():
                search_box = inp
                break
        if not search_box:
            log.info("未找到可见的搜索框")
            await page.keyboard.press("Escape")
            return
        try:
            # 逐字输入触发 Vue 的 input 事件和防抖搜索
            await search_box.click()
            await search_box.fill("")
            await page.keyboard.type(drama_name, delay=80)
            await page.keyboard.press("Enter")
            # 等待加载指示器消失
            try:
                await page.locator(".common-table-loading").wait_for(state="visible", timeout=2000)
                await page.locator(".common-table-loading").wait_for(state="hidden", timeout=5000)
            except Exception:
                pass
            await page.wait_for_timeout(1000)
        except Exception:
            log.info("搜索框输入失败")
            await page.keyboard.press("Escape")
            return

        # 等待搜索结果并点击。drama-row 在滚动容器内，Playwright 可能判定不可达
        clicked = False
        try:
            row = page.locator(".drama-row").first
            await row.wait_for(state="attached", timeout=8000)
            # 用 JS 直接点击，绕过 Playwright 可见性检查
            await row.evaluate("el => el.click()")
            await page.wait_for_timeout(500)
            clicked = True
        except Exception as e:
            log.warning(f"点击drama-row失败: {e}")
        if not clicked:
            try:
                await page.get_by_text(drama_name, exact=False).first.click(force=True, timeout=3000)
                clicked = True
            except Exception:
                pass

        if clicked:
            log.info(f"已选择短剧: {drama_name}")
        else:
            log.warning(f"未找到短剧: {drama_name}")
            # 诊断：打印搜索框值和可见的表格行
            try:
                val = await search_box.input_value()
                rows = await page.locator(".drama-row").count()
                all_rows = await page.locator("tr").count()
                tbody = await page.locator(".ant-table-tbody").count()
                log.warning(f"搜索框值={val}, drama-row数={rows}, tr总数={all_rows}, tbody数={tbody}")
                await page.screenshot(path="debug_drama_search.png")
            except Exception:
                pass
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
            log.info("无法切换到定时模式")
            return

        # 找到时间输入框并填写
        # 寻找 datetime-local 或日期时间 input
        datetime_inputs = page.locator('input[type="datetime-local"]')
        if await datetime_inputs.count() > 0:
            # 转换时间格式 "2026-04-28 10:30" → "2026-04-28T10:30"
            formatted = time_str.replace(" ", "T")
            await datetime_inputs.first.fill(formatted)
            log.info(f"定时发表已设置: {time_str}")

    async def _click_publish(self, page: Page):
        """点击发表按钮"""
        publish_btn = page.get_by_role("button", name="发表")
        await publish_btn.wait_for(state="visible", timeout=10000)
        await publish_btn.click()

    async def _verify_publish(self, page: Page, timeout_ms: int = 30000) -> bool:
        """验证发表是否成功——URL 跳到 post/list 或出现「已发表」"""
        try:
            await page.wait_for_url(
                lambda url: "/post/list" in url,
                timeout=timeout_ms,
            )
            log.info("已跳转到 post/list")
            return True
        except Exception:
            pass

        try:
            await page.wait_for_selector(
                'text=/已发表|发表成功/i',
                timeout=10000,
            )
            log.info("检测到[已发表]提示")
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
        log.error("登录失败，退出")
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
    log.info(f"结果: {result}")


if __name__ == "__main__":
    asyncio.run(test_upload())
