# SWE Agent Architecture

本文档记录当前系统中已经实现的关键架构设计。现阶段重点描述工具调用的
permission 机制。

## Permission 设计

### 目标

Permission 层位于模型产生工具调用和工具真正执行之间，主要目标是：

- 对明确禁止的 Bash 命令进行不可绕过的硬拒绝。
- 让严格受限的只读 Bash 命令自动执行，减少无意义确认。
- 对不能证明安全的命令请求用户授权。
- 支持仅当前会话有效的命令前缀授权。
- 保持 Chat Completions 工具调用消息链完整，即使工具被拒绝也向模型返回结果。
- 将 workspace 路径约束作为独立于用户确认的最终安全边界。

核心实现分布在：

- `src/runtime/agent_runner.py`：权限决策、终端确认和工具调用编排。
- `src/runtime/session.py`：会话级命令前缀授权状态。
- `src/tools/shell/bash.py`：Bash 工具执行及工作目录边界检查。
- `src/tools/toolset.py`：工具参数校验和实际分发。

### 权限决策结果

`AgentRunner._check_permission()` 返回：

```python
tuple[str | None, bool]
```

两个字段分别表示：

1. `reason`：需要确认或拒绝的原因；`None` 表示可以直接执行。
2. `hard_denied`：是否为不可由用户授权覆盖的硬拒绝。

决策语义如下：

| `reason` | `hard_denied` | 行为 |
| --- | --- | --- |
| `None` | `False` | 自动执行 |
| 非空 | `False` | 请求用户确认 |
| 非空 | `True` | 直接拒绝，不询问用户 |

### 工具调用流程

一次包含工具调用的 Chat Completions 循环按以下顺序执行：

1. 从 `response.choices[0].message.tool_calls` 取得工具调用。
2. 将 assistant 消息及其 `tool_calls` 原样加入 `session.history`。
3. 将 `tool_call.function.arguments` 从 JSON 字符串解析为字典，供权限层检查。
4. 调用 `_check_permission(tool_name, args, session)`。
5. 根据结果自动执行、询问用户或硬拒绝。
6. 允许时通过 `ToolSet.dispatch()` 校验参数并执行工具。
7. 拒绝时生成结构化失败结果，不执行工具。
8. 无论执行还是拒绝，都追加对应的 `role: "tool"` 消息：

```json
{
  "role": "tool",
  "tool_call_id": "call_xxx",
  "content": "{\"ok\": false, \"error\": \"Permission denied: ...\"}"
}
```

这保证每个 assistant `tool_call` 都有匹配的 tool 结果。模型可以理解工具未执行的
原因；但 permission 拒绝不会继续进入普通工具循环。

如果拒绝来自硬拒绝规则，或用户在确认中选择拒绝，当前 `AgentRunner.run()` 会在
追加拒绝结果后立即停止本次工具循环，不再执行同一 assistant 响应中的后续工具调用，
也不会让模型通过普通工具循环继续尝试替代方案。

停止前会额外发送一次无工具总结请求：

```python
tools=[]
tool_choice="none"
```

总结请求只允许模型生成自然语言说明，不能再次产生工具调用。总结 assistant 消息也会
追加到 `session.history`，并作为本次 `AgentRunResult.content` 返回。若总结请求失败，
系统返回固定的拒绝说明。

拒绝后的历史顺序为：

```text
user
assistant(tool_calls)
tool(permission denied)
assistant(tool-free summary)
```

本次运行以 `AgentRunResult.stopped_by_permission=True` 标记结束。该标记表示本次运行
是由硬拒绝或用户拒绝触发的，不表示模型请求或工具执行发生了系统错误；下一次用户
输入仍可创建新的 agent run。

如果同一个 assistant 消息包含多个工具调用，执行到第一个被拒绝的调用时立即停止，
后续调用不会执行，也不会逐个询问。已经成功执行的前置调用及其 tool 结果会保留在
历史中。

### Bash 权限分层

Bash 权限按固定顺序检查。顺序不可随意交换，因为前面的规则拥有更高优先级。

#### L1：硬拒绝

`DENY_LIST` 中的模式属于硬拒绝规则，例如：

```text
rm -rf /
sudo
shutdown
reboot
mkfs
dd if=
> /dev/sda
```

命中后：

- 不调用 `_ask_user()`。
- 不受 allowlist 或会话级授权影响。
- 不执行工具。
- 在终端记录拒绝日志，并向模型返回 permission denied 的 tool 结果。

