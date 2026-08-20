# SWE Agent Permission, Sandbox and Context Rewind Architecture

完整架构见 `docs/ARCHITECTURE.md`。本文聚焦 Shell sandbox、权限审批和 checkpoint 上下文回退；实现分别位于：

- `src/runtime/sandbox.py`：OS 级命令执行边界。
- `src/runtime/permissions.py`：风险和能力升级决策。
- `src/runtime/approval.py`：跨前台、Subagent 和 Team worker 的审批状态机。
- `src/runtime/agent_runner.py`：在 Tool 分发前组合策略、审批与执行上下文。
- `src/runtime/context_rewind.py`：checkpoint 快照、分支归档和安全回退。
- `src/tools/memory/rewind_context.py`：模型面对的结构化回退能力。

## 设计原则

审批用于能力升级，不用于每一条普通开发命令：

```text
Tool Call
  ↓
PermissionPolicy
  ├─ deny                → 直接拒绝
  ├─ allow               → sandbox 执行
  └─ require_approval    → ApprovalRuntime
                              ↓
                         once / turn / session / reject
                              ↓
                         带批准能力的 sandbox 执行
```

Permission、Sandbox 和 Tool 自身边界承担不同职责：

- Permission 判断调用是否需要能力升级。
- Sandbox 限制 Shell 实际能够访问的进程、文件、网络和环境。
- Tool 在执行时再次验证 workspace 路径，形成纵深防御。

用户批准不能覆盖硬拒绝，也不能关闭 workspace 边界。

## Shell Sandbox

`BashTool` 不再直接调用宿主 `subprocess.run(..., shell=True)`，而是通过 `CommandExecutor`。默认 `SandboxedCommandExecutor` 后端为：

- macOS：`/usr/bin/sandbox-exec`。
- Linux：`bubblewrap`（`bwrap`）。
- 其他或缺少后端：fail-closed，返回错误，不回退到宿主执行。

默认策略：

- workspace 可写；宿主其他位置不可写。
- 网络关闭；只有获得 `sandbox.network` 能力的调用才开启。
- 使用临时 HOME、TMP 和 XDG 目录。
- 环境变量采用白名单，只保留 PATH、locale 和终端基本信息；API key、云凭据和 SSH agent 不传入。
- 显式拒绝读取常见 SSH、AWS、Kubernetes、Docker、GPG 和云配置目录。
- timeout 会终止整个进程组。
- stdout/stderr 各自只保留末尾 12,000 字符。

Shell 字符串仍由 `/bin/sh -lc` 解释，但解释发生在 OS sandbox 内。Sandbox 允许修改 workspace，因此不能判断某次 workspace 修改是否符合用户意图；破坏性命令仍需 Permission 审批。

`network_access=true` 可让模型显式声明命令需要网络。常见网络命令和安装命令也会被策略自动识别；未声明但尝试联网的程序会被 sandbox 拒绝。

## PermissionPolicy

`PermissionPolicy.evaluate()` 返回结构化结果：

```python
PermissionDecision(
    outcome="allow" | "require_approval" | "deny",
    reason="...",
    risk="low" | "medium" | "high" | "critical",
    capability="sandbox.network" | "workspace.destructive" | "tool.<name>",
    scope_key="...",
)
```

当前规则：

| 类型 | 示例 | 结果 |
| --- | --- | --- |
| 硬拒绝 | `sudo`、`shutdown`、`mkfs`、`rm -rf /` | `deny` |
| workspace 外 cwd/path | Bash cwd、文件路径逃逸 | `deny` |
| sandbox 内普通开发命令 | 测试、lint、format、build、只读组合 | `allow` |
| 网络能力 | `curl`、`git push/pull`、依赖安装、`network_access=true` | `require_approval` |
| 破坏性 workspace 操作 | `rm`、`shred`、`git reset/clean/rebase` | `require_approval` |
| 未分类扩展 Tool | 动态注册且未列入内置分类 | `require_approval` |

内置文件 Tool 的 workspace 外路径直接拒绝，因为 Tool 执行层本来也不允许越界；没有“用户批准后访问 workspace 外”的通道。

## 审批作用域

审批结果支持：

- `approve_once`：只允许当前请求。
- `approve_turn`：当前 `AgentRunner.run()` 中相同 scope 自动允许。
- `approve_session`：写入 `Session.approved_permission_scopes`，恢复同一 Session 后继续有效。
- `reject`：拒绝，可携带反馈给模型。

scope 由 capability、规范化命令族和 cwd 生成。`uv run pytest`、`python -m pytest` 和 npm script 会保留第三个 token，避免旧的“两 token”授权把整个 `uv run` 或 `python -m` 都放开。

