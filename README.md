# iCloud HME — 多账号聚合管理平台

基于 iCloud Hide My Email 协议，批量创建 `@icloud.com` 隐私邮箱的商用聚合平台。

- 👥 **多账号管理** — 同时管理多组 iCloud 账号，每个独立存储、独立会话
- 🔗 **账号-别名映射** — 自动提取真实 Apple ID，每个隐私邮箱标注归属账号
- ⏱ **定时调度** — 整点自动触发，多账号轮询创建，触达上限自动切下一个
- 🌐 **Web UI** — 暖色面板，仪表盘 + 账号列表 + 别名管理 + 跨账号批量创建
- 📬 **独立取件链接** — 每个隐私邮箱使用不可猜测 token，自动刷新最新邮件
- ⚡ **后台快速同步** — 每个主账号统一增量收件，页面数量不会放大 Apple IMAP 请求
- 💾 **正文持久缓存** — 最新邮件自动预热，服务重启后仍可快速打开
- 📤 **导出防重** — 已导出邮箱自动归类，默认只展示未导出邮箱
- 🔄 **后台批量创建** — 不同主账号最多 5 个并行、单账号内部串行；每次限流等待 1 分钟后续建

## 前提条件

- **iCloud+ 订阅**（Hide My Email 需要 iCloud+）
- Python 3.10+
- Windows / macOS / Linux

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动 Web UI
python web_ui.py

# 3. 打开 http://127.0.0.1:5050
#    点击左下角「导入 Cookie」添加第一个账号
#    支持粘贴 Cookie Editor 的 Header String 或 JSON
```

## 使用方式

### Web UI（推荐）

```bash
python web_ui.py                    # 启动 Web 界面
python web_ui.py --port 8080        # 指定端口
python web_ui.py --scheduler        # 启动时自动开启调度器
```

界面功能：

| 模块 | 功能 |
|------|------|
| **账号管理** | 添加/切换/删除账号，每个账号独立 Cookie + 会话 |
| **仪表盘** | 账号总数、总别名数、今日创建数，每账号一张状态卡片 |
| **别名列表** | 实时拉取所有别名，按未导出/已导出分类，导出取件链接 TXT |
| **批量创建** | 勾选多个主账号，每账号最多 750 个；最多 5 个主账号并行，每次限流等待 1 分钟，重启后续建 |
| **调度器** | 一键启停，每整点遍历所有活跃账号创建到上限 |

### 命令行调度器

```bash
# 多账号定时调度（需要先通过 Web UI 添加账号）
python scheduler.py

# 指定账号间间隔
python scheduler.py --interval 5

# 后台守护进程
python scheduler.py -d
```

### CLI 手动操作

```bash
# 列出所有别名
python icloud_hme.py list --cookies cookies.json

# 创建别名
python icloud_hme.py create -n 5 --cookies cookies.json

# 删除别名
python icloud_hme.py delete --email xxx@icloud.com --cookies cookies.json
```

## Cookie 获取

| 方式 | 说明 |
|------|------|
| Web UI 导入 | 点击左下角按钮，粘贴 Cookie Editor 的 Header String |
| Chrome 自动提取 | Windows 下 `python icloud_hme.py export-cookies` |
| 命令行 `--cookies` | 指定 JSON 文件路径 |

支持两种输入格式：
- **Header String**：`name1=value1; name2=value2; ...`
- **JSON**：`{"name1":"value1", "name2":"value2"}`

导入后自动持久化到 `accounts.json`，重启无需重新粘贴。

## 调度逻辑

```
每整点触发一轮
  → 遍历所有活跃账号
  → 每个账号创建到 iCloud 返回上限
  → 账号间间隔 3 秒（可配）
  → 全部完成后等待下一个整点
```

## 文件结构

```
├── icloud_hme.py        # 核心库：Cookie 提取 / HME API / 账号身份提取
├── account_manager.py   # 多账号管理器：CRUD / 批量创建 / 别名索引
├── web_ui.py            # Flask Web 面板 + 内置调度器
├── scheduler.py         # 独立命令行调度器
└── requirements.txt     # pip 依赖
```

运行时生成：

```
accounts.json          # 所有账号及 Cookie（自动持久化）
scheduler_state.json   # 调度器历史状态
logs/                  # 运行日志
results/               # 创建的邮箱列表
results/batch_jobs.json # 批量任务状态和断点续建进度
results/mail_bodies.sqlite3 # 有界邮件正文缓存
```

## 依赖

```
requests>=2.25          # HTTP
pycryptodome>=3.15     # Chrome cookie 解密 (Windows)
pywin32>=305           # Windows DPAPI (仅 Windows)
flask>=3.0             # Web UI
```

## 生产部署说明

- 管理面板通过 `ADMIN_ACCESS_TOKEN` 建立 30 天安全 Cookie；取件链接本身是 bearer credential，请勿公开传播。
- `deploy/` 包含当前 Nginx 限流、脱敏日志和 systemd 资源边界配置。
- 取件 token 不会写入 Nginx 日志；邮件中的外部图片默认不加载，避免追踪访问设备 IP。
- 取件查询使用 O(1) token 索引；后台按主账号统一同步，默认间隔 2 秒。
- 取件页前台有新邮件时快速刷新，空闲或隐藏标签页自动降频，可承载大量常驻页面。
- `BATCH_MAX_ACCOUNT_WORKERS` 可调整跨主账号并行数，默认 `5`；同一主账号始终串行。
- 邮件正文通过一次 IMAP FETCH 同时读取头部和正文，连接按主账号复用。
- 正文缓存最多保留 5000 条或 64 MiB，页面数量不会增加缓存上限。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

## License

MIT
