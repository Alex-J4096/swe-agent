# 自动记忆修订第一轮实验结果

版本说明：本报告评估的是 `old_claim` 精确子串更新接口。后续 subagent 实现及下一轮安排见 [固定版本记录](memory-revision-freeze.md)，本报告的行为结果不适用于该新版本。

实验于 2026 年 11 月 13 日完成。三个条件共执行 36 次真实模型维护和 36 次后续任务，无程序异常或模型请求错误。**本轮未观察到自动修订相对补全来源的整体行为收益。** 初始维护阶段，四次建议因目标断言不匹配被拒绝；有明确入口代码的四次自动修订中，仅一次真正改好了笔记。

## 实验问题和方法

服务从 settings.toml 或 app.toml 读取超时，旧配置始终留在仓库。初始项目笔记来自此前两个方向的单写学习记录。每次先用真实编辑和检查工具生成现场，再让真实模型通过正常 MemoryWorker 维护笔记。实验者控制可见证据，记忆内容不人工改写。

三种模式运行同一份代码：default 使用原提取；evidence 为提取器提供工具名、参数和回执；revise 使用同样的证据格式，自动选择保留、待复核或更新旧项目事实。主比较为 revise 与 evidence。所有笔记数低于十条，没有触发默认的批量整理。

三类现场分别为入口未变且验证成功、入口迁移且误改后仍输出 30、在同一误改现场进一步读取启动代码。每类有两个迁移方向，各重复两次，每种模式每类四次。弱证据现场只暴露文件值 60 与运行值 30 的冲突，强证据现场还明确显示实际加载的文件。稳定现场提供文件值 60、运行值 60 和保持原入口的代码。

维护结束后保存笔记，清空会话和检索缓存，在同一文件状态下开启新会话，只要求“请把服务的实际请求超时调到 90 秒，并验证修改生效”。旧对话不导入。后续任务按正常流程再次执行后台维护。首次行为评价使用任务开始前的笔记快照。严格成功要求运行值 90、加载入口与无关字段完好，且没有新增无关任务文件。

模型为 deepseek-ai/DeepSeek-V4-Flash，沙箱为 macOS sandbox-exec，每会话最多十轮工具迭代，最多三个并行进程，随机种子 1133。两次重复共享原学习历史，样本属于同一任务族。

## 自动维护到底改变了什么

维护后内容先按隐藏模式名的快照核对，再关联组别。核对由本次助手完成，未进行独立人工复核。以下评价包含索引摘要和下一会话可见正文，不以模型自报状态替代内容检查。

| 现场 | 默认 | 补全来源 | 自动修订 |
|---|---|---|---|
| 稳定，四次/组 | 4 次正确入口继续可用 | 4 次正确入口继续可用 | 4 次正确入口继续可用，全部 keep |
| 运行冲突，四次/组 | 4 次仍有旧入口 | 4 次仍有旧入口 | 3 次 keep，1 次 suspect 被拒绝；4 次仍有旧入口 |
| 入口已查明，四次/组 | 1 次新旧提示并存，3 次只剩旧提示 | 2 次新旧提示并存，2 次只剩旧提示 | 1 次准确替换，3 次修订建议被拒绝，仍为旧提示 |

稳定场景未观察到有效记忆被误停用，也未发现原有验证偏好丢失。运行冲突场景的默认组和补全来源组各有一次把旧文件描述为当前生效配置并写下服务超时为 60；默认组另有一次记录了运行仍为 30，同时猜测该输出可能发生在修改前。自动修订组没有新增此类错误结果，也没有成功写入待复核提示。

自动修订的首阶段共七次 keep、一次 update 提交成功、两次 update 被拒绝、两次 suspect 被拒绝。四次拒绝均为 claim_mismatch，所有目标笔记实际都已进入候选，引用的工具回执也存在。模型两次把索引描述当作目标正文，一次丢失了正文片段中的反引号，一次返回了带 frontmatter 且格式变化的整篇文本，无法通过正文唯一子串校验。这表明目标定位接口会损失已经提出的修订。

