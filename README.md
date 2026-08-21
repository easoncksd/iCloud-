# iCloud mail

基于 iCloud Hide My Email，批量创建和管理 `@icloud.com` 隐私邮箱.

## 功能

- **多账号管理** — 每个 Apple 账号独立 Cookie 和会话
- **批量创建** — 不同账号最多 5 个并行，同一账号逐个创建；碰到 Apple 临时限制时每次等待 1 分钟后自动续建
- **邮箱列表** — 分页、复制、导出 CSV / TXT、独立取件链接
- **收件箱** — 使用 Apple App 专用密码收信
- **自动创建** — 北京时间 7:00 到 20:00，每隔 60 到 90 分钟给每个有效账号创建 3 到 5 个邮箱；正在批量创建的账号会跳过

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

- `accounts.json` 明文保存 Cookie 和收信密码，不要提交到 Git，也不要发给别人。
- 取件链接 `/pickup/<token>` 不需要登录，拿到链接就能读这封隐私邮箱，不要公开传播。
- 生产环境请设置 `ADMIN_ACCESS_TOKEN`，并通过 HTTPS 反代。

## 启动

```bash
python web_ui.py
python web_ui.py --port 8080
python web_ui.py --scheduler
```

环境变量: `HOST`、 `PORT`、 `ADMIN_ACCESS_TOKEN`、 `PICKUP_BASE_URL`

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

## License

MIT
