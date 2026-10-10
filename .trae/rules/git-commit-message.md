---
alwaysApply: true
scene: git_message
---

# Git 提交信息生成规则

## 1. 格式要求

遵循 [Conventional Commits v1.0.0](https://www.conventionalcommits.org/zh-hans/v1.0.0/) 规范：

```
<type>(<scope>)!: <subject>

[body]

[footer(s)]
```

- **type（必填）**：提交类型
- **scope（可选）**：影响范围，如 `backend`、`frontend`、`db`
- **!（可选）**：破坏性变更标记，加在 type/scope 与冒号之间，如 `feat(api)!:`
- **subject（必填）**：简洁描述，使用中文
- **body（可选）**：当变更复杂时补充详细说明，与 header 之间用空行分隔
- **footer（可选）**：脚注区，与 body 之间用空行分隔；多个脚注之间同样用空行分隔

## 2. type 类型

规范强制要求的 type：

| type | 语义化版本影响 |
|------|---------------|
| feat | 新功能，对应 minor 版本 |
| fix | 修复 bug，对应 patch 版本 |

以下为业界常用的扩展 type（规范未强制，可选用）：

| type | 含义 |
|------|------|
| docs | 仅文档变更 |
| style | 不影响代码含义的格式变更（空格、格式化、缺少分号等） |
| refactor | 重构代码（既不是修复也不是新功能） |
| perf | 性能优化 |
| test | 添加或修改测试 |
| build | 构建系统或外部依赖变更（打包、依赖等） |
| ci | CI 配置文件和脚本变更 |
| chore | 其他不修改 src 或 test 的变更 |

## 3. subject 要求

- 使用中文，动词开头，如「新增」「修复」「优化」
- 简洁明了，不超过 50 个字符
- 不要以句号结尾
- 不要使用「，」等逗号开头

## 4. 破坏性变更

任何 type 均可用 `!` 标记破坏性变更，至少满足以下标注方式之一：

1. 在 type/scope 后追加 `!`：`feat(api)!: 移除旧版任务查询接口`
2. 在 footer 区使用 `BREAKING CHANGE: ` 开头说明破坏性内容

## 5. 示例

```
feat(backend): 新增邮件提醒功能

feat(frontend): 新增任务编辑弹窗

fix(db): 修复 mail_log 表时间字段类型错误

refactor(app): 重构邮件发送逻辑，支持合并发送

chore(build): 更新打包脚本

feat(api)!: 移除旧版任务查询接口

BREAKING CHANGE: 旧版任务查询接口已移除，请迁移至新版
```

## 6. 其他约定

- 一次提交只包含一个逻辑变更；多个独立变更请拆分为多次提交
- 若 subject 过长无法概括，使用 body 补充细节，避免超长 subject
