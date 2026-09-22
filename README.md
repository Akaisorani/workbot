# WorkBot

[English README](README_EN.md) · [版本记录](CHANGELOG.md) · [架构](docs/architecture.md) · [安全](docs/security.md) · [Hybrid RAG](docs/rag.md)

WorkBot 是一个以 **Windows 控制节点**为中心、面向企业办公与研发场景的多节点 Agent 系统。它通过 WeLink 接收用户请求，以 CodeAgent 作为核心执行 Agent，同时管理长期会话、任务、工作流、审批、远端 Linux Worker、Memory、Workspace Knowledge 以及本地 Hybrid RAG。

当前版本：**V1.12.4**。



## V1.12.4：运行控制台 GUI 与 Agent 握手

- 新增 **WorkBot Control Center** 桌面 GUI：不仅负责安装/配置，也可以直接承载 WorkBot 运行，提供 Start / Stop / Restart、Dashboard、CodeAgent 状态与 tail、Task/Workflow 控制、Node/Peer 状态、RAG 操作、配置编辑、实时日志和 Doctor。
- GUI 通过新增 `WorkBotControlService` 复用现有 TaskManager / WorkflowManager / NodeRegistry / RAGService 与 SQLite 权威状态，不建立第二套状态机；后续 Web UI 也可以复用该 control service。
- 群聊中非 Pending Action 授权者碰巧发送 `yes / 创建 / 确认` 等自然词时改为**静默忽略控制语义**，不会回复“Pending Action 不属于你”，也不会泄露当前存在他人的 pending。
- 多 WorkBot 协作通过手动 `/agent discover` 或 GUI 的 **Discover WorkBots** 发起一次性 `hello / hello_ack` 发现与握手；启动和空闲时不再周期广播。`collaboration.peers` 是可选静态 pin；`discovery_enabled=true` 允许参与手动握手。
- `collaboration.agent_id` 可留空：配置了 `im.self_accounts` 时会自动派生稳定本机 id；`collaboration.groups` 为空时复用已配置的 WeLink groups。

GUI 推荐作为 Windows 主运行入口：

```powershell
.\scripts\run-gui.ps1
# 只打开控制台，不自动启动 WorkBot
.\scripts\run-gui.ps1 -NoAutostart
```

详见 [WorkBot Control Center GUI](docs/gui.md) 与 [多 Agent 协作](docs/multi-agent.md)。


## V1.12.3：策略热加载与多 Agent 群协作

- `ingestion_policy`、`reply_policy`、`execution_policy` 支持运行时热加载。WorkBot 监视 `config/local.json`，三个策略会先全部校验成功，再一次性替换；JSON/策略非法时继续使用上一版有效配置。
- CodeAgent 默认 `prompt_args` 更新为 `--skip-safe-check --permission-mode bypassPermissions -p {prompt}`。
- `config/workbot.example.json` 不再预置启用的 `linux-server1`，`nodes` 默认为空、`default_node=null`，避免直接复制 example 后自动尝试连接示例主机。
- 新增多 WorkBot 群协作协议：共享 `[AGENT]` 展示前缀，但身份绑定真实 WeLink sender account；只有可信 peer、明确 `to=本机 agent_id` 的 `request` 才会绕过 alias gate。`response/event` 默认只入库，不自动再次触发 Agent，避免 Bot↔Bot 无限回环。
- 提供 `/agent discover`、`/agent peers`、`/agent send <peer-id> <request>`；reasoning Agent 也可以提出 `peer_request`，但仍经过 Pending Action 确认后才发送。

推荐多 Bot 群中给每个实例配置不同的人类可读 alias，例如 `WorkBot-A`、`WorkBot-B`；机器间协作则使用结构化 peer 协议，不依赖 `[AGENT-A]` 这类前缀做身份认证。详见 [多 Agent 协作](docs/multi-agent.md)。

## V1.12.2：可用性与可观察性

- Pending Action 支持裸回复“创建 / 确认 / 好的 / yes”以及“取消 / 不创建 / no”，仅在存在 pending 且当前 sender 有确认权时绕过 strict alias gate。
- 仓库保留 `AGENTS.example.md`，运行时根据 allowlisted config 动态生成 `AGENTS.md`；token/secret/password/private key 不会进入生成上下文。
- 新增 `/agent status` 与 `/agent tail [N]`，可查看 CodeAgent `run_id`、PID、运行时长、最后输出和有界 stdout/stderr tail。CodeAgent 输出仍只是 observation，不会改变 Task/Workflow receipt state。
- 新增 `scripts/install.ps1`、`configure.ps1`、`doctor.ps1`、`generate-agents.ps1`，安装逻辑位于可复用的 `workbot.setup` Python service layer。

