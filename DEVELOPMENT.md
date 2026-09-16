# 待办事项追踪工具 —— 开发方案文档

## 1. 项目定位

面向个人 / 小团队的轻量级待办事项追踪工具。核心理念：**单机部署、零外部依赖、跨平台运行**。

适用场景：
- 个人事项管理（学习计划、生活 TODO、项目里程碑）
- 小团队内部协作（5-10 人级别）
- 作为项目管理工具的简化替代品（对比 Jira / Trello 更轻量）

不适用场景：
- 超大规模团队（>50 人并发）
- 跨地域分布式协作（需多机同步）
- 严格权限审批流程场景

## 2. 设计目标

| 目标 | 实现方式 |
|------|---------|
| 零依赖部署 | 仅用 Python3 标准库 + SQLite（标准库自带） |
| 跨平台 | Windows / Linux / macOS 通用，无平台相关代码 |
| 单文件分发 | 用 PyInstaller 打包成单 exe，双击即用 |
| 数据可迁移 | SQLite 单文件 `data.db`，拷贝即迁移 |
| 界面美观 | 原生 HTML/CSS/JS，深浅双主题，响应式设计 |

## 3. 技术栈选型

### 后端
- **语言**：Python3（>=3.8，推荐 3.10+）
- **HTTP 服务**：`http.server.BaseHTTPRequestHandler`（标准库，无需 Flask / Django）
- **数据库**：SQLite，通过 `sqlite3` 标准库模块访问
- **多线程**：`threading.Lock` 串行化数据库访问

**选型理由**：
- 目标是单机工具，不需要服务型数据库的横向扩展能力
- SQLite 单文件、零配置、标准库内置，部署门槛最低
- 对比 MySQL / PostgreSQL：免安装、免配置、免运维
- 对比 JSON 文件存储：原生支持外键级联、并发写、SQL 索引，性能更稳

### 前端
- **框架**：无，原生 HTML5 + CSS3 + JavaScript (ES6+)
- **构建**：无打包工具，源码直发
- **样式**：原生 CSS + CSS 变量（Custom Properties）实现主题切换

**选型理由**：
- 单机工具页面交互复杂度有限，框架引入会增加包体积和构建链
- 原生 JS + CSS 变量足够支撑四视图、拖拽、评论等特性
- 浏览器原生支持，无 polyfill 顾虑

## 4. 系统架构

```
┌─────────────────────────────────────────┐
│  浏览器（前端单页应用）                  │
│  - index.html                           │
│  - style.css（深浅双主题）              │
│  - app.js（视图渲染 + 拖拽 + 评论）     │
└──────────────┬──────────────────────────┘
               │ HTTP / JSON
┌──────────────▼──────────────────────────┐
│  Python3 HTTPServer (app.py)            │
│  - REST API 路由层                       │
│  - 静态资源服务（web/）                  │
│  - TodoStore（SQLite 数据访问层）        │
└──────────────┬──────────────────────────┘
               │ sqlite3 模块（标准库）
┌──────────────▼──────────────────────────┐
│  data.db (SQLite 单文件数据库)           │
│  - items 表（事项主表）                  │
│  - comments 表（评论，外键级联删除）     │
└─────────────────────────────────────────┘
```

**层次职责**：
- `Handler` 层：HTTP 路由 + JSON 序列化 + 静态资源，无业务逻辑
- `TodoStore` 层：SQL 操作 + 数据合法性校验 + 事务管理
- `SQLite`：持久化 + 外键级联 + 索引

## 5. 数据模型

### `items` 表（事项主表）

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | TEXT PRIMARY KEY | UUID 或自定义编号 |
| `parent_id` | TEXT (FK) | 父事项 id，NULL=根级，ON DELETE CASCADE |
| `title` | TEXT NOT NULL | 标题 |
| `owner` | TEXT | 负责人 |
| `status` | TEXT | pending / in_progress / done / blocked |
| `priority` | TEXT | low / medium / high / urgent |
| `progress` | INTEGER | 0-100 |
| `plan_end` | TEXT | 计划完成日期 YYYY-MM-DD |
| `actual_end` | TEXT | 实际完成日期（status→done 时自动填） |
| `remark` | TEXT | 备注 |
| `tags` | TEXT | JSON 数组字符串 |
| `depends_on` | TEXT | 前置依赖事项 id 数组（JSON） |
| `sort_order` | INTEGER | 同级排序序号 |
| `archived_at` | TEXT | 归档时间戳，NULL=未归档（主视图可见） |
| `created_at` | TEXT | ISO 时间戳 |
| `updated_at` | TEXT | ISO 时间戳 |

### `comments` 表（评论表）

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | TEXT PRIMARY KEY | UUID |
| `item_id` | TEXT (FK) | 所属事项 id，ON DELETE CASCADE |
| `author` | TEXT | 作者，默认"匿名" |
| `text` | TEXT | 评论内容 |
| `created_at` | TEXT | ISO 时间戳 |

