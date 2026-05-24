# 上传增强功能设计文档

## 概述

三个独立功能：失败自动重试、完成通知提醒、最小化到托盘。

## 1. 失败自动重试

### 触发条件
- `upload_single` 返回 `status != "published"`（非"取消"类失败）
- 区分取消：`cancel_ev.is_set()` 时跳过重试

### 行为
- 失败后**立即**重试（不等）
- 最多重试 3 次
- 每次重试前记录日志：`[重试] {title} (1/3)`
- 状态条更新：`重试中 (1/3)…`
- 3 次全失败才标记 `[失败]`
- 进度条不回退——保持当前位置
- 重试计入 `state["progress"]` 计算不变（`_current_index` 不前进）

### 改动文件
- `web_server.py` — `_run_account_upload` 循环内，`upload_single` 结果处理段
- `templates/index.html` — `updateUploadBar` 无需改动（状态文字由后端提供）

## 2. 声音+通知提醒

### 触发时机
- 全部上传完成后（`_run_account_upload` 正常结束，`state["running"] = False`）
- 不触发：取消、异常终止

### 通知
- 后端：`plyer.notification` 弹出 Windows 通知气泡
  - 标题："视频号上传"
  - 内容：`"{success} 个视频上传完成（共 {total} 个）{failed}"`
  - `{failed}` = 如有失败则追加 `"，{n} 个失败"`

### 声音
- 后端：`winsound.MessageBeep(0x00000040)`（`MB_ICONASTERISK`，系统提示音）
- 前提：用户设置 `提示音开启` 为 True

### 设置 UI
- 位置：debug 开关旁边（右侧主区域左下角）
- 形式：`☐ 提示音` 文字链接
- 状态：前端 `localStorage` 持久化，默认勾选
- 后端：新增全局变量 `_sound_enabled`，前端通过 `POST /api/config/sound` 同步状态
  - `POST /api/config/sound` body: `{"enabled": true/false}`
  - 上传线程读取 `_sound_enabled` 决定是否播放声音

### 改动文件
- `web_server.py` — 新增 config 端点、上传完成时发通知
- `templates/index.html` — 提示音开关 UI、开关 JS 逻辑
- `app.py` — 无需改动

## 3. 最小化到托盘

### 依赖
- `pystray` — 系统托盘
- `Pillow` — 生成托盘图标（16x16 绿色圆点或微信图标）

### 行为
- 关闭窗口（点 X）→ 窗口隐藏，图标缩到托盘
- 托盘图标右键菜单：
  - 显示窗口 — `webview.windows[0].show()` 恢复
  - 退出 — `webview.windows[0].destroy()`
- 退出时检测：有上传任务进行中 → 弹出确认框 → 确认后退出

### 图标
- 使用 PIL 动态生成一个 32x32 绿色圆角方形图标（或使用 app 图标文件）
- 默认图标路径：`templates/favicon.ico`（如有），否则生成纯色图标

### 生命周期
- `app.py` `main()` 中，Flask + webview 启动后，创建托盘线程
- 托盘与 webview 独立线程运行，互不阻塞
- 进程退出时托盘自动销毁

### 终止流程
```
窗口 X 按钮 → 窗口最小化（hide）
托盘右键 → 显示窗口 → 窗口恢复（show）
托盘右键 → 退出 → 检查运行任务 → 确认 → destroy 窗口 → 停止托盘 → 退出进程
```

### 改动文件
- `app.py` — 托盘线程创建、生命周期管理
- `templates/index.html` — 无需改动（窗口关闭提示已有 `beforeunload`）

## 验证方法

1. 上传一个必定失败的视频（如无效文件路径），确认重试 3 次后标为失败
2. 正常上传 2 个视频，确认完成后弹通知 + 声音
3. 关闭提示音开关，再次上传，确认只弹通知无声
4. 关闭窗口，确认缩到托盘；托盘右键恢复窗口
5. 托盘退出时，确认有任务运行时弹确认框
