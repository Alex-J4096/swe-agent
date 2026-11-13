# 实验分支测试

`codex/memory-revision` 用于研究仓库变化后的跨会话记忆。这里的自动化测试只维护记忆机制和实验有效性；通用 CLI、密钥管理、数据库迁移、团队、审批与终端等开发测试已从本分支移除，仍可在主分支及 Git 历史中查阅。

## 运行

在仓库根目录执行：

```bash
uv run python -m pytest -q
```

只验证一个部分：

```bash
uv run python -m pytest -q tests/memory
uv run python -m pytest -q tests/research
```

临时目录不可访问时，可加 `--basetemp=.swe-agent/pytest-temp`。`pyproject.toml` 将默认收集目录限定为 `tests`，避免收集实验生成工程中的任务测试。

所有模型响应均为确定性的模拟数据，不调用模型 API，不需要真实密钥或 Docker。实验评分测试会在临时目录内用本地 Python 执行内置的小工程；这不等于验证生产 Shell 沙箱。

## 保留范围

当前共 **87 个用例**，其中记忆模块 60 个、实验支持 27 个；数量包含参数化分支。原清理保留 78 个，随后为 C1 接入统一协议新增 7 个，再为观察执行要求新增 2 个行为回归分支。

| 文件 | 验证的问题 |
| --- | --- |
| `memory/test_store.py` | 笔记读写、索引、默认提取与整合基线 |
| `memory/test_scopes.py` | 会话隔离、旧笔记作用域与提取来源 |
| `memory/test_selection.py` | 默认检索、候选过滤、动态模型选择与缓存 |
| `memory/test_worker.py` | 后台维护使用独立快照，失效分支不能提交 |
| `memory/test_revision_tools.py` | 搜索、先读后改、版本冲突、创建、防覆盖、回滚及元数据解析 |
| `memory/test_revision_worker.py` | 修订子智能体完整调用、取消、空库创建与日志关联 |
| `memory/test_evidence.py` | 维护模式、工具结果配对、非零退出证据、拒绝未执行结果 |
| `memory/test_change_log.py` | 记忆新增、修改、删除和索引变更的 JSONL 记录 |
| `research/test_c1_experiment.py` | 原角色历史回放、首次修改与成本缺失值计算 |
| `research/test_c1_transfer.py` | C1 统一四组协议、共享准备、严格判分与主入口调度 |
| `research/test_task_environments.py` | A1/T1 评分器拒绝无效改动、伪造回执及运行时篡改 |
| `research/test_transfer_run.py` | 迁移保留已学代码、观察证据来源、最终报告及验证命令 |

`memory/support.py` 集中维护模拟客户端与证据构造；`research/conftest.py` 提供本地执行器 fixture，测试模块之间不互相导入。

`fixtures/memory_revision/` 保留真实试跑中脱敏后的 A1 空库观察与 T1 失败执行提案。它们是确定性故障回归输入，不能当成新的模型实验结果。

## 本次清理

从 346 个用例收窄到 78 个：

- 移除与本分支研究无关的 261 个通用智能体开发用例，以及 3 个通用 CLI 启动分支。
- 移除 1 个仅匹配 A1 提示词字面的测试；任务约束仍由行为评分测试验证。
- 合并 3 个重复检查：将旧记忆措辞差异并入完整修订流程，将证据错误的重试提示并入非法更新测试，将偏好持久化与默认作用域检查合并。
- 保留不同创建/更新入口的校验、失败证据、取消、回滚与跨会话隔离检查。这些边界直接影响实验处理组和结果，不能只因断言相似而删除。

此前报告中的测试数量属于当时版本，本次不改写历史验证记录。这里的通过结果说明实验基础设施和记忆边界符合断言，真实模型的行为、成本和研究结论仍需正式实验验证。