### `meta` 表（元数据，自动归档节流用）

| 字段 | 类型 | 说明 |
|------|------|------|
| `key` | TEXT PRIMARY KEY | 键（如 `auto_archive_last_run`） |
| `value` | TEXT | 值（ISO 时间戳） |

### 设计决策

- **树形结构用 `parent_id` 邻接表**：查询简单，删除用外键级联自动递归
- **`tags` / `depends_on` 用 JSON 字符串存**：独立建表查询范式更高，但单机工具查询简单，序列化成本可接受
- **`comments` 拆表**：避免更新评论时重写整条 item 行；且外键级联天然实现"删事项连带删评论"
- **`sort_order` 整数列**：拖拽排序时按同层 `parent_id` 分组排序

## 6. REST API 设计

### 路由表

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/api/items` | 列出全部未归档事项（含 comments） |
| GET | `/api/items/archived` | 列出全部已归档事项（归档 Tab 用） |
| GET | `/api/items/{id}` | 查询单条事项详情 |
| POST | `/api/items` | 新增事项 |
| PUT | `/api/items/{id}` | 更新事项字段 |
| DELETE | `/api/items/{id}` | 删除事项（CASCADE 级联删子项和评论） |
| POST | `/api/items/{id}/archive` | 手动归档单条（不限状态，不动子项） |
| POST | `/api/items/{id}/unarchive` | 恢复归档事项到主视图 |
| POST | `/api/items/batch-delete` | 批量永久删除归档事项（请求体 `{ids: [...]}`） |
| POST | `/api/items/{id}/comments` | 给事项添加评论 |
| DELETE | `/api/items/{id}/comments/{cid}` | 删除指定评论 |
| PUT | `/api/reorder` | 批量调整排序和父子关系 |
| GET | `/api/stats` | 统计聚合（按 status / priority 分组，仅未归档） |
| GET | `/api/export` | 导出全量 JSON（未归档 + 已归档，备份用） |
| POST | `/api/import` | 导入 JSON，按 `parent_id` 拓扑排序，ID 冲突跳过 |

### 关键约定

- **统一 JSON 请求 / 响应**，`Content-Type: application/json`
- **状态码**：200 成功、201 创建、404 不存在、400 参数错误
- **字段白名单更新**：`update_item` 只接受明确允许的字段，避免误改 id / created_at
- **自动填 `actual_end`**：`status` 改为 `done` 且未显式传 `actual_end` 时自动填当前时间

## 7. 前端功能模块

### 视图模块（Tab 切换，共享数据）
1. **列表视图**：树形展开/收起、拖拽排序、状态/优先级筛选
2. **看板视图**：按 status 分列的卡片墙，支持拖卡片改状态
3. **甘特视图**：按 plan_end 排期的水平时间轴；长标题截断 + 鼠标悬停浮动 tooltip 展示全名
4. **日历视图**：月历格子，按状态色码区分事件；格子最多 3 条，点击日期空白处弹出**当日事项弹窗**查看全部
5. **归档视图**：独立 Tab，只读列表 + 搜索/状态筛选 + 批量恢复 + 批量永久删除；与主视图数据隔离

### 交互模块
- **拖拽排序**：`mousedown` 设 `draggable`，`dragstart/dragover/drop` 实现；含循环依赖检测
- **评论 CRUD**：事项详情面板内嵌评论列表，支持新增/删除
- **搜索筛选**：实时搜索 title/owner/remark，按 status/priority 多维筛选
- **依赖多选**：编辑面板内自定义多选组件替代原生 `<select multiple>`；点击条目切换选中态（蓝底 + 勾选标记），顶部展示已选标签可单独移除；自动排除自身及后代节点防止循环依赖
- **主题切换**：`data-theme="dark"` 切换 CSS 变量集，localStorage 持久化
- **导出**：调 `/api/export` 拉取全量 JSON（未归档 + 已归档），触发浏览器下载 `todolist_export_YYYYMMDD.json`
- **导入**：选文件后解析 JSON，确认弹窗显示待导入条数，调 `/api/import` 后端按 `parent_id` 拓扑排序逐条插入，ID 冲突跳过，整事务回滚；前端 toast 反馈成功/跳过/失败条数
- **归档/恢复**：主视图行操作菜单含归档按钮（📦，不限状态）；归档 Tab 行操作含恢复（↩️）和永久删除（🗑）；归档态只读

## 8. 关键技术点

### 8.1 并发安全

SQLite 单连接被 HTTP 多线程共享时若并发写会抛 `OperationalError`。解决方案：
- `sqlite3.connect(path, check_same_thread=False)` 允许跨线程访问
- 全局 `threading.Lock` 包裹所有数据库操作（`TodoStore._tx` 上下文）
- Handler 层**不再额外加锁**（避免递归获取不可重入锁导致死锁）

### 8.2 拖拽循环依赖检测

将节点 A 拖到 B 内时，必须检查 B 是否是 A 的后代，否则形成循环链。实现：
- `_descendants(conn, item_id)` 递归遍历 `parent_id` 链收集后代 id 集合
- 若 `new_parent` 在后代集合中，跳过该条 reorder（静默拒绝，与前端 toast 联动）

### 8.3 外键级联删除

`PRAGMA foreign_keys = ON` + `ON DELETE CASCADE` 让删除父项时自动递归删除所有子项和评论，无需在 Python 中递归收集。

### 8.4 路径处理（源码 / 打包双模式）

```python
if getattr(sys, "frozen", False):
    # PyInstaller 打包后：资源在临时解压目录，数据写到 exe 同级
    BASE_DIR = os.path.dirname(sys.executable)
    WEB_DIR = os.path.join(sys._MEIPASS, "web")
