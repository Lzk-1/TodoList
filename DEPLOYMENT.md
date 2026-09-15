# 待办事项追踪工具 —— 使用与部署手册

本手册覆盖三类场景：源码直接运行、Windows 打包为 exe、Docker 容器化部署。Linux 与 Windows 步骤分别说明。

## 目录

- [1. 快速开始（5 分钟跑起来）](#1-快速开始5-分钟跑起来)
- [2. Linux 环境部署](#2-linux-环境部署)
- [3. Windows 环境部署](#3-windows-环境部署)
- [4. Windows 打包成 exe](#4-windows-打包成-exe推荐用于非开发人员分发)
- [5. Docker 容器化部署](#5-docker-容器化部署)
- [6. 数据备份与迁移](#6-数据备份与迁移)
- [7. 从旧版 JSON 迁移到 SQLite](#7-从旧版-json-迁移到-sqlite)
- [8. 日常使用指南](#8-日常使用指南)
- [9. 常见问题（FAQ）](#9-常见问题faq)
- [附：环境要求速查表](#附环境要求速查表)

---

## 1. 快速开始（5 分钟跑起来）

### 前置条件
- **Linux / macOS**：系统自带 Python3（>=3.8）
- **Windows**：需手动安装 Python3（[下载地址](https://www.python.org/downloads/)），安装时**勾选 "Add to PATH"**

### 一行启动

```bash
# Linux / macOS
cd todolist
python3 app.py

# Windows（cmd 或 PowerShell）
cd todolist
python app.py
```

看到以下输出即成功：

```
待办事项服务已启动：http://localhost:8000
数据文件：/path/to/data.db
Web 资源：/path/to/web
按 Ctrl+C 退出
```

浏览器打开 `http://localhost:8000/` 即可使用。

---

## 2. Linux 环境部署

### 2.1 源码运行（开发或长期使用）

```bash
# 1. 拷贝项目到任意目录
cp -r todolist /opt/todolist
cd /opt/todolist

# 2. 验证 Python 版本
python3 --version  # 输出应 >= 3.8

# 3. 验证 sqlite3 模块
python3 -c "import sqlite3; print(sqlite3.sqlite_version)"
# 应输出类似 3.46.1

# 4. 启动（前台）
python3 app.py

# 5. 启动到后台（推荐生产环境用 systemd，见 2.2）
nohup python3 app.py > /var/log/todolist.log 2>&1 &
```

### 2.2 用 systemd 自启动（推荐生产部署）

创建 `/etc/systemd/system/todolist.service`：

```ini
[Unit]
Description=TodoList Service
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/todolist
# 用 /usr/bin/env 查找 python3，避免硬编码路径在不同发行版失效
# 如确认本机路径固定，可改为 /usr/bin/python3 或 /usr/local/bin/python3
ExecStart=/usr/bin/env python3 /opt/todolist/app.py 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启用：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now todolist
sudo systemctl status todolist  # 验证运行状态
```

> 提示：`User=www-data` 需确保该用户对 `/opt/todolist` 有读写权限（含 data.db 文件）。可改用 `User=你的用户名` 或执行 `sudo chown -R www-data:www-data /opt/todolist`。

### 2.3 防火墙放行端口

```bash
# Ubuntu / Debian
sudo ufw allow 8000/tcp

# CentOS / RHEL
sudo firewall-cmd --permanent --add-port=8000/tcp
sudo firewall-cmd --reload
```

### 2.4 Nginx 反向代理（可选，用于 80/443 端口访问）

```nginx
server {
    listen 80;
    server_name todolist.example.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

---

## 3. Windows 环境部署

### 3.1 安装 Python（一次性）

1. 访问 [python.org/downloads](https://www.python.org/downloads/)，下载 3.10+ 安装包
2. 运行安装包，**务必勾选 "Add Python to PATH"**（最底部的复选框）
3. 验证：打开 cmd 输入 `python --version`，应输出版本号

### 3.2 源码运行

```cmd
:: 1. 把整个 todolist 目录拷到任意位置，如 D:\tools\todolist
:: 2. 在该目录打开 cmd（资源管理器地址栏输入 cmd 回车即可）
cd /d D:\tools\todolist
python app.py
```

看到启动信息后浏览器打开 `http://localhost:8000/`。

### 3.3 双击启动（推荐日常使用）

在 `todolist` 目录下新建 `start.bat`：

```bat
@echo off
cd /d "%~dp0"
python app.py
pause
```

以后只需**双击 `start.bat`** 即可启动。关闭窗口 = 停止服务。

### 3.4 开机自启（可选）

1. 按 `Win+R` 输入 `shell:startup` 打开启动文件夹
2. 把 `start.bat` 的快捷方式拖进去
3. 每次开机自动启动

### 3.5 防火墙放行（局域网访问）

```cmd
:: 用 8000 端口对外提供服务时执行
netsh advfirewall firewall add rule name="TodoList" dir=in action=allow protocol=TCP localport=8000
```

---

## 4. Windows 打包成 exe（推荐用于非开发人员分发）

把整个项目打包成**单文件 TodoList.exe**，无需 Python 环境即可在任意 Windows 机器双击运行。

### 4.1 打包人（开发人员，做一次）

#### 前置条件
- Windows 机器装好 Python3（同 3.1）
- 项目根目录下已有 `build_exe.bat`

#### 打包步骤

1. 双击 `build_exe.bat`，或在 cmd 中：

```cmd
cd /d D:\tools\todolist
build_exe.bat
```

2. 等待 1-2 分钟，自动完成：
   - 安装 PyInstaller
   - 用 `--onefile` 模式打包
   - 把 `web/` 目录嵌入 exe 资源

3. 产物在 `dist\TodoList.exe`，约 12-15 MB 单文件

#### 打包参数说明

脚本核心命令（手动执行等价）：

```cmd
pyinstaller --onefile --name TodoList --add-data "web;web" --clean --noconfirm app.py
```

| 参数 | 作用 |
|------|------|
| `--onefile` | 打包为单文件 exe，运行时自解压到临时目录 |
| `--name TodoList` | 输出 exe 名称 |
| `--add-data "web;web"` | 把 web/ 目录嵌入 exe（Windows 用 `;` 分隔） |
| `--clean` | 清理上次缓存，避免旧资源混入 |
| `--noconfirm` | 不询问确认直接覆盖 |

### 4.2 最终用户（非开发人员，零安装）

1. 拿到 `TodoList.exe`，拷到任意目录（桌面、U 盘都行）
2. **双击运行**，自动弹出黑窗口
3. 1.5 秒后**自动打开浏览器**到 `http://localhost:8000/`
4. 直接在网页里使用
5. 数据文件 `data.db` 自动生成在 exe 旁边，所有数据存这里
6. **关闭黑窗口 = 停止服务**

### 4.3 exe 使用注意事项

- **数据迁移**：把 `TodoList.exe` + `data.db` 一起拷走，到新机器双击即可继续使用
- **端口冲突**：默认 8000 端口被占用时，可在 cmd 执行 `TodoList.exe 9000` 换端口
- **数据备份**：定期复制 `data.db` 即完成备份
- **多实例**：同一台机器可以跑多份 exe，但要指定不同端口 + 放在不同目录（数据独立）

---

## 5. Docker 容器化部署

### 5.1 编写 Dockerfile

在项目根目录新建 `Dockerfile`：

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY . /app

EXPOSE 8000

CMD ["python3", "app.py"]
```

### 5.2 构建与运行

```bash
# 构建
docker build -t todolist:latest .

# 运行（只挂载 data.db 单文件，避免覆盖容器内代码）
docker run -d \
  --name todolist \
  -p 8000:8000 \
  -v /opt/todolist/data.db:/app/data.db \
  todolist:latest
```

> ⚠️ **避免挂载整个目录到 /app**：这会覆盖容器内的 `app.py` 和 `web/` 代码导致启动失败。**只挂载 `data.db` 单文件**即可，代码由镜像提供、数据由宿主持久化。

### 5.3 docker-compose 方式

新建 `docker-compose.yml`：

```yaml
version: "3.8"
services:
  todolist:
    build: .
    image: todolist:latest
    ports:
      - "8000:8000"
    volumes:
      - ./data.db:/app/data.db
    restart: unless-stopped
```

启动：`docker compose up -d`

> 注意：宿主侧 `./data.db` 必须是**已存在的文件**（即使是空文件），否则 Docker 会把它当成目录挂载。首次启动前执行 `touch data.db` 即可。

---

## 6. 数据备份与迁移

### 6.1 备份

**关闭服务后**复制 `data.db` 即完成一次完整备份：

```bash
# Linux
cp data.db data.db.bak.$(date +%Y%m%d)

# Windows
copy data.db data.db.bak.%date:~0,4%%date:~5,2%%date:~8,2%
```

**热备份**（服务运行中，不锁表）：用 sqlite3 的 `.backup` 命令：

```bash
# 方式一：若系统装了 sqlite3 命令行工具
sqlite3 data.db ".backup data.db.hot"

# 方式二：用 Python（标准库自带 sqlite3 模块，无需额外安装）
python3 -c "import sqlite3; c=sqlite3.connect('data.db'); c.execute('VACUUM INTO \"data.db.hot\"'); c.close()"
```

> 说明：`.backup` / `VACUUM INTO` 会生成一份一致性快照，不阻塞正在进行的写入操作。备份完成后可再 rename 成带日期的文件名。

### 6.2 跨机器迁移

1. 停掉源机器服务
2. 复制 `data.db`（exe 模式则连同 `TodoList.exe` 一起拷）
3. 在目标机器启动即可

### 6.3 定期自动备份（Linux）

crontab：

```bash
# 每天凌晨 3 点备份到 backup/ 目录，保留最近 30 天
0 3 * * * cp /opt/todolist/data.db /opt/todolist/backup/data.db.$(date +\%Y\%m\%d) && find /opt/todolist/backup/ -mtime +30 -delete
```

---

## 7. 从旧版 JSON 迁移到 SQLite

仅当从早期的 `data.json` 版本升级到 SQLite 版本时执行一次。

### 步骤

```bash
# 1. 停服务
pkill -f "python3 app.py"  # Linux
# Windows：在任务管理器结束 python.exe，或关闭运行中的窗口

# 2. 备份原 JSON
cp data.json data.json.bak  # Linux
copy data.json data.json.bak  # Windows

# 3. 执行迁移脚本
python3 migrate_to_sqlite.py  # Linux
python migrate_to_sqlite.py  # Windows

# 输出示例：
# 源文件：./data.json（19 条事项）
# 目标库：./data.db
# 已删除旧库：./data.db
# 导入完成：19 条事项、0 条评论

# 4. 启动新版服务
python3 app.py
```

### 验证迁移成功

```bash
# 调用 API 看条数
curl -s http://localhost:8000/api/stats | python3 -c "import sys,json; d=json.load(sys.stdin); print('total:', d['total'])"
```

### 回滚方案

如新版有问题，回到 JSON 版只需：

```bash
pkill -f "python3 app.py"
git checkout app.py  # 或恢复旧版 app.py
# data.json 还在（或从 .bak 恢复）
python3 app.py
```

---

## 8. 日常使用指南

### 8.1 启动与访问

| 模式 | 启动方式 | 访问地址 |
|------|---------|---------|
| 源码运行 | `python3 app.py` | http://localhost:8000 |
| 自定义端口 | `python3 app.py 9000` | http://localhost:9000 |
| Windows exe | 双击 `TodoList.exe` | 自动打开浏览器 |
| Windows bat | 双击 `start.bat` | http://localhost:8000 |

### 8.2 界面功能

- **顶部**：搜索框（实时搜 title/owner/remark）、主题切换、导出 JSON、新建顶层事项
- **左侧看板**：总数、完成率（环形进度）、按状态/优先级统计
- **筛选区**：状态、优先级、负责人多维筛选，4 视图共享筛选
- **主区**：5 个视图 Tab 切换
  - **列表**：树形展开，点击 chevron 收起，行内编辑按钮
  - **看板**：按 status 分列卡片墙，拖卡片改状态
  - **甘特**：按 plan_end 排期的水平时间条
  - **日历**：月历视图，按状态色码区分事件
  - **归档**：独立 Tab，只读列表，搜索/状态筛选，批量恢复或永久删除
- **事项详情面板**：标题、负责人、状态、优先级、进度、计划/实际完成时间、备注、标签、依赖、评论

### 8.3 拖拽操作

- **列表视图**：行左侧有拖拽手柄，拖到其他行上方调整顺序，拖到列表空白区变成根级项，拖到子行内变成它的子项
- **看板视图**：卡片拖到其他状态列即改状态
- **禁止操作**：把父项拖到自己的子项内会弹"不能移到自己的后代下"提示（循环依赖保护）

### 8.4 评论

事项详情面板内可加评论（输入框 + 提交按钮），也可删除自己的评论。

### 8.5 主题切换

右上角 🌙 按钮切换深 / 浅主题，localStorage 持久化记住偏好。

### 8.6 数据导出

顶部"导出"按钮调 `/api/export` 拉全量 JSON 触发下载，用于跨工具导入或外部备份。

### 8.7 归档

事项积累太多影响查看时，用归档把历史事项移出主视图：

- **手动归档**：主视图任意事项行操作菜单点 📦 按钮，不限状态立即归档
- **自动归档**：服务启动时扫一次，**90 天前 + status=done** 的事项自动归档（连带子项），24 小时节流避免频繁扫表
- **归档 Tab**：点顶部"📦 归档"查看所有已归档事项
  - 搜索 / 按状态筛选
  - 勾选多条 → "恢复选中"批量回主视图
  - 单条点 ↩️ 恢复，点 🗑 永久删除（CASCADE 级联删子项）
- **归档态只读**：归档事项不能直接编辑，先恢复才能改
- **业界对照**：和 Trello/Jira/Notion Archive 设计一致——软标记不删数据，独立视图只读，支持恢复

---

## 9. 常见问题（FAQ）

### Q1：端口被占用怎么办？

```bash
# Linux 找占用进程
ss -ltnp | grep :8000
kill <PID>

# Windows 找占用进程
netstat -ano | findstr :8000
taskkill /PID <PID> /F

# 或直接换端口
python3 app.py 9000
```

### Q2：双击 exe 后浏览器没自动打开？

- 等 1-2 秒，服务启动需要时间
- 手动访问 `http://localhost:8000/`
- 检查防火墙是否拦截

### Q3：Windows exe 在其他机器上无法运行？

- 确认目标机器是 64 位 Windows 7+
- 如提示缺少 DLL，装一下 [VC++ 运行库](https://learn.microsoft.com/zh-cn/cpp/windows/latest-supported-vc-redist)
- 不要把 exe 放在 U 盘上直接运行（写入速度慢且可能锁文件），拷到本地硬盘再用

### Q4：数据丢失了怎么办？

- 检查 exe 或源码目录下是否有 `data.db`
- 用 sqlite 工具（如 [DB Browser for SQLite](https://sqlitebrowser.org/)）打开 `data.db` 看数据
- 如果有按 6.1 节生成的备份（文件名形如 `data.db.bak.20260915` 或 `data.db.hot`），改名替换 `data.db` 即可恢复

### Q5：多个用户能同时访问吗？

- 同一局域网内：可以。其他用户访问 `http://你的IP:8000/`
- **多用户并发写时数据安全**：SQLite + 全局锁串行化写入，不会丢数据
- 但**多实例同时写同一 data.db 会导致锁竞争**，建议单实例运行

### Q6：如何换端口？

启动时加参数：`python3 app.py 9000` 或 `TodoList.exe 9000`

### Q7：迁移到新机器后数据没了？

- 源码模式：忘记拷 `data.db`
- exe 模式：`TodoList.exe` 旁边必须有 `data.db`，否则首次启动会建空库

### Q8：如何彻底重置所有数据？

```bash
# 关闭服务
# 删除 data.db
rm data.db  # Linux
del data.db  # Windows
# 重启服务，自动建空库
```

### Q9：能部署到公网吗？

可以但不推荐单机直连公网。建议：
- 用 Nginx 反向代理 + 加 BasicAuth
- 启用 HTTPS（Let's Encrypt 免费证书）
- 或用 Cloudflare Tunnel 暴露内网服务

### Q10：源码修改后要重新打包 exe 吗？

是的。PyInstaller 打包后代码固定在 exe 里，改源码后必须重新跑 `build_exe.bat` 重打。但 `data.db` 数据会保留（在 exe 旁边，不进 exe）。

### Q11：自动归档把不想归档的事项归了怎么办？

到归档 Tab 找到该事项，点 ↩️ 恢复按钮即可让它回到主视图。归档是软标记不删数据，恢复后所有信息（含评论）完整保留。

### Q12：自动归档什么时候触发？能关掉吗？

启动服务时扫一次，之后每 24 小时节流跑一次（基于 `meta` 表记录的上次扫描时间）。当前版本未提供关闭开关，若想禁用可临时把 `app.py` 的 `main()` 里 `store._run_auto_archive()` 这行注释掉。

---

## 附：环境要求速查表

| 部署方式 | 前置要求 | 文件大小 | 适用人群 |
|---------|---------|---------|---------|
| Linux 源码 | Python3.8+ | ~50KB 源码 | 开发人员 / 运维 |
| Windows 源码 | Python3.8+ | ~50KB 源码 | 开发人员 |
| Windows exe | 无（零安装） | ~12MB 单 exe | 非开发人员 |
| Docker | Docker Engine | ~50MB 镜像 | 容器化用户 |
