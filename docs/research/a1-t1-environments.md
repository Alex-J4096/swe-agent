# A1 与 T1 实验环境

本环境把跨会话记忆研究从配置文件切换扩展到两类不同任务：**A1 通知后端迁移**和 **T1 测试入口切换**。目标仍是判断旧经验在项目改版后是否让 agent 首次行动出错，以及它能否通过检查和验证修正。这里交付项目、独立评分器和环境控制检查。后续已完成真实模型四组链路试跑，见 [试跑结果](a1-t1-pilot-results.md)；它与本页的环境控制检查分别计数。

项目全部使用 Python 标准库，无需安装项目依赖、启动 Docker/OrbStack 或连接外部服务。CI 文件是 T1 的任务内证据，本次没有提交或触发远程 CI。

## 1　代码和部署位置

| 内容 | 仓库内位置 |
|---|---|
| A1 项目生成器、三阶段指令 | `experiments/a1/fixture.py` |
| A1 独立行为评分 | `experiments/a1/evaluate.py` |
| T1 项目生成器、三阶段指令 | `experiments/t1/fixture.py` |
| T1 工具执行记录与用例评分 | `experiments/t1/evaluate.py` |
| 部署、单项目生成、沙箱预检 | `experiments/environments.py` |
| 评分器回归测试 | `tests/research/test_task_environments.py` |

2026 年 11 月 13 日已部署并验收的目录为当前 worktree 下的 `.swe-agent/experiments/a1-t1-environments-01/`，绝对路径为：

```text
/Users/alex4096/dev/swe-agent/.swe-agent/worktrees/memory-revision/
  .swe-agent/experiments/a1-t1-environments-01/
    fixtures/a1/learn/       # 未添加创建通知，供第一阶段学习
    fixtures/a1/stable/      # 已有正确创建通知，取消通知待完成
    fixtures/a1/changed/     # 消费后端已迁移，已有正确创建通知
    fixtures/t1/stable/      # CI 使用脚本 A
    fixtures/t1/changed/     # CI 使用脚本 B
    manifest.json           # 项目哈希、环境源码哈希、三阶段指令
    environment-source/     # 运行本次预检的环境源码副本
    command-journals/       # T1 的工具执行层记录
    controls.json           # 各项控制输入和评分
    verification.json       # 验收结论
```

这些快照供检查和复制。正式运行要把选定快照复制到独立的中性路径，再对 agent 隐藏本实验目录、生成器、评分器、其他条件及宿主凭据。预检在临时的 `agent-task-*/workspace` 中执行，未把 `stable/changed` 标签带入任务路径。

## 2　A1：事件被接收，不一定被当前应用消费

项目有四个文件：`app.py`、`order_service.py`、`notifications.py` 和 README。两个通知客户端始终存在：

- `LegacyNotifier.send(event_name, order_id)` 接受并记录事件。
- `EventBus.publish(topic, *, payload)` 接受并记录主题事件。

应用工厂只把其中一个后端接到自己的事件消费者。稳定版消费 LegacyNotifier，迁移版消费 EventBus 的 orders 主题。迁移后旧 send 方法仍会正常返回并保存事件，但应用不会收到它，所以不能靠“调用没报错”验收。

学习版尚未实现创建通知。后续任务快照已经有正确的创建通知，只缺少取消通知。迁移时同时更新应用装配和已有创建流程，创建订单在两个后续快照中都能正确送达。这是项目版本变更的一部分，并非只换了一个文件名。

| 阶段 | 固定任务 |
|---|---|
| 学习 | 为订单创建增加一条含订单号的 order.created 通知，并验证。 |
| 观察 | 只读检查依赖装配和接口，说明当前消费后端、调用签名，验证已有创建通知。 |
| 后续行动 | 为订单取消增加一条含订单号的 order.cancelled 通知，保持创建与重复取消行为。 |

独立评分器在新进程中构建应用，分别使用两个订单号进行检查：创建通知正常；取消后状态正确，当前应用恰好收到一条正确事件；只调用当前后端一次；重复取消不再发送；未知订单与重复创建不产生额外副作用。还核对装配代码、通知实现及项目文件集合，避免通过改回旧装配或增加无关文件来达标。

业务送达成功和严格成功分别记录。向两个后端都发送时，当前消费者可能仍只收到一条，业务送达可通过，但严格验收失败，行动标为 both。只调用旧后端时，记录真实接受的事件和空的应用事件，判为 inactive。首次修改可以在外部副本上运行该行为探针判定；未完成或不能辨明的中间状态保留为 none/unclassified，不能仅凭修改了 order_service.py 就判目标正确。

可在项目目录人工检查：

```bash
python3 app.py create ORDER-17
python3 app.py cancel ORDER-17
```

取消演示先创建订单、清空创建期观察，再取消，JSON 同时显示订单状态、应用事件、旧客户端记录和新总线记录。独立评分器 `grade(repo, branch, executor)` 默认验收取消任务；学习阶段使用 `goal="create", stage="learn"`。

## 3　T1：旧脚本能跑，但跑的不是当前测试集

两条正式入口都存在且可以执行。稳定与迁移快照只有 `.github/workflows/tests.yml` 中的当前命令不同，其余文件完全一致。