旧 `approved_command_prefixes` 仍可读取并兼容已有 Session，但新审批只写入结构化 scope。使用 `/permissions revoke all` 可以同时清除新旧授权。

## ApprovalRuntime

`ApprovalRuntime` 是线程安全的进程内状态机。每个请求包含 request id、tool call id、参数 preview、原因、capability、scope、source Session/agent/task，以及 `pending / resolved / cancelled` 状态。

`resolve()` 只接受 pending 请求，因此第一份响应获胜。等待者按 request id 唤醒；source 停止时可以定向取消相关请求。历史 record 有上限，pending 请求不会被清理。

前台和同步 Subagent 在 root 终端直接询问：

```text
[y] once / [t] turn / [a] session / [n] deny
```

Team worker 不读取 stdin。它创建请求后进入 `awaiting_approval`，root 终端提示用户使用：

```text
/permissions
/permissions approve <request-id> [once|session]
/permissions deny <request-id> [feedback]
```

Team shutdown 会取消对应 pending 请求并唤醒 worker。权限拒绝使 teammate task 失败，而不会被误记为完成。

## 拒绝和消息链

硬拒绝或用户拒绝后：

1. 不调用 Tool。
2. 追加匹配当前 tool call id 的 `ok=false` tool 消息。
3. 停止同一 assistant 响应中的后续调用。
4. 使用 `tools=[]`、`tool_choice="none"` 请求一次自然语言总结。
5. 返回 `AgentRunResult(stopped_by_permission=True)`。

当前仍不会为被跳过的后续 tool calls 追加 cancelled 结果，这是已知协议限制。

## 持久化与运维

`SessionStore` 将结构化 scope 保存到 SQLite `session_state.approved_scopes_json`。旧前缀保存在 `approved_prefixes_json`。审批 pending record 只存在于进程内，进程退出时全部取消，不跨重启恢复。

相关命令：

```text
/permissions                         # 列出 pending
/permissions grants                  # 查看当前 Session scope
/permissions revoke <scope|all>      # 撤销授权
```

## Checkpoint 上下文回退

`rewind_context` 是工作记忆能力，不是文件系统事务。Runner 在每次模型 step 前把完整 `session.history` 原子保存到：

```text
.swe-agent/context/<session-hash>/checkpoints/<checkpoint-id>.jsonl
```

可用 checkpoint ID 临时注入当前 SYSTEM。工具只登记 pending 请求，而且必须是该 step 唯一的 tool call。完整 tool call/result 写入后，Runner 才在安全点执行：

```text
当前 branch → 归档完整 JSONL
             ↓
读取并校验 checkpoint digest
             ↓
checkpoint history + structured carry-forward
             ↓
创建新的 branch ID，继续 Agent Loop
```

废弃分支保存在 `.swe-agent/context/<session-hash>/branches/`。checkpoint 元数据、当前 branch ID 和 generation 通过 `session_state.context_state_json` 持久化；pending 请求仅存在内存，不跨重启恢复。

carry-forward 明确区分目标、完成项、文件变化、外部副作用、发现、失败方案、下一步和 `long_term_candidates`。长期候选不会直接写 Memory，而是随内部 bridge 消息进入现有 Memory 提取和整理管道，只有被复核为持久事实后才落盘。回退后会清除当前 Session 的 Memory 精筛缓存，并使旧的后台精筛和 Memory 提取 generation 失效；正在等待 LLM 的旧提取任务会在实际写文件前再次检查 generation。

每个 root、Subagent 和 Team worker 使用自己的 Session ID、branch 和 checkpoint 链，只能回退自己的上下文。它们可能共享 workspace，因此任何文件、命令、网络、审批、Task 或 Team 事件副作用都会保留。

## 已知边界

- macOS `sandbox-exec` 是历史接口；当前仍可用，但后续应提供容器或更稳定的系统后端。
- Linux 需要安装 `bubblewrap`；缺失时 Shell fail-closed。
- Sandbox 允许写 workspace，不提供自动快照或回滚；应结合 git/worktree 保留可恢复性。
- 硬拒绝仍有字符串模式，可能误报或漏报；真正的宿主边界由 sandbox 提供。
- Permission 在 Runner 入口执行；所有面向模型的新执行路径必须复用 Runner 或显式调用同一策略。
- checkpoint 和 branch transcript 当前没有配额或自动清理；长期运行的 workspace 需要后续增加保留策略。

## 测试

回归测试覆盖 sandbox executor 注入、环境清洗、无后端 fail-closed、可用 OS 后端的 workspace 写入与路径逃逸阻止、风险分类、ApprovalRuntime 幂等响应和取消、root 命令处理、Session scope 持久化，以及拒绝后的消息链。

当测试环境本身禁止嵌套 sandbox 时，OS 后端集成测试会跳过；其余契约测试仍执行。
