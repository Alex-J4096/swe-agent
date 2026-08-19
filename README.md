# Toy SWE Agent

这是一个用于学习和实验的基于终端的编程智能体项目，具备以下的功能：

- LLM模型与工具调用
- Agent 循环、子 Agent 和后台任务
- 会话持久化、上下文压缩与项目记忆
- 文件操作、Shell 权限控制和Skill加载

本项目主要面向学习用途，当前实现不保证生产环境所需的稳定性、隔离性和安全性。运行 Agent 生成的命令或文件修改前，请确认当前工作目录和权限范围符合预期。

## 基本功能

- 通过OpenAI Compatible API与模型进行对话
- 读取、写入和编辑当前workspace内的文件
- 在权限确认机制下执行Shell命令
- Todo、任务和后台任务管理
- 委派任务给子Agent
- 从`.agents/skills/`加载项目Skill
- 保存和恢复历史会话
- 自动维护项目级长期记忆
- 基于对话生成Memory，会话开始前通过本地快速检索与后台LLM精筛组装到上下文中
- 使用`/help`、`/model`、`/sessions`等斜杠命令管理会话

## 环境要求

- Python 3.13 或更高版本
- 一个可用的LLM API Key
- 推荐使用 [uv](https://docs.astral.sh/uv/) 管理依赖和运行项目

## 安装与运行

```bash
git clone <repository-url>
cd swe-agent
uv sync
```

启动程序：

```bash
uv run python main.py
```

启动后可以直接输入自然语言任务，例如：

```text
请分析当前项目的目录结构，并告诉我如何运行测试
```

## 常用命令

在交互界面中输入以下命令：

| 命令 | 说明 |
| --- | --- |
| `/help` | 查看所有斜杠命令 |
| `/status` | 查看当前模型、目录和会话信息 |
| `/model` | 先选择 Provider，再从该 Provider 提供的模型中切换 |
| `/memory [on\|off\|status]` | 开启、关闭或查看当前 Session 的 Memory |
| `/new [title]` | 创建新的持久化会话 |
| `/sessions [limit]` | 查看已保存的会话 |
| `/resume <session-id>` | 恢复指定会话 |
| `/clear` | 清空当前会话内容 |
| `/compact` | 压缩当前会话上下文 |
| `/debug` | 查看格式化后的会话历史 |

执行有副作用的 Shell 命令时，程序会请求确认；明确危险的命令会被直接拒绝。文件工具和 Shell 工具也会检查 workspace 边界。

用户提交普通 Prompt 后，程序先显示 `Preparing`，这个过程覆盖 Session 保存、SYSTEM 构建和本地 Memory 检索等准备工作。模型请求期间，交互式终端会在内容区显示 `Thinking` 动画。

非 TTY 输出（例如重定向到日志文件）不会写入终端控制字符。

Memory 默认开启。第一轮当前请求只使用本地关键词候选和上一轮已完成的精筛缓存；本轮结束后，精筛会在后台执行，结果供后续请求使用。关闭 Memory 后，当前请求不会注入 Memory，回合结束也不会执行 Memory 抽取。

## 配置说明

### API 配置

应用启动时会注册以下三个内置 Provider，默认使用 SiliconFlow：

| Provider | API 地址 | 内置默认模型 |
| --- | --- | --- |
| SiliconFlow | `https://api.siliconflow.cn/v1` | `deepseek-ai/DeepSeek-V4-Flash` |
| OpenAI | `https://api.openai.com/v1` | `gpt-5.6-luna` |
| OpenCode Go | `https://opencode.ai/zen/go/v1` | `kimi-k3` |

通过 Agent 指令配置 API Key 和模型：

1. 输入 `/model`。
2. 选择 Provider。
3. 如果该 Provider 尚未配置 API Key，按提示输入；输入内容不会回显。
4. 从该 Provider 获取的模型列表中选择模型。

配置成功后，API Key 会保存到本地数据库，Provider、模型和当前 Session 会立即更新。后续启动时会复用已保存的凭据。三个 Provider 均需提供 OpenAI-compatible 的 models 和 chat completions API。

### 数据存储

- 默认 SQLite 数据库：`~/.swe-agent/state.db`
- 可通过 `SWE_AGENT_DB_PATH` 指定数据库路径
- 当前 workspace 下的项目数据默认保存在 `.swe-agent/`，包括记忆、任务、对话压缩结果等

例如：

```bash
SWE_AGENT_DB_PATH=/tmp/swe-agent-state.db uv run python main.py
```

API Key 会保存在本地状态库中，请勿将状态库提交到 Git。

### 添加 Skill

在当前项目目录下创建 Skill 文件：

```text
.agents/skills/<skill-name>/SKILL.md
```

程序启动时会扫描该目录，并通过 `load_skill` 按需加载 Skill 内容。Skill 的具体格式可参考现有的 Skill 文件或项目架构文档。

## 运行测试

```bash
uv run pytest
```

## 项目结构

```text
main.py                  # 命令行入口和依赖装配
src/runtime/             # Agent、Session、子 Agent 运行时
src/tools/               # 文件、Shell、任务和 Skill 工具
src/infrastructure/      # Provider、数据库、记忆和会话持久化
src/utils/slash_commands # 交互式斜杠命令
src/prompts/             # Agent 系统提示词
tests/                   # 自动化测试
docs/ARCHITECTURE.md     # 详细架构说明
```

更多设计细节请阅读 [ARCHITECTURE.md](ARCHITECTURE.md) 和 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## License

MIT
