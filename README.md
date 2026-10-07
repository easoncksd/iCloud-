# iCloud批量注册隐私邮箱

基于 iCloud Hide My Email，批量创建和管理 `@icloud.com` 隐私邮箱.

> 这是一个面向个人效率和内部管理场景的 Web 工具：统一管理多个 iCloud 主账号、创建隐私邮箱、查看收件内容，并为每个邮箱生成独立取件链接。请遵守 Apple 服务条款和适用法律，不要用于垃圾邮件、欺诈或未经授权的自动化操作。

## 界面预览

账号管理与创建状态：

![账号管理界面](docs/images/dashboard-accounts.png)

## 功能

- **多账号管理** — 每个 Apple 账号独立 Cookie 和会话
- **批量创建** — 不同账号最多 10 个并行，同一账号逐个创建；碰到 Apple 临时限制时每次等待 30 分钟后自动续建
- **邮箱列表** — 分页、复制、导出 CSV / TXT、独立取件链接
- **收件箱** — 使用 Apple App 专用密码收信；认证失败后自动暂停并延迟复查
- **自动创建** — 北京时间 7:00 到 20:00，每隔 60 到 90 分钟给每个有效账号创建 3 到 5 个邮箱；正在批量创建的账号会跳过；触发临时限制后自动冷却

## 功能说明

### 多账号与 Cookie 会话

每个 iCloud 主账号独立保存 Cookie、Apple ID 和运行状态。添加账号后可以单独检查登录、查看当前隐私邮箱数量和剩余容量；删除账号时会同步清理该账号关联的取件链接、缓存和导出记录。

### 批量创建与限流保护

批量任务支持选择多个主账号并设置创建数量。相同主账号始终串行执行，避免并发请求互相冲突；不同主账号默认最多 10 路并行。检测到 Apple 临时限制时，任务会暂停并在等待窗口结束后自动继续，同时保留任务进度，服务重启后可以断点恢复。

### 邮箱、收件箱与取件链接

每个隐私邮箱都有独立的不可猜测取件链接。后台按主账号统一同步邮件，前端打开取件页时直接读取缓存中的最新内容；有新邮件时自动刷新，减少大量页面同时打开时对 iCloud 的重复请求。

### 导出与防重复

支持按账号和导出状态筛选隐私邮箱，导出 TXT 格式为“隐私邮箱----取件链接”。完成导出后邮箱会自动归类到“已导出”，避免重复导出和重复使用；需要时可以恢复为未导出状态。

## 联系方式

- X： [@fangao798](https://x.com/fangao798)
- Telegram： [联系我](https://t.co/fd6OPHgvKm)

## 前提条件

- iCloud+（Hide My Email 需要订阅）
- Python 3.10+

## 快速开始

```bash
pip install -r requirements.txt
python web_ui.py
```

浏览器打开 http://127.0.0.1:5050

1. 点「添加账号」。Chrome 安装 Cookie Editor，登录 icloud.com，导出 Header String 粘贴进去。
2. 到「设置」勾选账号、填写数量，点「开始创建」。
3. 若要收信，先给账号设置 App 专用密码。

Cookie Editor: https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm

默认只监听本机 127.0.0.1。若要监听 0.0.0.0，必须设置环境变量 `ADMIN_ACCESS_TOKEN`。

## 安全

- `accounts.json` 不直接保存 Cookie 和收信密码；凭据使用账号绑定的 AES-GCM 密文保存，`.credentials.key` 是解密所需的独立密钥。两个文件必须一起备份、权限保持为仅服务账号可读，任何一个丢失都应从同一份备份恢复。
- 不要把 `accounts.json`、`.credentials.key`、`results/`、`logs/` 或生产备份提交到 Git，也不要发给别人。部署脚本会保留这些运行时文件，不会用源码覆盖它们。

### 备份、测试与失败恢复

- 定时备份入口由部署脚本安装为 `deploy/icloud-hme-backup`，统一调用当前版本 `ops/backup.py`，不再维护另一份 Python 副本。
- 一致性备份会短暂停止原本运行中的 `icloud-hme`，取得与服务相同的数据锁，完成后恢复服务。备份期间网页可能暂时不可用；账号、创建任务和剩余数量保留。
- 快照包含 `.credentials.key`，生成时验证凭据解密与 SQLite 完整性。可用 `.venv/bin/python ops/backup.py --verify-archive /var/backups/icloud-hme/具体备份.tar.gz` 在临时目录做恢复演练；只输出统计，不输出凭据。
- 部署失败会先停止服务，再回滚代码与配置；不会用部署前的账号、任务或数据库覆盖新进度。回滚本身失败时保持停服，保留备份供人工恢复。
- `ICLOUD_DATA_DIR` 可指定独立数据根目录；pytest 会在导入代码前自动建立临时目录，禁止使用真实账号或凭据进行单元测试。Web 服务须通过 `python web_ui.py` 启动，以确保在初始化存储和迁移前取得进程锁。
- 邮件正文按 UIDVALIDITY 世代分开缓存。升级后旧版无世代缓存须先完成一次 IMAP 同步才能安全读取；认证暂停的账号需要验证恢复后重建这部分缓存。
- 取件链接 `/pickup/<token>` 不需要登录，拿到链接就能读这封隐私邮箱，不要公开传播。
- 管理界面生产环境必须设置高强度 `ADMIN_ACCESS_TOKEN`，并只通过 HTTPS 反代；不要把 token 放在公开链接、截图、日志或聊天记录中。服务默认只监听 `127.0.0.1`，不建议直接暴露端口。
- 备份会在维护锁下生成清单并校验文件哈希；恢复时应先停止服务，再按清单验证源码和状态文件，避免和运行中的写入交叉。

### 一次性脚本

仓库里的 `inject_*.py` 是历史版本的界面迁移/注入脚本，只用于对应旧版本的人工迁移。当前 `web_ui.py` 已包含正式实现，生产部署时不要重复执行这些脚本，也不要把它们当作启动步骤；重复执行可能造成重复 HTML/JavaScript 注入。新部署只运行 `deploy/install-production.sh`，由脚本统一复制完整源码并验证。

## 启动

```bash
python web_ui.py
python web_ui.py --port 8080
python web_ui.py --scheduler
```

环境变量: `HOST`、 `PORT`、 `ADMIN_ACCESS_TOKEN`、 `PICKUP_BASE_URL`

生产部署建议使用 `deploy/install-production.sh`，它会先对完整源码执行
`compileall` 和全量 `pytest`，再以 SHA-256 清单校验复制结果；运行时的账号、凭据、
缓存和日志目录会被保留，不会被源码同步覆盖。旧 checkout 如果没有
`.github/workflows/tests.yml`，部署会继续完成并明确跳过可选 workflow，不会因此中断。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

## License

MIT