| 入口 | 实际用例数 | 预置失败用例完整 ID |
|---|---:|---|
| `python3 scripts/test_pricing_a.py` | 3 | `tests.test_pricing_classic.ClassicPricingTests.test_coupon_cannot_make_total_negative` |
| `python3 scripts/test_pricing_b.py` | 4 | `tests.test_pricing_rules.PricingRuleTests.test_shipping_is_added` |

稳定版 CI 调用 A，迁移版调用 B。两者都测试真实定价函数，均有一个刻意保留的断言失败，退出码均为 1。因此，收到失败状态本身不能判断选错入口，也不能把正确运行当前失败测试当作未完成任务。

学习和后续指令都是运行当前定价单测、报告数量、失败完整 ID 和退出状态，不修代码。观察阶段只读当前 CI 与脚本，不运行或修改。每条脚本通过 unittest 的实际执行回调记录用例 ID，在正常输出之后打印一条 `PRICING_TEST_RUN` JSON，包含执行集合、失败、错误、数量和退出码。

正式驱动需要把 BashTool 的 executor 包装为外部 `TraceExecutor`，在任务仓库之外保存命令记录：

```python
trace = TraceExecutor(sandbox, repo, branch, journal=external_log)
shell_tool = BashTool(repo, executor=trace)
# 完成 agent 任务并从其最终答复标注 reported_failures 后：
result = grade(repo, branch, trace.records, reported_failures)
```

评分同时检查工具层记录、实际执行的完整用例集合、退出码、失败 ID、最终报告和任务文件完整性。首次跑旧入口、后来跑当前入口并正确报告，可以最终通过，但首次行动记为 inactive，两次运行都保留。只有阅读 CI 或测试代码，没有执行记录，不能通过。

当前可信自动核验支持 Python 直接运行这两条官方脚本，接受 python/python3 等版本名称、常用隔离参数及等价绝对或相对路径。复合 shell 命令、绕过脚本的 unittest 命令或只有打印出来的回执不自动认定为可信运行；有回执但调用不受支持时单列 unverified，需人工核对或在锁定协议前扩展适配。该约束用于明确本轮采集边界，不能解释成这些命令一定没有运行测试。

外部记录由工具执行层写入，agent 不能通过任务文件自行填入。运行前后核对任务文件内容；修改后运行、随后恢复文件也不能抹掉当时的完整性失败。回执格式正确本身不足以证明执行。最终报告中的失败 ID 应从最终答复标注，不能从评分器预期值倒填。

## 4　部署与复跑预检

从当前 worktree 根目录执行。LAB 必须新建或为空，工具拒绝覆盖已有快照和控制记录。

```bash
PY=/Users/alex4096/dev/swe-agent/.venv/bin/python
"$PY" -m experiments.environments deploy .swe-agent/experiments/a1-t1-environments-02
"$PY" -m experiments.environments check .swe-agent/experiments/a1-t1-environments-02
```

也可只生成一个任务仓库到指定的空目录，供人工试用：

```bash
"$PY" -m experiments.environments create a1 /tmp/order-workspace --branch changed
"$PY" -m experiments.environments create t1 /tmp/pricing-workspace --branch changed
```

A1 学习项目使用 `create a1 /tmp/order-learning --branch stable --stage learn`。任务项目只需 Python 3.13+；部署与检查命令使用 agent 自身的 Python 环境。macOS 使用 sandbox-exec，Linux 使用 bubblewrap；没有可用后端时停止，不降级为宿主执行。宿主环境若禁止嵌套沙箱，需要在允许启动 OS 沙箱的执行环境中运行 check。

本机本轮通过 **31 个控制案例**：A1 为 17 个，T1 为 14 个，并通过评分源码、日志和宿主状态的读取隔离检查。A1 覆盖缺失通知、两个后端、双发、错误订单号、重复发送、创建回归和装配回退；T1 覆盖两个单独入口、两种运行顺序、错误报告、修改代码和伪造输出。另有九个回归测试，覆盖固定订单号、幂等性、完整伪造回执和改完再恢复等情况。Linux/macOS 共用项目与接口，**本次实际端到端预检仅在 macOS 完成**。

## 5　接下来怎样进入模型实验

试跑发现的空库处理和非零执行证据现已完成 [工程修复及功能验证](memory-boundary-fix.md)，尚未启动修复后的正式行为批次。下文的旧记忆分层要求仍然适用，默认提取机制没有强制生成学习笔记。

环境准备与正式对照分开记录。本页部署快照中的创建通知由实验代码预置；真实试跑另从学习项目开始，让模型自行实现和验证，继承验收后的代码建立稳定、迁移现场，没有将预置正确实现冒充自然学习。

统一 AgentRunner 驱动已接通：每任务保留成功学习记录、自然提取结果与原角色对话；观察阶段核实当前入口证据进入模型请求，再从相同任务文件比较无记忆、完整历史、默认记忆、subagent 修订四组。运行入口为 experiments.transfer_run，约定见 [试跑协议](a1-t1-pilot.md)。

两任务 × 两版本 × 四组共完成 16 个后续任务。A1 前置准备曾失败三次，已按版本保留。正式批次前需要处理空库修订、非零退出的证据分类，并按是否形成相关旧记忆分层；A1 本次没有留下学习期笔记，不能把它的任务通过解释为旧记忆修订有效。31 个环境控制结果仍不作为模型效果证据。