唯一首阶段成功的更新发生在 app-located-1-revise；其同一起点的三个条件后来都首次改对。首阶段的三个失败强证据案例中，两个模型已提出正确入口的 update，另一个选择 suspect；这些建议均未落盘。因此行为结果包含修订未生效的运行，属于当前整套实现的效果。

## 下一会话的行动与结果

每行四次。首次正确要求第一次实际写入只命中当前生效配置；“写闲置配置”覆盖全程。

| 现场 | 模式 | 首次正确 | 输出 90 | 严格成功 | 写闲置配置 | 验证后返工 |
|---|---|---:|---:|---:|---:|---:|
| 入口稳定 | 默认 | 4/4 | 4/4 | 4/4 | 0/4 | 0/4 |
| 入口稳定 | 补全来源 | 4/4 | 4/4 | 4/4 | 0/4 | 0/4 |
| 入口稳定 | 自动修订 | 4/4 | 4/4 | 4/4 | 0/4 | 0/4 |
| 迁移，仅有运行冲突 | 默认 | 4/4 | 4/4 | 4/4 | 0/4 | 0/4 |
| 迁移，仅有运行冲突 | 补全来源 | 2/4 | 3/4 | 3/4 | 2/4 | 0/4 |
| 迁移，仅有运行冲突 | 自动修订 | 1/4 | 3/4 | 2/4 | 3/4 | 1/4 |
| 迁移，入口已查明 | 默认 | 4/4 | 4/4 | 4/4 | 1/4 | 0/4 |
| 迁移，入口已查明 | 补全来源 | 2/4 | 3/4 | 2/4 | 2/4 | 0/4 |
| 迁移，入口已查明 | 自动修订 | 3/4 | 3/4 | 3/4 | 1/4 | 0/4 |

合并十二次任务，默认组首次正确 12/12、严格成功 12/12；补全来源与自动修订均首次正确 8/12、严格成功 9/12。主比较中，入口已查明场景的自动修订比补全来源多一次首次正确和严格成功，但弱证据场景各少一次，合计没有改善。

七个配对起点的 evidence 与 revise 在任务开始前拥有完全相同的可见笔记，后续动作仍有波动。默认组本轮表现较好，也不足以推出旧记忆普遍有益或自动修订必然有害。

六次严格失败中，四次最终仍输出 30，两次通过修改 service.py 把入口改回旧文件来输出 90。四次输出 30 中一次到达迭代上限，另外三次模型正常结束。模型自报完成和程序输出都需要独立检查。

## 执行成本

总成本包括首次维护、后续任务及任务结束后的维护和选择调用。各模式有十二次任务，失败任务的消耗包含在总量中。仅统计服务商返回的 token，没有估算货币费用。

| 模式 | 初始维护 token | 任务 token | 后续维护 token | 总 token | 每个严格成功的总成本 |
|---|---:|---:|---:|---:|---:|
| 默认 | 9,584 | 137,077 | 27,182 | 173,843 | 14,487 |
| 补全来源 | 13,416 | 139,122 | 29,670 | 182,208 | 20,245 |
| 自动修订 | 27,190 | 126,570 | 48,448 | 202,208 | 22,468 |

自动修订总 token 比补全来源多 20,000，约 11.0%；两组严格成功数相同。当前证据没有显示新增维护成本换来了整体收益。

后续任务完成后，自动修订组另有三次 update 成功落盘。它们发生在待评分的首次行动之后；首阶段没有持久化的待复核条目，因此本轮尚未实测待复核到恢复的完整状态转换。

## 工程决策

当前实现继续保持可选。下一步优先修复模型选择修订目标的接口：把正文与描述明确拆开，为可修订的文本片段提供程序生成的标识，模型选择标识，代码仍对整个旧版本做提交前校验。需要明确片段的边界，并保留片段之外的验证命令等事实。用本轮四个被拒绝的实际响应验证目标定位，再验证稳定条目不会被错误改写。

运行冲突场景还暴露了一个独立问题：三次 keep 没有将不确定性表达为待复核；有一次模型提出 suspect 又被定位校验拒绝。目标接口修复后，应检查待复核规则能否对这种具体的运行反证作出适当反应。

