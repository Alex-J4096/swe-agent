# 自动记忆修订固定版本 v1

2026 年 11 月 13 日固定当前实现，用于下一轮 C1 行为对照。版本标记为 `research/memory-revision-subagent-v1`，所在分支为 `codex/memory-revision`。代码、提示词、工具接口、评分器和依赖锁文件以该 Git 标记指向的提交为准。固定版本不表示已经证明行为收益。

## 固定的实现与环境

默认模式仍为 `default`，保留原来的记忆提取、检索与批量整理。`evidence` 只为提取补全工具来源；`revise` 使用独立记忆 subagent，提供搜索、读取和整篇更新工具，先读后写、校验文件版本及证据、同步正文和索引。它不再使用 `old_claim` 精确子串替换接口。

| 项目 | 固定值 |
|---|---|
| 服务商和请求模型 | SiliconFlow；`deepseek-ai/DeepSeek-V4-Flash` |
| 前台任务预算 | 每次请求正常输出上限 1,024 token，最多 10 轮工具迭代；沿用现有截断恢复与重试逻辑 |
| 修订 subagent 预算 | 每次请求输出上限 4,096 token，最多 8 轮工具迭代 |
| 维护证据窗口 | 最近完整消息包，最多 12,000 字符 |
| 采样参数 | 不额外设置 temperature 或模型 seed，沿用服务端默认；1133 只控制实验顺序 |
| 执行环境 | macOS sandbox-exec；各任务和记忆隔离；本轮不声称已验证 Linux 端到端运行 |
| Python 与依赖 | 同目录 `memory-revision-freeze-environment.json` 记录实测版本和 `uv.lock` 哈希 |

模型别名无法固定服务端内部权重版本。每批日志继续记录实际返回的模型标识、请求参数和消耗。依赖以提交中的 `uv.lock` 为准，环境记录用于核对本机实际安装状态，不保存凭据。

## 下一批固定实验

沿用 [第一轮方案](automatic-memory-revision-experiment.md) 的输入、评分和顺序，只将 `revise` 实现换为当前 subagent。两方向 × 三类现场（稳定、运行冲突、入口已查明）× 两次重复 × 三种维护模式，共 **36 次维护和 36 次后续任务**。三种模式全部重跑，不复用旧版行为结果充当本轮对照。

初始记忆继续取本机主仓库 `.swe-agent/experiments/c1-round3/settings-single/` 和 `app-single/`。应与旧批次 `.swe-agent/experiments/c1-auto-revision-01/manifest.json` 的五个初始文件哈希一致；各模式获得字节相同的副本。初始记忆预期哈希也保存在环境记录中。最多三个并行进程，顺序随机种子 1133。任务统一要求实际超时改为 90 秒并验证。

主要观察迁移后的首次误改、实际验证后返工、严格最终成功、稳定记忆误伤，以及维护和前台任务的总成本。修订接口损失按搜索、读取、证据、版本冲突、写入及预算耗尽分类。旧方案中的“断言不唯一”仅属于历史接口。预算不同是机制成本的一部分，不将本轮解释为等 token 预算比较。

本轮结束后按既定指标报告结果，停止追加 C1 措辞变体。若收益不明显，报告任务上限或未观察到收益，再扩展 A1、T1 等任务类型。若出现需要改代码的实现错误，保留该批记录，建立新版本后整批重跑，禁止在一批中途改实现或提示词。

从标记对应的干净 checkout 运行现有驱动，使用新的输出目录：

```bash
PY=/Users/alex4096/dev/swe-agent/.venv/bin/python
LAB=/Users/alex4096/dev/swe-agent/.swe-agent/experiments
"$PY" -m experiments.c1.automatic_revision "$LAB/c1-subagent-revision-check-01" --previous "$LAB/c1-round3" --check-only
"$PY" -m experiments.c1.automatic_revision "$LAB/c1-subagent-revision-01" --previous "$LAB/c1-round3"
```

固定版本时只完成检查和提交，尚未运行以上批次。实际运行前核对提交、工作区状态和初始文件哈希；运行后保留驱动生成的源码压缩包、manifest、输入、JSONL、记忆快照及独立评分。

## 已有验证及其边界

固定前全量测试：301 项通过，1 项跳过。六次真实模型维护功能回归位于本机 `.swe-agent/experiments/c1-subagent-smoke-02/`：三个明确入口场景更新正确，一个运行冲突场景标为待复核，两个稳定场景保留正确入口。这些功能回归没有后续编程任务，不能证明首次误改或返工减少。

[旧版结果](automatic-memory-revision-results.md) 保留为历史证据，其中 `claim_mismatch` 及行为计数属于旧接口。当前固定版本的行为收益仍待上述批次检验。