else:
    # 源码运行：资源与数据都在源码目录
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    WEB_DIR = os.path.join(BASE_DIR, "web")
```

**Why**：PyInstaller `--onefile` 模式运行时把 web/ 解压到 `sys._MEIPASS` 临时目录，但 `data.db` 必须写到 exe 同级目录才能持久化（否则重启数据丢失）。

### 8.5 归档机制

主视图与归档视图数据隔离，业界对照 Trello/Jira/Notion 的 Archive 设计：

- **软标记**：`items.archived_at` 字段，NULL=未归档（主视图可见），非 NULL=已归档（归档 Tab 可见）
- **手动归档**：`POST /api/items/{id}/archive`，不限状态，只动当前项不动子项
- **恢复**：`POST /api/items/{id}/unarchive`，单条恢复到主视图
- **自动归档**：`_run_auto_archive(days=90)` 扫描 90 天前 + status=done 的事项
  - **触发时机**：服务启动时跑一次；`meta` 表记录上次扫描时间，24 小时节流避免频繁扫表
  - **树形联动**：自动归档父项时**连带归档所有未归档子项**，避免父项消失后子项悬空
  - **判定依据**：优先用 `actual_end`，缺失时回退到 `updated_at`
- **归档态只读**：归档后的事项不可编辑，需先"恢复"回主视图才能改
- **Schema 升级**：`_init_schema()` 检测旧库缺 `archived_at` 列时自动 `ALTER TABLE` 补上，无需停服迁移

## 9. 项目文件结构

```
todolist/
├── app.py                  # 后端：HTTPServer + TodoStore + 路由
├── migrate_to_sqlite.py    # 一次性迁移脚本：data.json → data.db
├── build_exe.bat           # Windows 打包脚本（PyInstaller）
├── data.db                 # SQLite 数据库（运行时自动创建）
├── data.json               # 旧版 JSON 数据（迁移前备份用）
├── data.json.bak           # 迁移前自动备份
├── web/
│   ├── index.html          # 页面结构
│   ├── style.css           # 样式（深浅主题）
│   └── app.js              # 前端逻辑（视图/拖拽/评论）
├── DEVELOPMENT.md          # 本文档（开发方案）
└── DEPLOYMENT.md           # 使用与部署手册
```

**可选文件**（运行/打包时生成）：
- `start.bat`：Windows 双击启动脚本（见 DEPLOYMENT.md 3.3 节，用户自行新建）
- `Dockerfile` / `docker-compose.yml`：容器化部署配置（见 DEPLOYMENT.md 5 节）
- `build/`、`dist/`、`*.spec`：PyInstaller 打包过程的中间产物，可安全删除

## 10. 已知限制与未来改进

### 当前限制
- **无多机同步**：单进程 + 单文件数据库，不支持多实例同时写
- **无用户体系**：owner 字段是自由文本，不做账号认证
- **无历史版本**：每次更新覆盖，无法回溯（建议定期备份 `data.db`）
- **大数据量瓶颈**：items > 1 万条时前端渲染变慢（无虚拟列表）

### 改进路线（按优先级）
1. 数据备份：定时自动 copy `data.db` 到 `backup/` 目录
2. 用户认证：加 BasicAuth 或简单 token 机制
3. 虚拟列表：列表视图用 IntersectionObserver 懒加载
4. 数据同步：可选接入 WebDAV / S3 远程备份
5. 多视图筛选联动：列表筛选状态同步到看板/甘特/日历

## 11. 编码规范

- **Python**：遵循 PEP 8，缩进 4 空格，行宽 100
- **前端 JS**：缩进 4 空格，ES6+ 语法
- **CSS**：BEM-ish 命名，CSS 变量管理主题色
- **注释**：解释 "Why"（为何这么做），而非 "What"（在做什么）
- **错误处理**：边界做校验，内部代码信任框架契约
- **避免过度工程**：不为假想需求添加配置项和扩展点
