# Agent Teams

当前版本提供一个本地文件驱动的 Agent Teams 垂直切片：Lead 负责提出计划，用户显式批准后，队友从共享任务板中原子认领 ready task 并独立运行 Agent Loop。

## 使用

```text
/team start demo-team 2
```

启动后，让 Lead 分析任务。Lead 会调用 `propose_team_plan`，返回 `plan_id`，并停在 `awaiting_approval`。

团队模式下 Lead 会先提出计划，再进行其他工具调用。计划打印完成后会询问是否继续 Agent Loop；选择返回提示符后即可执行审批命令。

计划提案完成后，终端会展开显示完整信息：`plan_id`、目标、任务描述、依赖关系、负责人提示和 workspace 模式，并询问：

```text
? Continue Agent Loop? [y] continue / [n] return to prompt
```

- 输入 `n`：结束本轮 Lead Loop，回到 `›`，然后执行 `/team approve <plan_id>`。
- 输入 `y`：继续让 Lead 运行下一轮。
- 在任意工具权限确认窗口按 `Esc`：立即中断当前循环并返回用户提示符。

Lead 在计划提案前不会调用 workspace 工具，避免用户还没有拿到 `plan_id` 就被探查命令或权限流程阻塞。

```text
/team approve <plan_id>
/team status
/team shutdown
```

审批前，团队上下文中的 `write_file`、`edit_file`、写入型 Bash 和任务管理修改会被运行时拦截。Lead 只能编排；队友只有在持有 active task 时才能修改工作区。

### Bash 权限判断

权限层和 TeamRuntime 共用只读 Bash 判断。以下命令及其安全组合默认允许，不需要额外确认：

```text
pwd、ls、find、cat、rg、grep、head、tail、wc、sort
git status、git diff、git log
```

只读命令可以使用 `|`、`&&` 和 `||` 组合，例如：

```bash
find . -type f -name "*.py" | grep -v __pycache__ | sort
pwd && ls -la
```

`2>/dev/null` 这类将错误输出重定向到系统空设备的形式也会被视为安全。写入重定向、命令替换、`find -exec`/`-delete` 以及带绝对路径的 `cd` 仍会进入权限确认。工具失败时终端会同时显示具体错误原因。

## 状态目录

```text
.swe-agent/teams/<team_id>/
├── state.json
├── tasks.json
├── inbox/<agent_id>/
├── processing/<agent_id>/
└── processed/<agent_id>/
```

任务板和收件箱使用原子 JSON 写入。任务认领使用跨进程文件锁、`claim_token` 和租约；队友进程退出后，过期 claim 会回到 `pending`，由其他空闲队友重新发现。消息消费使用 processing/processed 两阶段移动；Runtime 启动时会把遗留的 processing 文件恢复到 inbox，避免 Lead 或队友异常退出后消息永久丢失。

## Workspace 绑定

默认任务使用仓库目录。计划任务设置 `workspace_mode="worktree"` 时，第一次被认领会在以下位置创建隔离 worktree：

```text
.swe-agent/teams/<team_id>/worktrees/<task_id>/
```

worktree 路径会写回任务板，队友的文件工具和 Bash 都通过 `ToolContext.workspace_root` 指向该目录。worktree 中的改动不会自动合并；完成任务后应由 Lead 检查 diff，再决定合并或显式清理 worktree。清理接口保留为显式操作，避免无意丢失队友产出的未提交改动。

已批准的团队在再次执行 `/team start <team-id>` 时会恢复任务板、收件箱和已批准计划，并重新启动队友；未 ack 的消息和过期任务 claim 会在启动阶段恢复。

如果 CLI 在计划审批前崩溃，计划仍保存在 `state.json` 中。重启后使用相同的 team ID 启动，再执行原来的 `/team approve <plan_id>` 即可，不需要重新生成计划。

当前仍未提供进程级 sandbox、跨重启的 Agent Session 恢复，以及 worktree 自动合并。这些属于后续可靠性和交付阶段。
