# 多账号同时上传 设计文档

**日期：** 2026-05-21  
**范围：** 后端 server.py + 前端 index.html + accounts 模块扩展

---

## 1. 目标

将当前单账号串行上传升级为多账号同时上传。每个账号拥有独立的视频队列、表单状态和上传进度，多个账号可并发运行，互不干扰。

## 2. 界面设计

### 布局

左侧窄边栏 + 右侧主区域：

- **侧边栏（~200px）：** 账号列表，每行显示昵称 + 状态指示（空闲/上传中/等待中/出错）。当前选中账号高亮。底部"添加账号"按钮和管理入口。
- **主区域（剩余宽度）：** 与当前界面几乎一致——拖拽区、视频队列卡片、封面、描述/标题/短剧名/定时表单、发表按钮、内嵌进度条和日志。**去掉顶上的账号选择下拉列表**，因为账号已在侧边栏选中。

### 交互

- 点击侧边栏账号 → 主区域切换为该账号的完整工作区（队列、表单、进度）
- 拖视频到主区域 → 进入当前选中账号的队列；拖到侧边栏某个账号上 → 直接加入该账号队列
- 切走当前账号时，自动保存表单内容到前端内存（`accountStates[name]`），切回来时恢复
- 发表按钮只启动当前选中账号的批量上传，不影响其他账号
- 禁止页面刷新（`beforeunload` 拦截）和右键菜单，防止误操作丢失状态

## 3. 后端改动

### 3.1 状态模型

当前 `_upload_state` 是全局单例 dict。改为按账号 keyed：

```python
_account_upload_state: dict[str, dict] = {}
# 结构：
# {
#   "火玫瑰信箱": {
#     "running": False,
#     "status": "上传中 (2/5)...",
#     "progress": 0,
#     "logs": [...],
#     "result": None,
#     "cancelled": asyncio.Event(),
#     "_task": asyncio.Task 引用,
#     "_video_queue": [...],
#   }
# }
```

### 3.2 API 改造

所有上传相关接口从全局路径改为按账号路由：

| 旧 | 新 |
|---|---|
| `POST /api/upload` 单条 | `POST /api/accounts/<name>/upload/start` 整队列 |
| `GET /api/upload/status` | `GET /api/accounts/<name>/upload/status` |
| `POST /api/upload/cancel` | `POST /api/accounts/<name>/upload/cancel` |

新增：

| 接口 | 用途 |
|---|---|
| `POST /api/accounts/<name>/upload/skip` | 跳过当前视频，继续队列 |
| `POST /api/accounts/<name>/queue` | 编辑队列（增删调序） |
| `GET /api/accounts/<name>/upload/queue` | 获取当前队列和状态 |
| `GET /api/upload/status/all` | 一次性返回所有账号的摘要状态（侧边栏轮询用） |

`start` 请求体：

```json
{
  "videos": [
    {"video_path": "...", "title": "...", "description": "...", "cover_path": "...", "short_drama_name": "...", "publish_time": "...", "location": "none"},
    ...
  ],
  "cover_path": "",
  "interval_min": 5
}
```

### 3.3 并发控制

- `asyncio.Semaphore(max_concurrent)`（默认 3，可通过环境变量/配置文件覆盖）控制同时运行的 Chrome 实例数
- 第 N+1 个账号状态显示"等待中"，前端侧边栏展示
- 上传完成 / 取消后释放槽位，排队中的下一位自动开始

### 3.4 会话缓存与释放

- 上传队列跑完后，session 保留 60 秒（可配置），超时自动关闭 Chrome
- 60 秒内重新开始上传则取消倒计时复用 session
- 失败或取消后立刻释放，不缓存

## 4. 前端改动

### 4.1 状态模型

从全局变量改为按账号 keyed：

```javascript
const accountStates = {};
// accountStates["火玫瑰信箱"] = {
//   videoQueue: [], coverPath: "",
//   form: { title: "", desc: "", drama: "", schedule: "now", time: "", interval: 0 },
//   status: "idle", progress: 0, logs: []
// };
```

### 4.2 账号切换

```
saveCurrentForm() → accountStates[currentAccount] ← loadAccountState(targetAccount)
```

切换时 DOM 重建队列卡片、恢复表单值、恢复进度条和日志。如果目标账号正在上传中，立即开始轮询状态。

### 4.3 轮询策略

- 每一秒轮询当前选中账号的 `/api/accounts/<name>/upload/status`（详细状态）
- 每三秒轮询 `/api/upload/status/all` 更新侧边栏所有账号的状态摘要
- 切走正在上传的账号时，轮询继续但频率降低（每 3 秒），切回来恢复 1 秒

### 4.4 安全措施

- `beforeunload` 事件拦截：页面有未完成上传或未保存内容时弹出确认
- 全局禁止右键菜单：`document.addEventListener('contextmenu', e => e.preventDefault())`

## 5. 队列编辑与跳过

### 5.1 队列编辑

上传运行中，前端可以修改后端队列：
- `POST /api/accounts/<name>/queue` 传入完整的新队列数组，替换旧队列
- 后端每次处理下一个视频前从 `_account_upload_state[name]["_video_queue"]` 重新取，拿到的是最新

### 5.2 跳过当前视频

- `POST /api/accounts/<name>/upload/skip` 设置该账号的 `skip_current` asyncio.Event
- 后端处理循环在 `upload_single()` 前后检查该信号
- 跳过逻辑：关闭当前 page → 记录该视频状态为"已跳过" → 继续处理队列下一个
- 正在上传的视频浪费流量，但不会被发表

## 6. 资源控制

- Semaphore 默认 3 并发，可配
- 空闲 session 60 秒超时释放
- 视频上传超时保持默认 600s，长视频场景不调整
- 所有 Chrome 进程在 atexit 时强制清理（已有 `_kill_chrome_process` 兜底）

## 7. 不做的事情

- 不持久化前端状态到磁盘（刷新丢状态靠禁止刷新解决）
- 不引入进程池或多进程架构（个位数并发 asyncio 够）
- 不改变 `WeChatUploader` 的核心上传逻辑（它已经是单账号设计，无需改）
- 不支持单视频跨账号分发（每个账号独立队列）
