# 把「聪明钱复盘台 · A股」部署到 Render（免费云）

目标：把本机上跑的 Python 服务搬到 Render，**电脑关机也不影响访问**，手机/别的电脑打开网址就能用。

- 仓库结构已准备好，新增了 3 个文件：`render.yaml`（Render 蓝图）、`requirements.txt`（无第三方依赖，只为构建用）、`.gitignore`
- `server.py` 已支持云平台：自动读 `PORT` 环境变量、监听 `0.0.0.0`，健康检查用现成的 `/api/health`
- 本地运行方式完全没变：`python server.py` 仍是 `127.0.0.1:8765`

---

## 一、先在有 Git 的电脑上装好 Git（一次性）

本机目前**没有安装 git**。任选一种：

```powershell
# 方式 A：winget 一条命令（推荐）
winget install --id Git.Git -e --source winget
```

```text
方式 B：去 https://git-scm.com/download/win 下载安装，一路默认
```

装完**重开一个终端**，`git --version` 能出版本号即可。

> 不想装 Git 也行：在 GitHub 网页上「Add file → Upload files」，直接把整个项目文件夹拖进去（下面的步骤 3 跳过）。

---

## 二、先在本机模拟一次云端启动（确认没问题）

```powershell
cd "E:\AI 编程\wyckoff-a-share"
$env:PORT=10000
python server.py
# 打开 http://127.0.0.1:10000/api/health 应返回 {"ok": true, ...}
```

看到 `访问地址：http://0.0.0.0:10000/` 且 `/api/health` 正常，说明这套代码可以直接上云。

---

## 三、代码传到 GitHub

1. 注册 / 登录 GitHub（免费）：https://github.com/signup
2. 右上角 **+ → New repository** → 名字随意（例：`wyckoff-a-share`）→ 选 **Private**（私有，别人看不到代码）→ **Create repository**
3. 在项目目录执行（把 `你的GitHub用户名` 换掉）：

```powershell
cd "E:\AI 编程\wyckoff-a-share"
git init
git add .
git commit -m "聪明钱复盘台 · A股"
git branch -M main
git remote add origin https://github.com/你的GitHub用户名/wyckoff-a-share.git
git push -u origin main
```

首次 push 会弹浏览器要求登录 GitHub 授权，按提示点「Authorize」即可。

---

## 四、在 Render 上创建服务

1. 打开 https://render.com → **Get Started** → 选 **GitHub** 登录并授权（第一次会要求授予访问仓库的权限，**只勾这一个私有仓库**最安全）
2. 控制台右上 **New +** →
   - 想省事：选 **Blueprint**，选中刚才的仓库，Render 会读 `render.yaml` 自动建服务
   - 手动也行：选 **Web Service**，选中仓库，然后填：
     - **Runtime**：`Python 3`
     - **Build Command**：`pip install -r requirements.txt`
     - **Start Command**：`python server.py`
     - **Health Check Path**：`/api/health`
     - **Instance Type**：`Free`
     - **Region**：`Singapore`（离国内数据源近；没有这个选项就选默认）
3. 点 **Create Web Service / Deploy Blueprint**，等日志出现 `访问地址：https://xxx.onrender.com/` 即可
4. 部署成功后访问：
   - `https://你的服务名.onrender.com/api/health`
   - `https://你的服务名.onrender.com/`（网页）
   - `https://你的服务名.onrender.com/api/wm?code=600519`（W-M 分析，第一次要 8~15 秒）

---

## 五、免费版要知道的几件事

| 项目 | 说明 |
| --- | --- |
| 休眠 | 免费 Web Service **15 分钟没有请求就休眠**，下次访问冷启动约 **30~60 秒**（页面会转圈，别以为挂了） |
| 保活 | 想让它一直醒着，可用 UptimeRobot（免费）每 10 分钟 GET 一次 `https://你的服务名.onrender.com/api/health` |
| 内存 | 免费档 512MB；本工具是标准库纯 Python，占用很小；但「全市场扫描」这种重任务在云端会慢一些 |
| 数据源 | 服务跑在新加坡/美国机房，访问同花顺、腾讯、新浪的速度取决于跨境链路；代码里已有多源降级（同花顺 → 腾讯 → 新浪），真连不上时页面会提示「降级口径」 |
| 公开性 | 部署后的网址是**公开的**（知道网址的人都能打开，且没有登录）。别发到公开群里；若需要密码，可在 Render 前面套 Cloudflare Access，或告诉我给服务加一个简单的口令校验 |
| 更新 | 以后改完代码：`git add . && git commit -m "xxx" && git push` → Render 自动重新部署（`render.yaml` 里 `autoDeploy: true`） |

---

## 六、常见报错对照

| 现象 | 原因 / 处理 |
| --- | --- |
| Build 失败：找不到 `requirements.txt` | 确认文件已提交（`git add .` 那步），或在 Render 里把 Build Command 改成 `echo skip` |
| 部署成功但访问 502 | 服务没监听 `PORT`/`0.0.0.0`：本仓库 `server.py` 已处理；若改过代码请回退 |
| 页面能开但分析报错「取数失败」 | 机房到国内数据源被拦，属网络环境问题；可换 Region（重建服务），或本机 `python server.py --http-only` 先确认是 https 被掐断 |
| 网页白屏 | `web/index.html` 没提交（`.gitignore` 没排除它）；`git ls-files web` 应能看到 `web/index.html`、`web/dd.html` |

---

## 七、可选：自定义域名 / 更省钱

- 自定义域名：Render 服务页 **Settings → Custom Domains**，按提示在域名商加 CNAME。
- 想完全免费又不要冷启动：可换 Railway / Fly.io / 自己的云服务器（Docker），迁移逻辑相同（`PORT` + `0.0.0.0`）。