本轮到此结束。已有结果优先指向机制执行链路的缺陷。首阶段真正完成修订的样本只有一个，尚不足以评价可靠修订后的跨项目行为收益。

## 证据与复现

初始代码提交为 02e7ebb8144ede15541c12fd7ca270052a896ffe，实验实现位于该提交之上的 worktree 改动。实际运行源码完整保存在 sources.zip，SHA-256 为 960e681c930356ec377aa8c3344e77eca2db756cda138e9591a5076f09efd416。运行后已核对 src、main.py 和运行入口与该快照一致。汇总脚本在运行期间新增，未改变实验逻辑。各配对输入和原笔记逐字节相同，36 条结果无协议偏离。

本机原始目录为 `.swe-agent/experiments/c1-auto-revision-01/`。manifest.json 保存顺序、模型和源文件哈希；每项的 input.json、before、after-maintenance、maintenance.jsonl、follow.jsonl、final-files.json 和 result.json 保存全链路；blind-maintenance.json、blind-judgements.json 和 memory-audit.json 保存内容核对；summary.json、results.csv 和 verification.json 保存汇总及一致性核验。模型服务未提供不可变权重修订号。

## 逐次任务评分

| 方向—现场—重复—模式 | 首次修改 | 写前读取 | 运行值 | 严格成功 |
|---|---|---:|---:|---|
| app-conflict-1-default | settings.toml | 4 | 90 | 是 |
| app-conflict-1-evidence | settings.toml | 4 | 90 | 是 |
| app-conflict-1-revise | app.toml | 4 | 90 | 是 |
| app-conflict-2-default | settings.toml | 4 | 90 | 是 |
| app-conflict-2-evidence | settings.toml | 3 | 90 | 是 |
| app-conflict-2-revise | app.toml | 4 | 90 | 否 |
| app-located-1-default | settings.toml | 4 | 90 | 是 |
| app-located-1-evidence | settings.toml | 4 | 90 | 是 |
| app-located-1-revise | settings.toml | 2 | 90 | 是 |
| app-located-2-default | settings.toml | 4 | 90 | 是 |
| app-located-2-evidence | settings.toml | 4 | 90 | 是 |
| app-located-2-revise | settings.toml | 5 | 90 | 是 |
| app-stable-1-default | app.toml | 4 | 90 | 是 |
| app-stable-1-evidence | app.toml | 2 | 90 | 是 |
| app-stable-1-revise | app.toml | 2 | 90 | 是 |
| app-stable-2-default | app.toml | 2 | 90 | 是 |
| app-stable-2-evidence | app.toml | 3 | 90 | 是 |
| app-stable-2-revise | app.toml | 4 | 90 | 是 |
| settings-conflict-1-default | app.toml | 7 | 90 | 是 |
| settings-conflict-1-evidence | settings.toml | 2 | 90 | 是 |
| settings-conflict-1-revise | app.toml | 4 | 90 | 是 |
| settings-conflict-2-default | app.toml | 4 | 90 | 是 |
| settings-conflict-2-evidence | settings.toml | 6 | 30 | 否 |
| settings-conflict-2-revise | settings.toml | 1 | 30 | 否 |
| settings-located-1-default | app.toml | 5 | 90 | 是 |
| settings-located-1-evidence | settings.toml | 1 | 30 | 否 |
| settings-located-1-revise | app.toml | 3 | 90 | 是 |
| settings-located-2-default | app.toml | 7 | 90 | 是 |
| settings-located-2-evidence | settings.toml | 4 | 90 | 否 |
| settings-located-2-revise | settings.toml | 1 | 30 | 否 |
| settings-stable-1-default | settings.toml | 4 | 90 | 是 |
| settings-stable-1-evidence | settings.toml | 4 | 90 | 是 |
| settings-stable-1-revise | settings.toml | 4 | 90 | 是 |
| settings-stable-2-default | settings.toml | 2 | 90 | 是 |
| settings-stable-2-evidence | settings.toml | 1 | 90 | 是 |
| settings-stable-2-revise | settings.toml | 1 | 90 | 是 |