推荐首次安装入口：

```powershell
.\scripts\install.ps1
# 最小交互模式
.\scripts\install.ps1 -Quick
# 环境诊断
.\scripts\doctor.ps1
```

## 1. 核心能力

### WeLink 对话与可靠投递

- 支持桌面端与手机端 quote/reply 卡片统一解析；手机端 `cardContext.replyMsg` 会在 alias gate 前恢复为当前正文，`preMsg` 作为引用上下文。
- 图片优先以可信本地文件路径交给多模态 CodeAgent 直接理解；RapidOCR 作为文字提取补充，图片/OCR 失败不会阻断剩余文本处理。

- 支持群聊和私聊消息接入。
- 支持 WeLinkBot WebSocket Hook 实时接收，并以 CLI 历史查询作为恢复/对账通道。
- 支持严格 alias 模式：普通自然语言消息需以 `bot_aliases` 开头；合法 WorkBot `/slash` 命令可直接使用。
- 支持 WeLink quote/reply 消息：保留被引用消息的发送者、消息 ID 和正文，并作为当前请求的显式上下文。
- 支持图片上下文：解析 WeLink 本地图片路径，并可使用 RapidOCR 在 Windows 本地提取中英文文字；图片获取/OCR 失败不会阻塞剩余文本消息。
- 自动识别图片、文件、音频、视频等 WeLink 富媒体占位符，避免误把 `/:um_begin...` 当成命令。
- 回复通过 durable outbox 持久化，遇到发送超时/结果不确定时先验证历史消息，再安全重试。
- Markdown 代码块自动转换为 WeLink 原生代码块；表格与 ASCII 示意图采用等宽显示。
- 一条 WeLink 消息最多包含一个代码块；长回复自动拆分为多条消息且保持顺序。
- 支持 `welink-cli im send-to-group/send-to-user --file` 直接发送本地附件。

### 权限、审批与执行治理

WorkBot 将“收到消息”“回答问题”“执行任务”“产生外部副作用”分成不同权限层：

- `access_control` / `ingestion_policy`：谁的消息可以进入系统。
- `reply_policy`：谁可以获得只读分析/问答回复。
- `execution_policy`：谁可以执行代码、修改文件、创建任务/工作流等。
- `action_policy`：发送消息、修改外部系统、部署、删除等副作用是否允许、拒绝或需要审批。

审批命令：

```text
/approve approval-xxxx
/deny approval-xxxx [原因]
```

同一 operator 在允许列表中时，群聊和自己发起的私聊均可获得相同 execution 权限。

### 多节点任务与 Workflow

- Windows `office-pc` 作为控制面。
- Linux 节点通过持续 SSH relay 与 Windows 通信。
- 多目标执行自动升级为 Workflow，而不是把多个节点塞进一个任务。
- 支持 Task Steering：

```text
/steer task-... <纠正指令>   # 立即终止当前轮并在同一 Agent session 中纠正
/add task-... <后续指令>     # 当前轮完成后继续
```

- Linux 任务使用独立 CodeAgent session、workspace 和进程组，支持恢复、取消和进程回收。
- Linux→Windows RPC 默认只读，并经过 method/root allowlist。

### Conversation、Session 与 Memory

WorkBot 在 SQLite 中持久保存：

- WeLink Conversation 与消息历史
- Conversation summary
- CodeAgent session
- Task / Workflow / Approval
- durable outbound message
- 长期 Memory
- Workspace index
- RAG corpus/vector metadata

Memory RAG 默认允许**跨 conversation 检索**，适合一个项目同时存在于多个群聊和私聊的实际工作模式；原 conversation scope 仍作为来源信息保留。如果某个部署要求严格隔离，可在配置中恢复 `memory_scope=conversation`。

### Workspace Knowledge

WorkBot 可以在空闲时增量扫描配置的源码/文档目录：

- Windows 本地目录；
- Linux 节点上的 `workspace_knowledge_roots`；
- 自动发现项目；
- 对代码和文本进行分块并建立 FTS 索引；
- 维护项目级架构摘要；
- 排除 `.env`、密钥、数据库、构建目录、虚拟环境等敏感或无价值文件。

对于代码问题，Workspace/RAG 的作用主要是**定位证据**；涉及具体控制流、函数行为时，Agent 仍应打开真实源码进行确认。

## 2. Hybrid RAG

V1.11 引入本地 Hybrid RAG：

```text
Memory / Workspace / future sources
                │
                ▼
        rag_documents
                │
                ▼
          rag_chunks
          /        \
      FTS5       Vector
     lexical     sqlite-vec
          \        /
             RRF
              │
       source-aware context
              │
           CodeAgent
```

