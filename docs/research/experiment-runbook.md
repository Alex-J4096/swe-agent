# 编程智能体记忆实验操作手册

当前 C1、A1、T1 的新批次使用 [统一四组协议](three-scenario-protocol.md) 与 `experiments.transfer_run`，每轮 24 次后续任务。下文第 3、4 节保留早期三组 C1 的复现步骤，不作为新四组批次入口。

在 Linux 或 macOS 上打开 Bash，从本项目根目录开始。第一天的目标是拿到两份能检查的材料：一道论文样题的证据链，以及一次“项目改版后修改超时”的完整操作记录。跑通后再扩大样本。[研究计划与六对任务](agent-memory-updates.md)

## 1. 建立实验目录

```bash
AGENT=$(pwd -P)
UV_CACHE_DIR="$AGENT/.uv-cache" uv sync --frozen
PY="$AGENT/.venv/bin/python"
LAB=$(mktemp -d "${TMPDIR:-/tmp}/agent-memory.XXXXXX")
mkdir -p "$LAB"/{logs,work,snapshots,results}
printf '实验目录：%s\n' "$LAB"
git rev-parse HEAD > "$LAB/results/agent-commit.txt"
git diff HEAD > "$LAB/results/agent-working.patch"
git status --short
case "$(uname -s)" in
  Linux) command -v bwrap ;;
  Darwin) command -v sandbox-exec ;;
esac
```

保留这个终端以复用变量。实验结束后把整个 LAB 目录复制到长期保存的位置。两个平台分别使用本机的虚拟环境；搬机器时复制数据、代码和日志，再运行 uv sync。若有未跟踪的实验代码，也一并保存。本次只做 C1 时可从第 1 节直接到第 3 节。Linux 默认沙箱依赖 bubblewrap，macOS 依赖 sandbox-exec；用一次实际读文件、执行 Python 的操作验证权限。记录系统、所选模型及执行模式，同一组成对比较使用同一平台。

## 2. 跑论文的事实更新任务

论文仓库使用单独的环境。先下载代码、锁定版本，并用数据浏览脚本找出单跳和多跳的短上下文记录。

```bash
git clone https://github.com/HUST-AI-HYZ/MemoryAgentBench "$LAB/paper"
cd "$LAB/paper"
git rev-parse HEAD > "$LAB/results/paper-commit.txt"
uv run --no-project --python 3.10 --with datasets python - <<'PY'
from datasets import load_dataset
d = load_dataset("ai-hyz/MemoryAgentBench", split="Conflict_Resolution")
for i, row in enumerate(d):
    print(i, row["metadata"]["source"], len(row["context"]), len(row["questions"]))
i = min(range(len(d)), key=lambda i: len(d[i]["context"]))
print(d[i]["questions"][0], d[i]["answers"][0])
PY
git ls-files | grep -Ei 'longcontext|fact_sh|fact_mh|6k'
```

