# 编程智能体记忆实验操作手册

在 Linux 或 macOS 上打开 Bash，从本项目根目录开始。第一天的目标是拿到两份能检查的材料：一道论文样题的证据链，以及一次“项目改版后修改超时”的完整操作记录。跑通后再扩大样本。[研究计划与六对任务](agent-memory-updates.md)

## 1. 建立实验目录

```bash
AGENT=$(pwd -P)
uv sync --frozen
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

保留这个终端以复用变量。实验结束后把整个 LAB 目录复制到长期保存的位置。两个平台分别使用本机的虚拟环境；搬机器时复制数据、代码和日志，再运行 uv sync。若有未跟踪的实验代码，也一并保存。Linux 默认沙箱依赖 bubblewrap，macOS 依赖 sandbox-exec；用一次实际读文件、执行 Python 的操作验证权限。记录系统、所选模型及执行模式，同一组成对比较使用同一平台。

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

## 3. 搭建自己的第一个任务

下面的小程序只用 Python 标准库。两个配置文件同时存在，程序打印实际读取的值；由此可以独立判断 agent 的修改有没有生效。

```bash
mkdir "$LAB/work/seed"
cd "$LAB/work/seed"
printf 'timeout = 10\n' > settings.toml
cp settings.toml app.toml
cat > service.py <<'PY'
from pathlib import Path
import tomllib

config = Path(__file__).with_name("settings.toml")
print(tomllib.loads(config.read_text())["timeout"])
PY
"$PY" service.py
start_agent() {
  "$PY" "$AGENT/main.py" --experiment-log "$LAB/logs/$1.jsonl"
}
start_agent learn
```

期望初始输出为 10。此例把研究计划里的旧 YAML 换成 TOML，以免解析器差异影响路径选择。首次启动用 `/model` 选择模型，必要时按仓库 README 配置服务商。输入“请把服务的请求超时调到 30 秒，并验证修改生效”。检查 `learn.jsonl` 中的模型请求与工具结果，确认运行时返回 30。等待后台记忆提取返回，检查工作区的 `.swe-agent/memory`，记录它是否自然保存了配置经验，再退出。记忆未生成也是结果，不手工补写。

```bash
cd "$LAB/work"
for version in stable changed; do
  cp -R seed "$version"
  printf 'timeout = 30\n' > "$version/app.toml"
done
"$PY" -c 'from pathlib import Path; p=Path("changed/service.py"); p.write_text(p.read_text().replace("settings.toml","app.toml"))'
for version in stable changed; do
  (cd "$version" && "$PY" service.py)
done
```

两次输出都应为 30。分别在两个目录启动 `start_agent observe-stable` 和 `start_agent observe-changed`，每次开新会话、选择同一模型，输入“请读取服务启动代码，说明配置从哪里加载，并确认当前超时值。只需检查。”在日志中确认包含加载路径的工具结果已经进入后续模型请求。等待记忆写入完成，退出，保存两个目录快照。目录中的会话状态、分支标签和日志不应暴露给第三阶段模型；以下用相同中性路径运行所有条件。

## 4. 运行六次第三阶段评测

先在单独终端记录六个组合：稳定／迁移各配无记忆、完整历史、默认记忆。打乱顺序。每次新建同名干净工作区，例如让不同父目录下的 `repo` 通过隔离环境挂载到同一工作路径。可先在本机按下列步骤手动跑通一组。将标签传给启动函数只用于命名外部日志。

```bash
cd "$LAB"
cp -R work/stable snapshots/stable
cp -R work/changed snapshots/changed
mkdir repo
cp snapshots/changed/{service.py,settings.toml,app.toml} repo/
cd repo
start_agent changed-none
```

每次进入全新会话，使用同一模型。无记忆组执行 `/memory off`。默认记忆组在启动前把对应快照的 `.swe-agent/memory` 复制到新工作区同一相对路径（不存在就记为空），开启记忆。完整历史组关闭记忆，提供第一、二阶段按顺序排列的原始用户消息、助手回复和工具调用与结果。**当前 CLI 尚无跨会话完整历史导入功能**。试跑时可把去重后的原始对话作为有边界的文本随任务提交，并标记“文本回放”；正式评测需由驱动脚本按原消息角色重建历史，核查发送请求未被截断。三组均使用同一句指令“请把服务的请求超时调到 60 秒，并验证修改生效”。

每次结束，在 agent 外部运行下列检查。保存工作区后，再创建新的空 repo 运行下一个条件。正式比较时隔离其他组的快照、日志和会话数据库，避免从工具或历史文件读到旧轨迹。

```bash
"$PY" service.py                       # 运行时验收，期望 60
cat settings.toml app.toml             # 哪些文件最终发生变化
cd "$LAB"
mv repo results/changed-none-workspace # 每次替换为本次唯一标签
```

## 5. 从日志填出第一行结果

```bash
"$PY" - "$LAB/logs/changed-none.jsonl" <<'PY'
import json, sys
from pathlib import Path
for line in Path(sys.argv[1]).read_text().splitlines():
    e = json.loads(line)
    if e["event"] == "tool_result":
        print(e["sequence"], e["tool"], e["arguments"], e["output"])
    elif e["event"] == "model_response":
        print(e["sequence"], e["component"], e["response"].get("usage"))
PY
```

在 results 中建表，每次运行一行：任务、版本、记忆条件、模型、run_id、首次成功写入的文件、写入前读取次数、最终运行值、主任务 token、后台记忆 token、日志路径。查看实际模型请求以核对哪些信息出现在索引、正文或工具结果中。两个文件同时被改标记为“同时修改”，没有修改标记为“未行动”，接口未返回用量记为“未知”。文件最终状态和最后一次测试无法还原首次修改，需结合日志顺序判定。

第一对任务的完成条件是六行结果都有独立运行值，另一位成员能从任一行回到日志，复核第一次改在哪里以及修改前看到了什么。完成后按研究计划加入另外五对任务，先冻结指令、模型和判分规则，再跑 36 次正式评测；早期调试运行单独保存。
