# 测试说明

在仓库根目录运行：

```bash
uv run python -m pytest -q
```

系统临时目录不可访问时，可指定专用目录：

```bash
uv run python -m pytest -q --basetemp=.swe-agent/pytest-temp
```

## 测试范围

| 文件 | 验证内容 |
| --- | --- |
| `test_keys_management.py`、`test_model_selection_errors.py` | 密钥管理、来源、错误脱敏与模型选择 |
| `test_selection_menu.py`、`test_slash_commands.py`、`test_terminal.py` | 模糊搜索、键盘选择、命令补全与单键审批 |
| `test_permission_scopes.py`、`test_permissions.py`、`test_approval_runtime.py` | 精确授权、审批、拒绝与停止 |
| `test_persistence.py`、`test_context_rewind.py` | 数据迁移、会话恢复与上下文分支 |
| `test_sandbox.py`、`test_interrupt.py` | 执行边界、环境清洗与中断 |
| `test_host_execution.py` | 显式 host 启动、每次审批、PowerShell 执行、超时取消和来源展示 |
| `test_task.py`、`test_background_task.py`、`test_teams.py` | 任务、后台事件、团队与 worktree |
| 其他 `test_*.py` | Memory、压缩、重试与终端渲染 |

## 平台与保留用例

- POSIX 终端集成测试在 Windows 跳过；OS 沙箱集成测试需要可用后端。
- 中断单元测试模拟 POSIX 进程接口，不要求 Windows 提供 `killpg`／`SIGKILL`。
- 中断回归包含两次 Esc 确认、阻塞模型取消、迟到结果隔离、重试取消、Windows 按键与输入锁协调；POSIX 终端恢复另有集成测试。
- worktree 集成测试在 `.swe-agent/test-worktrees/` 创建短路径临时仓库，完成后清理。
- 菜单交互测试需要本地事件循环通信；受限沙箱可能阻塞，应在普通终端运行。
- Windows 当前未实现 `FileLock` 的互斥后端，并发领取测试会暴露重复领取或文件替换竞争；应修复实现，不跳过或放宽断言。
- 旧 `--reset-key`、前缀授权、旧 schema 等用例用于验证移除行为与迁移兼容，不是失效测试。

密钥与模型测试使用模拟凭据和客户端，不需要真实 API Key。
