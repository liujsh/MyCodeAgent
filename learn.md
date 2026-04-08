根据项目的目录结构和核心设计，MyCodeAgent 从上到下可以分为 4 个核心逻辑层：

1. 交互层 (Interaction Layer)
核心文件：chat_test_agent.py
职责：这是整个项目的入口。它使用了 rich 和 prompt_toolkit 构建了一个带有语法高亮、状态展示的漂亮命令行交互界面（CLI UI）。
运行方式：负责读取 .env 中的配置项（比如你目前用的 siliconflow 和 GLM-4.7 模型），然后实例化并启动下层的 CodeAgent。
2. 代理层 (Agent Layer)
核心文件：codeAgent.py (继承自 agent.py)
职责：Agent 的“大脑”。
它接收来自 CLI 的用户输入。
维护一个核心的 ReAct 循环或 Function Calling 循环（即：思考 -> 行动 -> 观察 -> 再思考）。
它会加载所有的 Prompt（位于 prompts 目录下），结合下层传来的上下文，统一打包发给大模型。
3. 核心引擎层 (Core Runtime)
这也是项目最核心的中间件集散地，全部在 core 目录下：

LLM 通信 (llm.py)：对各大模型厂商（OpenAI, Zhipu, DeepSeek, SiliconFlow等）的 API 进行了一致性封装，代理层只管调用这个统一接口。
上下文工程 (context_engine)：
负责维护对话窗口不被撑爆。
history_manager.py 和 summary_compressor.py 负责当对话太长（触发大模型 Token 阈值）时，自动对旧的对话记录进行“摘要压缩”。
多智能体协作 (team_engine)：实验性的 AgentTeams 引擎，支持拉起其他子 Agent（Teammate）来执行特定任务。
4. 工具与执行层 (Tools Layer)
核心文件：tools 目录
职责：Agent 的“手和脚”。
分为内置工具 (builtin) 和外部扩展 (mcp)。
内置工具包含了 25 个具体功能：如 bash (执行终端命令), read_file, edit_file, list_files 等等。
核心特色：所有工具统一了响应协议（遵守 status, data, text, stats, context, error 规范）。对于 cat 或 grep 读出的超大文本，这一层还做了截断与落盘处理（避免撑爆上下文）。
🔄 核心链路一览（数据流转）
你可以把整个大流程想象成这样一个循环：

用户 在 chat_test_agent.py 中输入： "帮我看看 README.md 里面写了啥"
控制中心 CodeAgent 接到请求，调用 context_engine 打包当前的对话历史和环境变量。
请求发给 llm.py。
模型思考后返回：“我需要调用 read_file 工具，参数是 README.md”。
控制中心 发起工具调用，去 read_file.py 执行，拿到内容。
返回的内容被附加到历史记录里中（如果太长还会被压缩）。
送回 LLM 继续思考，最后 LLM 输出最终回答给用户。
整个过程被 trace_logger.py 记录到日志中，供复盘查阅。

MyCodeAgent 核心运行机制笔记
1. 核心运行流程：ReAct 模式
项目采用了标准的 ReAct (Reasoning and Acting) 范式，但它是通过 Function Calling 协议实现的，而不是原始的文本解析。

数据流转图
输入预处理 (run 方法) -> 2. 进入循环 (_react_loop) -> 3. 上下文构建 -> 4. 模型推理 -> 5. 执行工具/返回结果
2. 关键阶段拆解
A. 准备阶段：run() 方法
这是 Agent 的主入口，它主要做了几件事：

@file 展开：在 preprocess_input 中，如果用户写了 读取 @src/main.py，它会自动读取文件内容并拼接到 prompt 里，这叫“显式上下文注入”。
状态初始化：递增 run_id，初始化 TraceLogger（记录执行轨迹）。
记录 User 消息：把处理后的用户输入存入 HistoryManager。
B. 循环阶段：_react_loop() (Agent 的心脏)
这是一个 while 循环，直到模型决定“结束”或达到最大步数。

上下文构建 (build_messages)：
它将 System Prompt (L1)、Skills/Tools 描述 (L2) 和 对话历史 (L3) 组合。
大模型推理 (llm.invoke_raw)：
将组合好的消息发给大模型（如你的 GLM-4.7）。
面试点：如果大模型返回空响应（没说话也没调工具）怎么办？代码里有一个 empty_retry_used 机制，会追加一条提示让模型重试一次。
意图解析：
模型返回的结果可能是 Content（直接回答）或者是 Tool Calls（要动用工具了）。
工具执行与观察 (_execute_tool_calls)：
如果是工具调用，Agent 会遍历所有的 tool_calls。
获取工具结果（Observation），将其作为 role: tool 的消息 自然累积 到历史记录中。
面试点：如何处理长输出？工具返回的结果如果太大，会在工具层被截断，防止撑爆模型窗口。
C. 资源管理：历史压缩 (History Compression)
在每一轮循环开始前，Agent 会检查当前的 Token 数。

阈值检查：如果超过 CONTEXT_WINDOW * COMPRESSION_THRESHOLD。
压缩策略：调用 history_manager.compact()。它不是简单的删除，而是可能调用 LLM 把前面的对话生成一个 Summary（摘要），保留核心信息，删除原始细节。
3. 面试加分项（项目亮点）
特性	实现细节	价值
消息累积模式	不使用临时的 scratchpad，而是直接利用模型的 tools 消息记录。	符合 OpenAI 最新标准，模型对上下文的理解更连贯。
可观测性 (Trace)	每一步 Thought -> Action -> Observation 都记录在 HTML 轨迹中。	方便调试（Debug）和审计，是工业级 Agent 的标配。
自愈机制	针对空响应、Tool Call 格式错误有重试逻辑。	提升了 Agent 在廉价或不稳定模型上的鲁棒性。
Skills 动态加载	根据需求从 skills 目录加载特定的处理逻辑。	模块化设计，方便扩展垂直领域的能力。
👨‍💻 建议查看的代码位置
入口逻辑：codeAgent.py:254 的 run 方法。
主循环控制：codeAgent.py:353 的 _react_loop（建议重点读这个方法的 while 块）。
工具执行逻辑：codeAgent.py:484 的 _execute_tool_calls。