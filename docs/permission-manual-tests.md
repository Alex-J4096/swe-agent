# 权限审批人工测试

运行 `uv run python main.py`。下方提示词输入给助手；确认实际工具、命令和 cwd 与用例一致，助手改写命令时需重测。

下表主要验证默认 sandbox 模式；Windows 默认没有后端，出现 `No supported sandbox backend` 是预期的拒绝执行行为。需要验证沙箱执行时使用 Linux + bubblewrap 或 macOS + sandbox-exec。

## Windows host 模式验收

运行 `uv run python main.py --execution-mode host`，或在目标目录用项目 Python 和 main.py 的绝对路径启动并加同一参数。

| 操作 | 预期 |
| --- | --- |
| `/status` | 显示 host、NO OS SANDBOX、host-powershell |
| cwd=`.`，`curl.exe --head https://example.com` | 卡片显示真实目录、host.execute、host.network、无隔离；按 y 无需回车，进入 Executing 并显示结果 |
| 再次执行完全相同命令 | 仍需审批，仅有单次批准选项 |
| 执行 `Get-Location` | 只读命令也必须审批 |
| 请求 `Start-Sleep -Seconds 30`，批准后连续按两次 Esc | 第一次提示，第二次停止并返回输入框 |
| 不加 host 参数重新启动并恢复会话 | 仍为 sandbox，不继承 host 模式或授权 |

host 模式不适用下表中的 a/t 授权复用预期；网络实际可用性仍取决于本机网络。

## 准备

让助手通过 `write_file` 创建 `approval-test/a.txt` 和 `approval-test/b.txt`，内容均为 disposable。预期不弹审批；删除用例后按需重建。

统一提示格式：

> 使用 run_bash 执行下表命令，cwd 为指定目录，不要改写命令。

交互终端应将问题、选项和输入提示分行显示；Windows／POSIX 均按单键立即响应，不需要回车。先检查按 y 后立刻出现批准结果和 Executing 状态。

## 验收用例

| # | 操作 | 选择／检查 |
| --- | --- | --- |
| 1 | cwd=`.`，`curl --head https://example.com` | 卡片完整显示命令、目录、来源、风险、网络能力；选 y，再执行仍需审批 |
| 2 | 同上选 a，再执行原命令 | 不再审批；换成 example.org 必须审批，选 n |
| 3 | `curl --head https://example.com && rm -f approval-test/a.txt` | 即使已授权原 curl，仍需审批；同时显示网络和破坏性能力；选 n 后文件仍在 |
| 4 | 原 curl 改为 cwd=`approval-test` | 必须重新审批，显示实际绝对目录；选 n |
| 5 | `rm -f approval-test/a.txt` 选 a，再删除 b.txt | 删除 b 必须重新审批；选 n，读取确认 b 仍在 |
| 6 | `python -c 'print("approval-test")'` | 显示 shell.opaque、once only；仅支持单次，重复执行仍审批 |
| 7 | 联网失败时要求用 write_file 创建 offline.txt | 选 n 后可以继续离线方案；换未授权 URL 重测，选 s／Esc 后立即停止 |
| 8 | `/permissions grants`、`/permissions revoke 1` | 显示可读授权；撤销后原操作重新审批；revoke all 清空 |
| 9 | 为 curl 选 a，记录 /status 的 Session UID，退出并 /resume | 历史保留，grants 为空，原 curl 重新审批 |
| 10 | worker 请求联网，主输入框执行 /permissions | 来源、目标、目录、风险和全部权限完整；按下表处理 |

用例 6 可在回车输入模式输入 `always` 或 `a`，应继续等待合法答案，不产生授权。

用例 7 示例：

> 使用 run_bash 执行 curl --head https://example.net；如果我拒绝联网，就使用 write_file 创建 approval-test/offline.txt，写入 offline，并告诉我结果。

## 后台审批

每项使用新的待审批请求：

| 命令 | 预期 |
| --- | --- |
| `/permissions approve <id> once` | 仅唤醒对应操作 |
| `/permissions deny <id> 请尝试离线方案` | 原操作不执行，模型收到反馈并可调整 |
| `/permissions stop <id>` | 对应运行停止 |
| 对 once only 请求执行 `/permissions approve <id> session` | 拒绝扩大授权，请求仍待处理 |

## 自动化检查

```bash
uv run pytest tests/test_permission_scopes.py tests/test_permissions.py tests/test_approval_runtime.py tests/test_persistence.py tests/test_sandbox.py -q
```

覆盖权限汇总、命令／参数／目录隔离、单次限制、审批展示、撤销、拒绝后规划、停止消息配对和恢复会话不恢复授权。沙箱集成用例可能因平台或嵌套沙箱限制跳过。
