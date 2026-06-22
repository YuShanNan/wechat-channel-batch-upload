"""
视频号批量上传核心引擎
基于 CloakBrowser (stealth Chromium)
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
import shutil
import subprocess
import sys
from logger import get_logger
from pathlib import Path
from typing import Optional, TYPE_CHECKING

# Windows 控制台 UTF-8
sys.stdout.reconfigure(encoding='utf-8') if sys.stdout else None

log = get_logger("uploader")

if TYPE_CHECKING:
    from cloakbrowser import BrowserContext as CloakContext
    from playwright.async_api import Page, Frame

CREATE_URL = "https://channels.weixin.qq.com/platform/post/create"
_WIDTH_RE = re.compile(r"width:\s*([\d.]+)%")

SEL_ACCOUNT_INFO = ".account-info"
SEL_ACCOUNT_NAME = ".account-info .name"
SEL_UPLOAD_ZONE = ".ant-upload-btn"
SEL_PUBLISH_BTN = "发表"
SEL_DRAMA_ROW = ".drama-row"
SEL_DRAMA_SEARCH_INPUT = 'input[placeholder="搜索内容"]'
SEL_DRAMA_TAB = "视频号剧集"
SEL_DRAMA_LINK_BTN = "选择链接"
SEL_TITLE_PLACEHOLDER = "概括视频主要内容"
SEL_TITLE_FALLBACK = 'input[placeholder^="概括"]'
SEL_DESC_EDITOR = ".input-editor"
SEL_PROGRESS_BG = ".ant-progress-bg"
SEL_LOADING = ".common-table-loading"
SEL_QRCODE_LOGIN = "text=微信扫码登录"
SEL_QRCODE_EXPIRED = 'text=二维码已过期'
SEL_QRCODE_CONFIRM = 'text=需在手机上进行确认'
SEL_QRCODE_SCANNED = 'text=已扫码'
SEL_EDIT_BTN = "编辑"
SEL_CONFIRM_BTN = "确认"
SEL_CANCEL_BTN = "取消"
SEL_UPLOAD_COVER = "上传封面"
SEL_FILE_INPUT = 'input[type=file]'
SEL_POSITION_DISPLAY = ".position-display-wrap"
SEL_NO_LOCATION = "不显示位置"
SEL_SCHEDULED_RADIO = "定时"
SEL_DATETIME_INPUT = 'input[type="datetime-local"]'

class WeChatUploader:
    """
    视频号上传器
    每个实例绑定一个账号 profile 目录，使用 CloakBrowser 反检测浏览器
    """

    def __init__(self, profile_dir: Path, headless: bool = True, executable_path: str = ""):
        self.profile_dir = Path(profile_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self._executable_path = executable_path
        self._context: Optional[CloakContext] = None
        self._upload_progress = 0

    @staticmethod
    def find_system_browser() -> str:
        """CloakBrowser 自带浏览器，此方法保留兼容但不再使用"""
        return ""

    async def start(self):
        """启动 CloakBrowser。headless=True 时使用无头模式。"""
        from cloakbrowser import launch_persistent_context_async

        kwargs = dict(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        if not self.headless:
            kwargs["args"] = ["--window-position=100,100", "--window-size=1200,800"]

        self._clean_profile_locks()  # 主动清理残留锁文件
        last_error = None
        for attempt in range(2):
            try:
                self._context = await launch_persistent_context_async(**kwargs)
                return self
            except Exception as e:
                last_error = e
                if attempt == 0:
                    log.info(f"[启动] 浏览器启动失败，清理锁文件后重试: {e}")
                    self._clean_profile_locks()
                    await asyncio.sleep(1)
        raise RuntimeError(f"浏览器启动失败（已重试）: {last_error}")

    def _reset_window_state(self):
        """删除上次窗口位置缓存，避免 --window-position 被覆盖"""
        default = self.profile_dir / "Default"
        p = default / "Preferences"
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()
        except Exception as e:
            log.debug(f"非关键操作失败: {e}")

    def _clean_profile_locks(self):
        """清理 Chrome profile 锁文件，解决 exitCode=21 浏览器无法启动的问题"""
        for d in (self.profile_dir, self.profile_dir / "Default"):
            if not d.exists():
                continue
            for p in d.glob("Singleton*"):
                try:
                    p.unlink()
                except Exception as e:
                    log.debug(f"非关键操作失败: {e}")

    async def close(self):
        if not self._context:
            return
        log.info("关闭浏览器...")
        # 1. 尝试正常关闭 context
        try:
            await self._context.close()
            log.info("浏览器已关闭 (context)")
        except Exception as e:
            log.debug(f"非关键操作失败: {e}")
        # 2. 备用：browser.close() 直接终止进程
        browser = self._context.browser if self._context else None
        if browser:
            try:
                await browser.close()
                log.info("浏览器已关闭 (browser)")
            except Exception as e:
                log.debug(f"非关键操作失败: {e}")
        self._context = None
        # 3. 兜底：系统级杀死该 profile 的 Chrome 进程
        self._kill_chrome_process()
        # 4. 清理残留锁文件，避免下次启动 exitCode=21
        self._clean_profile_locks()

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
        except Exception as e:
            log.debug(f"非关键操作失败: {e}")

    async def ensure_login(self, timeout_seconds: int = 120) -> bool:
        """确保已登录。返回 True 表示已登录。"""
        page = await self._context.new_page()
        await page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=30000)

        # 等待已登录标识出现，避免固定延时
        try:
            await page.locator(SEL_ACCOUNT_INFO).first.wait_for(state="attached", timeout=15000)
            log.info("[登录] 已登录")
            await page.close()
            return True
        except Exception:
            pass

        log.info(f"[登录] 请在浏览器中扫码登录（{timeout_seconds}秒超时）...")
        for i in range(timeout_seconds):
            if await page.locator(SEL_ACCOUNT_INFO).count() > 0:
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
        el = page.locator(SEL_ACCOUNT_NAME).first
        if await el.count() > 0:
            text = (await el.text_content() or "").strip()
            if text:
                return text
        raise RuntimeError("无法获取昵称")

    # ==================== QR 码 ====================

    async def _find_qrcode_frame(self, page: Page) -> "Frame":
        """找到包含二维码的最内层 iframe（reverse 从内到外）"""
        for frame in reversed(page.frames):
            if await frame.locator(SEL_QRCODE_LOGIN).count() > 0:
                return frame
        raise RuntimeError("未找到二维码 iframe")

    async def _find_qrcode_element(self, page: Page):
        """
        多策略查找二维码元素。
        微信可能改版：iframe 内 / 主页面内 / img 标签 / canvas 等。
        返回 (locator, container_label) 或 raise。
        """
        # 策略1: iframe 内的 img（原有逻辑）
        try:
            frame = await self._find_qrcode_frame(page)
            img = frame.locator("img").first
            if await img.count() > 0:
                return img, "iframe-img"
        except RuntimeError:
            pass

        # 策略2: 主页面中查找二维码图片（class/id 包含 qrcode/qr/login 等）
        for sel in [
            'img[class*="qrcode"]', 'img[class*="qr_code"]', 'img[class*="QrCode"]',
            'img[id*="qrcode"]', 'img[id*="qr_code"]',
            'img[src*="qrcode"]', 'img[src*="qr_code"]',
            '.qrcode img', '.qr_code img', '.login-qr img',
            '.wx-login-qr img', '.weixin-qr img',
        ]:
            el = page.locator(sel).first
            if await el.count() > 0:
                return el, f"main-{sel}"

        # 策略3: 主页面中查找 canvas（部分新版用 canvas 渲染二维码）
        for sel in ['canvas[class*="qr"]', 'canvas[id*="qr"]']:
            el = page.locator(sel).first
            if await el.count() > 0:
                return el, f"main-{sel}"

        # 策略4: 查找包含二维码图片的 iframe（不限文字匹配）
        for frame in reversed(page.frames):
            for sel in ['img[class*="qr"]', 'img[src*="qr"]', 'img[src*="login"]']:
                el = frame.locator(sel).first
                if await el.count() > 0:
                    return el, f"fallback-frame-{sel}"

        raise RuntimeError("所有策略均未找到二维码元素")

    async def capture_qrcode(self, page: Page) -> str:
        """截取登录页二维码，返回 base64 data URL（多策略）"""
        qr_area, strategy = await self._find_qrcode_element(page)
        log.info(f"[QR] 使用策略: {strategy}")
        await qr_area.wait_for(state="visible", timeout=30000)
        screenshot = await qr_area.screenshot(type="png")
        return "data:image/png;base64," + base64.b64encode(screenshot).decode()

    async def check_qrcode_expired(self, page: Page) -> bool:
        """检测二维码是否已过期（搜索所有 frame）"""
        for frame in page.frames:
            if await frame.locator(SEL_QRCODE_EXPIRED).locator(':visible').count() > 0:
                return True
        return False

    async def check_qrcode_scanned(self, page: Page) -> str:
        """检测二维码扫描状态，返回 'waiting'|'scanned'|'confirming'（搜索所有 frame）"""
        for frame in page.frames:
            if await frame.locator(SEL_QRCODE_CONFIRM).locator(':visible').count() > 0:
                return "confirming"
            if await frame.locator(SEL_QRCODE_SCANNED).locator(':visible').count() > 0:
                return "scanned"
        return "waiting"

    # ==================== 上传 ====================

    STATUS_PUBLISHED = "published"
    STATUS_FAILED = "failed"
    STATUS_UNKNOWN = "unknown"
    STATUS_UNCERTAIN = "uncertain"

    async def upload_single(
        self,
        video_path: str,
        title: str,
        description: str = "",
        cover_path: str = "",
        short_drama_name: str = "",
        publish_time: str = "",
        location: str = "none",
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
        """
        result = {
            "video_path": video_path,
            "title": title,
            "status": self.STATUS_UNKNOWN,
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
                result["status"] = self.STATUS_FAILED
                result["error"] = "未登录"
                return result
            # 等待表单渲染——标题输入框出现即表示页面就绪
            # 等待视频标题输入框可见（避免匹配到隐藏的合集标题框）
            title_box = page.get_by_role("textbox", name=SEL_TITLE_PLACEHOLDER)
            # 容错：如果 WeChat 改了 placeholder，回退到 CSS 选择器
            if await title_box.count() == 0:
                title_box = page.locator(SEL_TITLE_FALLBACK).first
                if await title_box.count() == 0:
                    title_box = page.get_by_role("textbox").first
            await title_box.wait_for(state="visible", timeout=60000)
            await page.wait_for_timeout(2000)

            # 2. 优先选择短剧链接（必须完全匹配，否则直接失败）
            if short_drama_name:
                log.info(f"[剧名匹配] 搜索短剧: {short_drama_name}")
                await self._select_short_drama(page, short_drama_name)
            else:
                log.info("[剧名匹配] 跳过短剧链接")

            # 上传视频文件
            log.info("上传视频文件...")
            # 无头模式下 ant-upload 的隐藏 input 可能不挂载，改用 file chooser 机制
            async with page.expect_file_chooser() as fc_info:
                # 点击上传拖拽区触发文件选择器
                upload_zone = page.locator(SEL_UPLOAD_ZONE).first
                await upload_zone.click()
                await page.wait_for_timeout(500)
            file_chooser = await fc_info.value
            await file_chooser.set_files(video_path)

            # 等待上传完成 —— 检测封面预览区域出现
            await self._wait_for_upload_complete(page)
            log.info("上传完成")

            # 设置封面
            await self._set_cover(page, cover_path, video_path)

            # 填写短标题
            log.info(f"填写标题: {title}")
            await title_box.fill(title)

            # 填写描述
            if description:
                log.info("填写描述...")
                await self._fill_description(page, description)

            # 设置位置为"不显示"
            log.info("设置位置...")
            await self._set_location_none(page)

            # 定时发表 / 立即发表
            if publish_time:
                log.info(f"设置定时发表: {publish_time}")
                await self._set_scheduled_time(page, publish_time)

            log.info("点击发表...")
            await self._click_publish(page)

            # 验证发布结果
            publish_ok = await self._verify_publish(page)
            if publish_ok:
                result["status"] = self.STATUS_PUBLISHED
                log.info(f"发表成功: {title}")
            else:
                result["status"] = self.STATUS_UNCERTAIN
                result["error"] = "未能确认发表状态"
                log.warning(f"发表状态不确定: {title}")

        except Exception as e:
            result["status"] = self.STATUS_FAILED
            result["error"] = str(e)
            log.error(f"失败: {e}", exc_info=True)

        finally:
            await page.close()

        return result

    async def _wait_for_upload_complete(self, page: Page, timeout_ms: int = 600000):
        """等待视频上传完成——发表按钮 class 中 weui-desktop-btn_disabled 消失即为上传完成"""
        log.info("等待上传完成...")
        publish_btn = page.get_by_role("button", name=SEL_PUBLISH_BTN)
        for i in range(timeout_ms // 500):
            cls = await publish_btn.get_attribute("class") or ""
            if "weui-desktop-btn_disabled" not in cls:
                log.info("上传完成，发表按钮已启用")
                self._upload_progress = 100
                return
            # 读取 WeChat 原生进度条
            try:
                el = page.locator(SEL_PROGRESS_BG).first
                if await el.count() == 0:
                    continue  # 进度条已消失，直接检查发表按钮
                style = await el.get_attribute("style", timeout=5000) or ""
                m = _WIDTH_RE.search(style)
                if m:
                    self._upload_progress = float(m.group(1))
            except Exception as e:
                log.debug(f"非关键操作失败: {e}")  # 获取失败立即跳过
            await page.wait_for_timeout(500)
        raise TimeoutError("视频上传超时，发表按钮仍为禁用状态")

    async def _set_cover(self, page: Page, cover_path: str, video_path: str):
        """设置封面图片——个人主页卡片(3:4) + 分享卡片(4:3)"""
        log.info("设置封面...")

        actual_cover = self._resolve_cover_path(cover_path, video_path)
        if not actual_cover:
            log.info("无自定义封面，使用平台自动封面")
            return

        # 获取所有封面"编辑"按钮：第一个=个人主页卡片(3:4)，第二个=分享卡片(4:3)
        edit_btns = page.get_by_text(SEL_EDIT_BTN, exact=True)
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
                confirm_btn = page.get_by_role("button", name=SEL_CONFIRM_BTN)
                if await confirm_btn.count() > 0:
                    await confirm_btn.click(force=True, timeout=5000)
                    await page.wait_for_timeout(1000)
                    log.info(f"{label} 封面已设置")
                else:
                    cancel_btn = page.get_by_role("button", name=SEL_CANCEL_BTN)
                    if await cancel_btn.count() > 0:
                        await cancel_btn.click()
                        await page.wait_for_timeout(500)
            except Exception as e:
                log.info(f"{label} 封面设置: {e}")

    async def _upload_cover_in_dialog(self, page: Page, image_path: str):
        """在封面编辑弹窗中上传图片（不可见时跳过，封面可能已存在）"""
        # 点击"上传封面"（可能不可见，如封面已自动生成）
        upload_text = page.get_by_text(SEL_UPLOAD_COVER)
        if await upload_text.count() > 0:
            el = upload_text.first
            if not await el.is_visible():
                return  # "上传封面"不可见，封面已自动生成，跳过
            try:
                await el.click(force=True, timeout=3000)
                await page.wait_for_timeout(500)
            except Exception:
                return  # 点击失败（竞态/被遮挡），封面已自动生成，跳过

        # 找弹窗内的 file input 上传
        file_inputs = page.locator(SEL_FILE_INPUT)
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
        editor = page.locator(SEL_DESC_EDITOR)
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
            clickable = form_item.locator(SEL_POSITION_DISPLAY)
            await clickable.click()
            await page.wait_for_timeout(500)
        except Exception as e:
            log.info(f"位置点击失败: {e}")
            return

        # 2. 在弹出面板中点击"不显示位置"
        try:
            no_show = page.get_by_text(SEL_NO_LOCATION, exact=True)
            await no_show.wait_for(state="visible", timeout=5000)
            await no_show.click()
            await page.wait_for_timeout(500)
            log.info("位置已设为不显示")
        except Exception as e:
            log.info(f"不显示位置选项点击失败: {e}")
            try:
                await page.keyboard.press("Escape")
            except Exception as e:
                log.debug(f"非关键操作失败: {e}")

    async def _select_short_drama(self, page: Page, drama_name: str):
        """选择短剧链接，必须完全匹配剧名，否则抛出异常"""
        # 点击"选择链接"
        select_link = page.get_by_text(SEL_DRAMA_LINK_BTN)
        if await select_link.count() == 0:
            raise RuntimeError("未找到选择链接按钮")

        await select_link.first.click()
        await page.wait_for_timeout(500)

        # 点击"视频号剧集"
        drama_tab = page.get_by_text(SEL_DRAMA_TAB, exact=True)
        try:
            await drama_tab.wait_for(state="visible", timeout=5000)
            await drama_tab.click()
        except Exception:
            raise RuntimeError("未找到视频号剧集标签")

        # 点击选择器打开搜索弹窗
        drama_selector = page.get_by_text("选择需要添加的视频号剧集")
        if await drama_selector.count() == 0:
            drama_selector = page.get_by_text("选择需要关联的视频号剧集")
        try:
            if await drama_selector.count() > 0:
                await drama_selector.click()
        except Exception:
            pass

        # 搜索短剧 — 找到可见的剧集搜索框
        all_inputs = page.locator(SEL_DRAMA_SEARCH_INPUT)
        search_box = None
        for j in range(await all_inputs.count()):
            inp = all_inputs.nth(j)
            if await inp.is_visible():
                search_box = inp
                break
        if not search_box:
            raise RuntimeError("未找到搜索框")

        # 搜索短剧并选择结果（最多重试两次）
        for retry in range(3):
            if retry == 0:
                # 首次：清空并逐字输入（fill 不触发 Vue 搜索事件）
                await search_box.click()
                await page.wait_for_timeout(300)
                await search_box.click(click_count=3)  # 三击全选
                await page.keyboard.press("Backspace")
                await page.wait_for_timeout(200)
                await page.keyboard.type(drama_name, delay=120)
                await page.wait_for_timeout(800)
                await page.keyboard.press("Enter")
            elif retry == 1:
                # 第一次重试：直接按 Enter（搜索框大概率还有文本，更快）
                await search_box.click()
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(2000)
            else:
                # 第二次重试：完整重新输入（兜底，搜索框可能被清空了）
                await search_box.click()
                await page.wait_for_timeout(300)
                await search_box.click(click_count=3)
                await page.keyboard.press("Backspace")
                await page.wait_for_timeout(200)
                await page.keyboard.type(drama_name, delay=120)
                await page.wait_for_timeout(800)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(2000)

            # 等搜索请求完成 — 先等 loading 出现再等消失
            try:
                await page.locator(SEL_LOADING).first.wait_for(state="visible", timeout=3000)
                await page.locator(SEL_LOADING).first.wait_for(state="hidden", timeout=10000)
            except Exception:
                pass
            await page.wait_for_timeout(500)

            # 选第一个结果
            try:
                first_row = page.locator(SEL_DRAMA_ROW).first
                await first_row.wait_for(state="attached", timeout=15000)
                await first_row.evaluate("el => el.click()")
                await page.wait_for_timeout(500)
                log.info(f"已选择短剧: {drama_name}")
                return
            except Exception:
                continue

        raise RuntimeError(f"未找到短剧: {drama_name}")

    async def _set_scheduled_time(self, page: Page, time_str: str):
        """设置定时发表时间"""
        scheduled_radio = page.get_by_role("radio", name=SEL_SCHEDULED_RADIO)
        try:
            await scheduled_radio.click()
            await page.wait_for_timeout(500)
        except Exception:
            raise RuntimeError("无法切换到定时模式")

        datetime_inputs = page.locator(SEL_DATETIME_INPUT)
        if await datetime_inputs.count() > 0:
            formatted = time_str.replace(" ", "T")
            await datetime_inputs.first.fill(formatted)
            log.info(f"定时发表已设置: {time_str}")
        else:
            raise RuntimeError("未找到定时时间输入框")

    async def _click_publish(self, page: Page):
        """点击发表按钮"""
        publish_btn = page.get_by_role("button", name=SEL_PUBLISH_BTN)
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
