# 编程智能体记忆实验操作手册

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

## 3. 在本机验证 C1，再运行真实模型

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