默认技术栈：

- Embedding：`Qwen/Qwen3-Embedding-0.6B`
- Dimension：512
- Vector：`sqlite-vec 0.1.9`
- Distance：cosine
- Lexical：SQLite FTS5
- Fusion：weighted Reciprocal Rank Fusion (RRF)
- Reranker：可选，默认关闭

查询和文档采用非对称 embedding：

```text
用户 query    -> prompt=query
语料 document -> prompt=none
```

### RAG 命令

```text
/rag status
/rag sync
/rag sync status
/rag sync stop
/rag embed 100
/rag embed all
/rag embed status
/rag embed stop
/rag search <query>
/rag reindex
```

`/rag sync` 将 Memory/Workspace 等业务数据同步到统一 RAG corpus；它不负责生成向量。

`/rag embed N` 表示最多为 N 个 pending chunk 生成 embedding。长任务会转为后台微批任务，每个小批次及时写入数据库，可以查看进度或停止。

`/rag reindex` 清理当前向量派生索引，使 chunk 重新进入待 embedding 状态；不会删除 Memory/Workspace 原始数据。

### CPU-only 环境

Qwen3-Embedding-0.6B 在纯 CPU 上处理大型源码库会比较慢。CPU 已接近 100% 时不建议继续增加 Python embedding worker；这通常只会增加线程竞争。

可通过：

```json
"rag": {
  "embedding": {
    "cpu_threads": 0,
    "batch_size": 16
  },
  "index": {
    "job_batch_chunks": 32
  }
}
```

控制 CPU 使用和提交粒度。`cpu_threads=0` 表示由 PyTorch 自动决定线程数。

Embedding 会优先处理高价值数据（Memory、产品手册、Wiki/Web、代码等），因此无需等待整个大型 Workspace 达到 100% coverage 才能使用 RAG。

## 3. 来源标注与 GaussDB 场景

Agent 被要求对依赖外部证据的关键结论标注来源，例如：

```text
[源码: path/file.cpp::Symbol]
[产品手册: 《文档名》 -> 章节]
[Wiki: 页面标题 | URL]
[W3/Web: 页面标题 | URL]
[文件: path -> section]
```

只能引用真正读取或检索到的来源，不得虚构文件名、函数、章节或 URL。

WorkBot 支持可配置的 GaussDB 研发上下文。对于数据库内核、优化器、执行器、Stream、分布式执行等具体实现问题，在用户没有明确指定其它数据库且该假设确实影响答案时，Agent 可以优先按 GaussDB 背景理解，并在必要时结合源码、产品手册和内部 Wiki 进行证据核实。

相关路径/入口都可以在 `agent.gaussdb_context` 中配置。

## 4. 环境要求

Windows 控制节点建议安装：

- Python 3.11+
- `codeagent`
- `welink-cli`
- OpenSSH Client
- PowerShell

RAG 可选依赖：

- `sqlite-vec==0.1.9`
- `sentence-transformers`
- `transformers`
- `Qwen/Qwen3-Embedding-0.6B`

Linux Worker 还需要：

- Python 3
- SSH
- `tmux`
- `setsid`
- `codeagent`

## 5. 快速安装

解压/克隆项目后：

```powershell
cd D:\code\workbot
.\scripts\setup-windows.ps1
```

脚本会：

1. 创建 `.venv`；
2. 安装 WorkBot 与开发依赖；
3. 如果不存在则从 `config/workbot.example.json` 创建 `config/local.json`；
4. 检查 `welink-cli`、`codeagent` 和已配置 SSH 节点。

然后编辑：

```text
config\local.json
```

至少配置自己的 WeLink account、群聊、权限策略和需要的节点。

启动：

```powershell
.\scripts\run-workbot.ps1
```

### 安装图片 OCR（可选）

如果希望 WorkBot 自动读取 WeLink 截图中的文字：

```powershell
.\scripts\setup-vision.ps1
```

默认使用 RapidOCR + ONNX Runtime，在 Windows 本地完成 OCR。未安装该可选依赖时，quote/普通文本功能仍然正常，图片解析会降级为可用路径/失败提示。

### 安装 RAG

```powershell
.\scripts\setup-rag.ps1 -DownloadModel
```

启动 WorkBot 后：

```text
/rag sync
/rag embed all
/rag status
```

## 6. 常用配置

主配置文件：

```text
config/local.json
```

它不会提交到 Git。

常用配置区域：