因此，会话授权永远不能覆盖 `DENY_LIST`。

#### L2：严格只读 allowlist

当前自动允许的命令前缀是：

```text
pwd
ls
rg
grep
head
tail
wc
git status
git diff
git log
```

命中前缀本身还不够，命令必须同时满足：

- `cwd` 解析后位于 workspace 内。
- 参数不包含绝对路径。
- 参数路径不包含 `..`。
- 命令不包含 shell 操作符或 shell 展开字符。
- 命令可以被 `shlex.split()` 正确解析。

当前按保守策略处理以下字符：

```text
| & ; > < ` $ ( ) { } * ? [ ] ~
```

换行和回车同样会触发确认。这意味着即使基础命令在 allowlist 中，下面的调用也不会
自动执行：

```bash
git status && touch marker
rg "*.py"
ls > files.txt
```

#### L3：会话级授权

用户可以在确认时选择允许相同命令前缀在当前会话继续执行。授权记录保存在：

```python
Session.approved_command_prefixes: set[tuple[str, ...]]
```

命令通过 `shlex.split()` 解析后，取前两个 token 作为前缀；只有一个 token 时使用该
token。例如：

```text
npm install package-a  -> ("npm", "install")
git add README.md      -> ("git", "add")
pytest                 -> ("pytest",)
```

会话授权具有以下边界：

- 只存在于当前 `Session`，不会持久化到下一次启动。
- 只匹配完全相同的 token 前缀，不做字符串前缀匹配。
- 必须先通过硬拒绝检查。
- 即使命中会话授权，仍必须通过 workspace 和 shell 操作符检查。

例如用户授权了 `("npm", "install")`，后续 `npm install package-b` 可以自动执行，
但 `npm install package-b && shutdown` 仍会被安全检查拦截。

#### L4：用户确认

未被硬拒绝、未命中只读 allowlist、也未命中有效会话授权的 Bash 命令需要用户确认：

```text
[y] once     仅本次允许
[a] session  当前会话允许相同命令前缀
[n] deny     拒绝
```

空输入、未知输入、EOF 和 `Ctrl-C` 均按拒绝处理。用户完成选择后，终端会清除临时
提问行，仅保留允许或拒绝日志。

`AgentRunner.run()` 还接受可选的 `confirm_permission` 回调，供非终端界面接管普通
确认。硬拒绝发生在回调之前，因此外部回调不能覆盖 `DENY_LIST`。

### 文件工具权限

`read_file`、`write_file` 和 `edit_file` 会检查 `file_path` 是否位于 workspace 外部。
外部路径会触发 permission 确认。

用户确认不是路径沙箱的替代品。各文件工具在真正执行时还会再次验证路径必须位于
项目根目录；因此即使用户允许，工具自身也不会访问 workspace 外部路径。这属于
defense in depth。

### Bash 执行边界

`BashTool` 在执行前将 `cwd` 解析为绝对路径，并使用：

```python
workdir.is_relative_to(project_root)
```

确认其位于项目根目录内。禁止使用字符串 `startswith()` 判断目录关系，因为类似
`/tmp/project-secrets` 的兄弟目录会错误匹配 `/tmp/project` 前缀。

当前 Bash 仍通过 `shell=True` 执行。Permission 层通过硬拒绝、严格 allowlist、shell
语法检测和人工确认降低风险，但这不是操作系统级沙箱。未来如果需要更强隔离，应将
Bash 参数改为结构化的 `program + args`，并优先使用 `shell=False`。

### 扩展规则

增加新工具或权限规则时，应遵循：

1. 明确工具是自动允许、需要确认还是硬拒绝。
2. 有副作用的新工具不能依赖“未配置规则即允许”的默认行为。
3. 安全边界必须在具体工具中再次验证，不能只依赖 `AgentRunner`。
4. 会话授权只能放宽普通确认，不能覆盖硬拒绝或 workspace 边界。
5. 被拒绝的工具调用也必须生成匹配的 `role: "tool"` 结果。
6. 为硬拒绝、自动允许、shell 操作符、会话授权和路径逃逸分别添加测试。

### 测试覆盖

Permission 相关回归测试位于 `tests/test_permissions.py`，当前覆盖：

- `DENY_LIST` 硬拒绝。
- 严格只读 Bash 命令自动允许。
- shell 操作符触发确认。
- 会话级相同前缀授权。
- Bash `cwd` 兄弟目录前缀逃逸防护。
- `_ask_user()` 终端结果日志。