输出长度是字符数；用数据源名称和作者配置确认 **6K**。打开数据页，取一个单跳题和一个多跳题，记下题号、旧陈述、新陈述，以及多跳题的中间事实。手算应能从这些证据得到参考答案。[数据浏览器](https://huggingface.co/datasets/ai-hyz/MemoryAgentBench/viewer/default/Conflict_Resolution)

正式复现使用作者 README 的 Quick Setup 和 long-context 入口。按检出版本的脚本找到实际调用的 Python 入口，把 agent 配置限定为表 5 的 GPT-4o，数据配置限定为 6K 的两个任务，再执行该条命令。先各跑一题，核对发送的上下文、提示、模型标识和输出，再跑所选任务的全部问题。保存配置、数据修订号、逐题预测、参考答案和官方 `substring_exact_match` 分数。表 5 的参考值为单跳 92%、多跳 28%；使用替代模型时另列模型与结果。作者 README 的 Bash 路径可能随提交变化，以刚才列出的实际文件为准。[作者运行说明](https://github.com/HUST-AI-HYZ/MemoryAgentBench) · [论文表 5](https://arxiv.org/html/2507.05257v4)

## 3. 复现早期三组 C1：本机验证与真实模型

从第 1 节的 Bash 终端执行。驱动使用本项目 Python 3.13、内置 SiliconFlow 默认模型和 CLI 相同的凭据解析顺序（先数据库中启用的默认凭据，再 `SILICONFLOW_API_KEY`）。需要配置时从项目根目录运行 `"$PY" main.py`，用 `/keys` 管理硅基流动凭据。不要把密钥写入实验目录。

```bash
cd "$AGENT"
"$PY" -m experiments.c1.run "$LAB/c1-controls" --check-only
"$PY" -m experiments.c1.run "$LAB/c1-live" --seed 4096
```

第一条命令无需模型。它在真实 shell 沙箱里尝试读取外部标记和 agent 源码，检查这两次读取都被拒绝，随后分别在稳定版、迁移版只改一个配置并启动新进程。四个期望值依次是 60、30、30、60。第二条命令先重复这些自检，再运行一次学习、两次观察和六次第三阶段任务。每次选择新的空输出目录，失败记录留在原处。随机种子只固定六组的执行顺序。

初始工作区包含 `settings.toml`、`app.toml`、`service.py`。这里沿用手册的双 TOML 设计，两份配置均含 `timeout` 和无关字段 `port`。第一阶段要求 agent 把 10 改为 30 并实际验证。两个分支随后都从这次成功经验出发，都将两份配置的超时设为 30，只有启动代码中的文件名不同。观察阶段沿用同一条只读指令。驱动在后续发送的模型请求的工具消息中查找实际加载代码，记录首次出现的事件序号。缺失时保留空值，单列这类轨迹。

每次会话均在相同的中性绝对路径下运行。驱动使用真实的 AgentRunner、文件读写、shell 和待办工具，关闭了与 C1 无关的委派、后台任务、上下文回退和技能工具。对通常需要确认的 shell 命令，统一批准 `python service.py`、`python3 service.py` 等列入驱动的本地验证命令。其他需要确认的操作会收到拒绝。单会话最多 10 轮，正常输出上限 1024 token，沿用 runner 的截断恢复机制。启动命令中的 seed、条件与标准答案保存在外部目录。所有组使用同一套工具、权限、模型和预算。

| 第三阶段 | 初始化方式 |
|---|---|
| 无记忆（none） | 从对应快照只复制三个任务文件，关闭记忆，无前序消息。 |
| 完整历史（full） | 同样关闭记忆，将学习及对应分支观察会话的原始 user、assistant（含 tool_calls）、tool（含 tool_call_id）消息按顺序送入每次主请求。驱动在本轮压缩范围外保留此前消息，并逐次核对请求前缀。 |
| 默认记忆（default） | 从同一快照额外复制自然生成的 `.swe-agent/memory`。使用 CLI 的索引、关键词选择、缓存、正文和后台提取逻辑。每个新会话有新的选择缓存。 |

只有学习与观察阶段产生的自然记忆可以进入快照。每次结束都等待提取与选择线程完成，再关闭 JSONL 和复制快照。交互式 CLI 自身使用 `shutdown(wait=False)`，而 `/experiment off` 会立即停止记录。手动试跑可用 `/experiment on <外部路径>`、`/experiment status`、`/experiment off`，或启动时指定 `--experiment-log`。省略路径会写到当前仓库内，文本回放只适用于调试。正式 C1 比较请使用上面的驱动。

首轮 macOS 结果含六次独立判分，支持离线核对下列步骤。[逐次结果与原始证据包](c1-pilot-2026-11-12.md)

## 4. 从一行结果复核到模型请求

```bash
"$PY" - "$LAB/c1-live" <<'PYTHON'
import json, sys
from pathlib import Path
lab = Path(sys.argv[1])
rows = json.loads((lab / "results.json").read_text())
for row in rows:
    print(row["branch"], row["condition"], row["first_files"], row["reads_before"],
          row["runtime"]["stdout"], row["tools"], row["agent"], row["memory"], row["log"])
row = next(r for r in rows if r["branch"] == "changed" and r["condition"] == "full")
for line in (lab / row["log"]).read_text().splitlines():
    e = json.loads(line)
    if e["event"] in {"memory_selection", "tool_result", "workspace_delta"}:
        print(e["sequence"], e["event"], e.get("tool"), e.get("arguments"), e.get("changes"))
PYTHON
```

- `manifest.json` 保存顺序、固定指令、请求的模型与预算。`learn-grade.json` 记录学习验收，`observations.json` 给出能识别当前配置入口的请求序号。`seed/`、`stable/`、`changed/` 是前三个会话完成后的任务与记忆快照，`*.history.json` 是压缩前捕获的各阶段对话。
- `results.json` 每行包含日志路径与 run_id。`delta_sequence` 指向首次实际改变任务文件的工具结束事件，`workspace_delta.changes` 是这次调用造成的差异。按工具调用逐次取样，同一条 shell 命令内的中间状态需要人工复核。对照所有差异可区分先错后改、同一调用双写、先正确后额外修改，以及始终未行动。
- `reads_before` 计首次变更前成功的 `read_file` 和能识别的只读 shell 调用（含列目录、搜索）；一次调用计一次。复杂 shell 的读取要回看原命令。`tools` 计本轮已处理并记录结果的调用，包含失败和待办调用。主任务与后台记忆分别累计输入、输出 token；没有请求记 0，有请求缺用量记 null。阅读成本和 token 成本可分别比较。
- `runtime` 保留独立 Python 进程的标准输出、错误与退出码，`success` 判定实际输出是否为 60。`loader_intact`、`unrelated_fields_intact`、`inactive_changed` 和 `final_diff` 用于检查改启动器、波及无关字段或额外修改闲置文件的情况。
- `model_request.request.messages` 是 SDK 提交的内容。完整历史组与学习、观察两份原始记录逐消息核对；默认组检查首个 system 消息的 Persistent memory index 与 Relevant memories，以及 `memory_selection`。能验证客户端是否完整提交；服务端内部处理以服务商返回的信息为限。

Linux 需要能实际启动非特权 bubblewrap 的环境，macOS 需要允许启动 sandbox-exec。在已经限制嵌套沙箱的桌面或 CI 环境，先从普通本机终端运行 `--check-only`。跨平台复跑时另建 LAB，分别保存平台、四项对照和六次运行结果。

## 5. 复核助手怎样修改了记忆

开启实验日志后，记忆存储器每次成功改变笔记或 `MEMORY.md` 索引都会记录 `memory_change`。按 `sequence` 顺序查看 `target`、`filename`、`operation` 和完整的 `before`、`after`。创建时前值为 null，删除时后值为 null，内容相同的重写不产生变更事件。写入或删除操作报错时另记 `memory_change_error`。日志包含笔记全文。

后台工作线程的 `source` 区分提取（extract）与整理（consolidate），`session_id` 标识触发会话，同一次处理中的模型请求、响应和实际文件变更共享 `maintenance_id`。先按该 ID 核对模型建议与落盘结果，再检查下一次会话的 `memory_selection` 和主模型请求，确认模型实际看到了哪个版本。仅在日志开启期间采集；旧实验文件无法据此补回历史。此事件覆盖记忆存储器的操作，手动编辑 Markdown 文件需另行记录。


## 实验性自动修订

C1 已使用 [固定版本记录](memory-revision-freeze.md) 中的提交完成对照。当前 worktree 又修复了空库和非零执行证据，见 [修复记录](memory-boundary-fix.md)；它是后续实现，旧固定标记与旧结果保持原义。

实验分支提供启动参数 `--memory-maintenance default|evidence|revise`。默认值为 `default`。三种模式都在正常会话结束后由 MemoryWorker 执行。

| 模式 | 维护行为 |
|---|---|
| default | 原来的提取与达到阈值后的批量整理。 |
| evidence | 提取时按 tool_call_id 配对工具名、参数与返回值；仍使用原提取规则和批量整理。 |
| revise | 独立记忆 subagent 搜索、读取、创建或提交完整笔记更新并读回检查。沿用正常 AgentRunner，最多八轮工具迭代。跳过原来的整篇批量整理。 |

在任务仓库中启动实验分支的程序，例如：

```bash
cd /path/to/task-repo
/Users/alex4096/dev/swe-agent/.venv/bin/python /Users/alex4096/dev/swe-agent/.swe-agent/worktrees/memory-revision/main.py --memory-maintenance revise --experiment-log .swe-agent/revision.jsonl
```

每组使用独立任务仓库和记忆副本。工具名、参数、回执和消息顺序进入维护上下文，按完整消息保留最近 12,000 字符。超出预算的旧消息及单条超长回执不进入本次修订证据。原始对话仍由正常会话保存流程维护。

subagent 有独立会话，收到本轮证据和初始候选文件名。初始候选仍来自现有关键词检索。它可通过 search_memory 扩展搜索，该工具结合正文关键词和摘要近似度排序，也可用空查询列出项目事实。可搜索和修订的范围为项目范围的 project 类型笔记。

read_memory 返回分开的摘要、正文和整个文件的版本哈希。update_memory 接收文件名、读到的版本、完整新摘要、完整新正文、active/suspect 状态、原因及证据 ID，只修改已存在的笔记。create_memory 用于新的独立项目事实，不要求旧版本，不覆盖已有文件。空库时可直接新增或无新增退出。

两个写入工具都校验本轮可用证据，至少包含一条读取或运行观察。命令返回非零结果时标为 command_failed，是失败观察而非验证成功；权限拒绝、超时及未执行不纳入此类证据。成功编辑回执可作为辅助证据。无效引用返回可用 ID；仅引用编辑操作返回 missing_observation。更新前检查整个文件版本。笔记名称、类型、范围等原有元数据保留，正文不接受 frontmatter；suspect 自动附加 Pending verification 提示。

更新使用存储层统一提交，正文和索引同步写入，索引写入失败会回滚正文。版本冲突时工具返回 stale_version，subagent 可重新读取并修改。已取消维护会拒绝提交，并在下一次模型请求前结束。笔记版本变化使旧选择缓存失效。模型需要保留无关经验和验证命令，其语义正确性与保留质量仍需实验评价。

四个记忆工具在专用权限策略中授权。该 subagent 的工具集限定为 search_memory、read_memory、create_memory、update_memory，采用后台执行方式。正常的文件编辑及命令工具仍按原权限策略运行。

JSONL 中 memory_subagent_started/finished 记录独立会话、created、updated 和 error。模型调用和工具结果的 component 为 memory，可与前台 agent 分别统计。memory_revision_decision 记录 created、committed、memory_exists、read_required、invalid_evidence、stale_version、write_error 等结果；memory_change 保留实际写入前后内容，沿用原 maintenance_id。memory_consolidation_skipped 表示原批量整理已跳过。未开启日志时，修订状态与证据仍存在笔记 frontmatter 内。

早期 C1 自动修订报告使用原 old_claim 精确子串接口，其结果保持为历史记录。当前 subagent 版本通过完整笔记和版本标识提交更新。实验汇总分别统计前台工具调用和 maintenance_tools，并将 subagent 模型用量计入 memory 成本。

2026 年 11 月 13 日完成 subagent 功能回归。首轮暴露了复合引用被过度拒绝的问题：模型同时引用成功的编辑、回读和运行证据，工具仍因编辑 ID 不在允许列表而拒绝。修正后允许成功编辑/写入作为补充证据，同时要求至少一条读取或运行观察，并向失败调用返回可用 ID。

最终版本复测原四个 claim_mismatch 场景及两个稳定场景，共六次真实模型维护。三个有明确启动代码的场景正确更新入口，一个只有运行冲突的场景写入 suspect，两个稳定场景保留了正确入口并补充本轮已验证信息。原验证偏好和任务文件保留。原始记录、源代码快照和 verification.json 位于本机 `.swe-agent/experiments/c1-subagent-smoke-02/`。初轮失败记录位于同级 c1-subagent-smoke-01。功能回归未执行后续编程任务，其结果不计入上一轮行为对照。

同日已完成固定提交 `86c1370` 的完整 C1 行为验证：36 次维护和 36 次后续任务，数据在主仓库 `.swe-agent/experiments/c1-subagent-revision-01/`。修订组首次正确、严格成功均为 12/12，补全来源组均为 9/12；修订组全流程 token 为后者 2.94 倍。四条待复核笔记在后续源码及运行证据出现后恢复为 active。报告保留了一次重复笔记和一次最终更新后未读回的问题。详见 [C1 subagent 行为结果及收尾决策](subagent-memory-revision-results.md)。本批运行期间没有修改固定代码，下一步扩展任务类型。

## A1 与 T1 环境

后续进度：现已接通 experiments.transfer_run，并完成 16 个真实模型后续任务。见 [试跑协议](a1-t1-pilot.md) 和 [试跑结果](a1-t1-pilot-results.md)。T1 使用 pilot-01 的八次结果，A1 使用 pilot-04 的八次结果；其间三次 A1 前置失败均保留。A1 学习未留下笔记，修订组空库处理未成功；T1 后续维护存在非零测试退出回执被拒绝的问题。上述两处后续已完成 [工程修复和功能验证](memory-boundary-fix.md)，未合入原试跑行为计数。正式批次尚未启动，环境控制和模型试跑分别计数。

通知后端迁移和测试入口切换的项目、独立评分器、部署与预检入口现已实现，见 [A1/T1 环境手册](a1-t1-environments.md)。从当前 worktree 执行 `python -m experiments.environments deploy LAB`，再执行 `python -m experiments.environments check LAB`。LAB 使用新的空目录，预检不调用模型。2026 年 11 月 13 日在 macOS sandbox-exec 上通过 31 个控制案例与隔离检查，五份项目快照位于当前 worktree 的 `.swe-agent/experiments/a1-t1-environments-01/fixtures/`。真实模型链路试跑情况见上段结果报告。