| 配置 | 用途 |
|---|---|
| `im` | WeLink、realtime、history、alias、quote、图片 OCR |
| `agent` | CodeAgent 命令、session、`prompt_args`、并发 |
| `nodes` | Linux 节点与能力 |
| `collaboration` | 多 WorkBot 群协作身份、手动发现/静态 peer 与允许群 |
| `access_control` | 基础访问控制 |
| `reply_policy` | 只读回答权限 |
| `execution_policy` | 执行权限 |
| `action_policy` | 外部副作用与审批 |
| `memory` | Memory 采集与检索 |
| `workspace_knowledge` | Workspace 索引范围 |
| `rag` | embedding/vector/retrieval/reranker |
| `rpc` | Linux→Windows RPC |

### 配置热加载

V1.12.3 对配置采用**白名单式热加载**，默认每 2 秒检查一次 `config/local.json`（可通过 `config_reload_interval_seconds` 调整）：

- `ingestion_policy`
- `reply_policy`
- `execution_policy`

三个策略按一个事务式批次处理：新文件先完成 JSON 解析和三套 `PolicyEngine` 校验，全部成功后才替换运行时对象；任一项非法时保留上一版有效配置。这样不会出现只更新了一半权限边界的状态。

以下配置仍需要重启 WorkBot：

- `agent.prompt_args` / CodeAgent model 参数
- `action_policy`
- bot alias、`im.self_accounts`
- node topology / `default_node`
- `collaboration` identity / discovery / peer 配置
- RAG embedding/model/vector 配置
- WeLink transport 配置

`/rag embed stop`、`/approve` 等运行态命令不属于配置热加载，它们本身即时生效。

## 7. 常用操作命令

```text
/status
/nodes
/sessions
/agent status
/agent tail [N]
/agent discover
/agent peers
/agent send <peer-id> <request>
/stop-session <agent-id>
/stop-sessions all
/memory [query]
/workspace [query]
/rag status
/rag search <query>
/approve <approval-id>
/deny <approval-id> [reason]
/steer <task-id> <instruction>
/add <task-id> <instruction>
```

## 8. 发布验证

历史版本的 `check-vXX.ps1` 已经合并为统一入口：

```powershell
.\scripts\check-release.ps1
```

它会执行：

- 发布目录卫生检查；
- Python compile check；
- 完整 pytest 回归测试。

如需检查本机 CodeAgent：

```powershell
.\scripts\check-release.ps1 -CodeAgent
```

如需检查实际 RAG 依赖/runtime：

```powershell
.\scripts\check-release.ps1 -RagRuntime
```

所有 Python regression test 仍保存在 `tests/`，不会因为整理历史脚本而删除。

## 9. 目录结构

```text
workbot/
├─ workbot/             # Windows 控制面 Python 代码
├─ linux/               # Linux worker/runtime
├─ skills/              # Agent skill / 操作规则
├─ config/              # 示例配置；local.json 不提交
├─ docs/                # 架构、安全、Memory、RAG、WeLink 等文档
├─ nodes/               # 节点说明
├─ scripts/             # 当前 setup/config/run/check 工具
├─ tests/               # 完整回归测试
├─ AGENTS.example.md    # 动态 AGENTS.md 模板；运行时 AGENTS.md 不提交
├─ README.md             # 中文
├─ README_EN.md          # English
├─ CHANGELOG.md         # 所有版本更新记录集中于此
└─ pyproject.toml
```

运行时的：

```text
state/
logs/
config/local.json
linux/state/
linux/logs/
```

以及 SQLite WAL/SHM、本地模型 cache、`.venv` 等均由 `.gitignore` 排除。

## 10. 进一步文档

- [系统架构](docs/architecture.md)
- [WorkBot Control Center GUI](docs/gui.md)
- [多 Agent 协作](docs/multi-agent.md)
- [Access Control](docs/access-control.md)
- [Action Policy](docs/action-policy.md)
- [Memory](docs/memory.md)
- [Hybrid RAG](docs/rag.md)
- [Workspace Knowledge](docs/remote-workspace.md)
- [WeLink Tools](docs/welink-tools.md)
- [WeLink Reliability](docs/im-reliability.md)
- [Workflow](docs/workflows.md)
- [Security](docs/security.md)
- [发布指南](docs/release-guide.md)
- [完整版本记录](CHANGELOG.md)

## 11. 发布注意事项

正式提交仓库前建议执行：

```powershell
.\scripts\check-release.ps1
```

并确认：

- `config/local.json` 未进入 Git；
- `state/` / `logs/` 未进入 Git；
- 不包含 token、密码、私钥和 `.env`；
- 示例配置中的账号、群 ID、节点地址均已替换为适合发布的值；
- 若项目发布到公司外部，进一步审查内部 Wiki、产品手册路径、公司专用 CLI/Skill 等内容是否允许公开。
