# 企业知识问答 Agent（小影科技 2027 校招 · AI Agent 笔试题 3）

员工问制度问题，现在的解法是问 HR 或者自己在共享盘里翻。这个项目想解决的是翻到之后的两件事：给的答案有没有原文撑得住，以及几份制度互相打架时该听谁的。

参考实例给的判定口径是：D01 说 30 天、D02 说 15 天，两份都没写适用范围和优先级，那就把两个数值连同原文一起摆出来，请求确认，不替用户选。项目按这个口径实现。

技术选型上没引任何第三方库（Python 3.10+ 标准库），两种模型通道，11 组验收用例，37 条单元测试。

## 评审导航

| 检查项 | 入口 |
| --- | --- |
| 业务目标与验收口径 | [`docs/01-requirements.md`](docs/01-requirements.md) |
| 流程、模块、设计取舍 | [`docs/02-solution.md`](docs/02-solution.md) |
| 用例、预期与实际结果、缺陷记录 | [`docs/03-evaluation.md`](docs/03-evaluation.md) |
| AI 辅助范围、我的修改与验证 | [`docs/04-ai-usage.md`](docs/04-ai-usage.md) |
| 不足与后续迭代 | [`docs/05-retrospective.md`](docs/05-retrospective.md) |
| 一条完整决策链 | [`examples/transcripts/C2.md`](examples/transcripts/C2.md) |
| 两通道对照（mock vs 真实模型） | [`docs/03-evaluation.md` §9](docs/03-evaluation.md) · [`examples/llm/`](examples/llm) |
| 原始执行记录 | [`examples/test-results.txt`](examples/test-results.txt) |

`examples/` 里是 11 组用例的输入、实际输出和运行记录，三类场景都覆盖到了：正常（C1、C10、C11）、信息或资料不足（C3、C7）、工具失败（C6、C7）。

## 提交信息

| 项 | 内容 |
| --- | --- |
| 题号 | 项目三「企业知识问答 Agent」 |
| 仓库 | https://github.com/zhuhongjun123/xiaoying-kb-agent |
| 最终 commit | 本文件所在提交，`git rev-parse HEAD` 即为最终 SHA，与提交表单一致 |
| 评审权限 | 仓库为公开，无需额外授权 |
| 材料位置 | 代码、文档、数据、运行记录都在本仓库内，没有外部附件 |

对应的执行入口就是本文件：`## 环境与安装` 和 `## 运行` 两节给的是可直接复制的命令。

## 环境与安装

| 项 | 说明 |
| --- | --- |
| Python | 3.10 及以上。用到 `Path.is_relative_to`、`date.fromisoformat`，本地实测 3.13 |
| 第三方依赖 | 无。`requirements.txt` 里只有注释，说明为什么空 |
| 安装步骤 | 没有。克隆下来直接在仓库根目录跑，不需要虚拟环境、不需要装包、不需要 key |
| Agent 框架 | 未使用。LangGraph、ADK 都看过，不用；理由和替代方案见 `docs/02-solution.md` §6、§9 |
| 操作系统 | 开发在 Windows 11 上，脚本本身不依赖平台；产物按 LF 写，跨平台重跑不会产生 diff |

## 运行

```bash
# 单条用例：输入是文件，结论写到 stdout
python -m src.main --input examples/inputs/C2-conflict.input.json

# 直接问也行
python run.py ask "报销期限是多久？"
python run.py "报销期限是多久？"          # 无子命令时默认 ask

# 全量验收 + 单元测试
python run.py run-cases --strict
python -m unittest discover -s tests
```

`run.py` 和 `python -m src.main` 等价。前者短，后者不依赖我这个自定义入口，用哪个都行。

| 行为 | 约定 |
| --- | --- |
| 输入 | `--input <file.json>`，字段 `question / history / tool_faults / budget`，见 `examples/inputs/` |
| 输出 | 默认打印人能读的文本；`--json` 打完整 trace；`--out <file>` 只写结论 JSON |
| 退出码 | `run-cases --strict` 有断言不过返回 1。业务异常（工具不可用之类）走状态返回，不抛栈 |

三条能看出行为差异的命令：

```bash
# 冲突：两个数值都摆出来，并请求确认
python run.py ask "报销期限是多久？"

# 冲突但可解：D08 声明取代 D07，给 60 天并交代依据
python run.py ask "调休必须在多久内用完？"

# 工具失败：首轮超时 → 改写查询重试 → 还失败就明确拒答
python run.py ask "设备借用一次最长可以借多久？" --fail search_docs:"*"
```

## 一次真实输出

`examples/transcripts/C2.md` 的结论段：

```text
=== 状态：conflict ===
资料库中关于「差旅费用报销期限」存在互相冲突的规定：
- 《差旅报销管理办法》（D01，v1.2，生效 2025-03-01）规定「差旅费用报销期限」为 30 天
- 《费用报销实施细则》（D02，v2.0，生效 2026-01-15）规定「差旅费用报销期限」为 15 天

查证结论：D01、D02 有效期重叠、均未声明替代关系，且未给出可用于区分的适用范围或优先级；
适用范围表述不同（D01：公司全体在职员工；D02：总部及各分公司全体在职人员），但均覆盖本次提问人群。
因此这条问题暂时无法给出唯一答案，也不能从中擅自挑一个数值回答。

需要你确认：「差旅费用报销期限」在 D01、D02 中规定不一致，应以哪份为准？
```

底下走了四步：`search_docs` → `read_doc D01` → `read_doc D02` → `ask_user`。第二、三步不是流程表里写好的，是检索结果里出现同一条款两个数值之后才发起的。检索工具本身不返回优先级和替代关系，想查证只能读全文，所以这两步有实际作用。

## 边界

做的是制度与产品 FAQ 的可溯源问答：答案必须带 `doc_id` 和原文片段，片段要能在原文里逐字比对通过；同一条款多值时暴露冲突而不是择一；库里没有就说没有；决策循环带步数和工具次数上限。

不做通用聊天，不做「看起来像」的引用，也不让模型自己决定哪份制度更权威。后面这条在 `docs/02-solution.md` §3.3 里写了理由。换掉 `data/docs/` 里的数字，结论会跟着变，因为判定走的是文档内容，不是写死的分支。

## 决策循环

```text
        预算检查：步数 / 工具调用次数
        （超限 → status=unfinished，不输出答案）
                    │
                    ▼
        决策器：读 State（检索结果 / 已读文档 / 冲突 / 失败）
        输出一个 Action：search_docs | read_doc | ask_user | finish
                    │
                    ▼
        工具执行 → Observation（ok/error + 结构化数据）
        State 更新：合并片段、记录检索指标、全文入册
                    │
                    ▼
        确定性护栏：冲突判定与消解 → 引用可核验性校验
        引用对不上原文 / 数字无支撑 → 降级为 insufficient
```

两种决策器共用同一个 Action JSON 契约，所以换模型不用动循环：

| 通道 | 命令 | 承担什么 | 不能证明什么 |
| --- | --- | --- | --- |
| 规则策略 | `--provider mock`（默认） | 执行骨架。按 State 判定下一步，用来验证循环、工具、护栏、预算跑通 | 不是 ReAct 决策。它走的是条件判断，不能拿来证明模型的决策效果 |
| 真实模型 | `--provider llm` + `.env` | 模型自己读 State、选动作、写每步依据 | 稳定性。跑过多轮，未做次数统计 |

题目允许用预设流程作执行骨架，但明确指出只有固定条件分支不算完成 ReAct 决策。所以这里的分工是：规则策略证明工程链路，真实模型证明决策。**判断「这是不是 ReAct」，看 `examples/llm/` 那一套，不看默认通道。**

真实模型用 DeepSeek-V3 跑过 11/11。步数从 2 到 7 不等：C1 两步走完，C2 读到两份原文才追问，C11 多读一次原文，C7 连续重试检索。记录在 `examples/llm/`，C2 那次的路径与每步依据在 `docs/03-evaluation.md` §9.2。

## 目录

```text
xiaoying-kb-agent/
├── run.py                    短入口（自动处理 sys.path）
├── requirements.txt          无必需依赖，文件里写了原因
├── .env.example              只有真实模型通道需要这 3 个变量
├── data/
│   ├── docs/                 9 份合成制度/FAQ（doc_id、版本、生效日期、优先级、替代关系、主题）
│   ├── cases.jsonl           11 组验收用例（对话历史、故障注入、预算覆盖、断言）
│   └── synonyms.json         口语与正式说法对照，用于查询扩展
├── src/
│   ├── main.py               `python -m src.main --input ...` 入口
│   └── kb_agent/
│       ├── types.py          State / Action / Observation / Answer / Conflict
│       ├── retrieval.py      文档解析、中文 bigram 分词、BM25、标题加权、置信度
│       ├── tools.py          search_docs / read_doc，含可控故障注入
│       ├── guards.py         冲突判定与消解、引用核验、答案组装
│       ├── policy.py         离线规则策略（mock）
│       ├── llm.py            OpenAI 兼容通道 + 解析失败回退
│       ├── agent.py          决策循环、预算、trace 生成
│       └── cli.py            ask / run-cases / dump
├── tests/                    37 条 unittest（含 7 条「改文档看结论是否变」）
├── examples/
│   ├── inputs/C*.input.json  11 份可复跑输入
│   ├── outputs/C*.output.json 11 份实际结论（确定，可 diff）
│   ├── traces/C*.json        11 份完整 trace（步骤、参数、返回、依据、预算）
│   ├── transcripts/C*.md     11 份人类可读运行记录
│   ├── llm/                  真实模型的同构四件套
│   ├── SUMMARY.md            用例 → 验收点 → 结论
│   └── test-results.txt      unittest 与 run-cases 的原始输出
└── docs/                     01 需求 / 02 方案 / 03 评测 / 04 AI 使用 / 05 复盘
```

## 技术选型

- 语言与依赖：Python 3.10+，零第三方依赖。评审一条命令能跑，不受网络和装包影响。
- 决策循环：手写 while 循环，不用 Agent 框架。循环自己写，决策点才看得见。
- 冲突判定：写成代码（claim 比对 + 三级消解），不交给模型。审核要点要验「换文档内容结论跟着变」，交给模型这一步不可测。
- 回答与引用：规则策略走抽取式，真实模型走生成式但必须过引用校验。引用对不上原文的答案不交付。
- 检索：BM25 + 中文 bigram + 标题命中加权。9 份文档的规模上，可解释比先进值钱。

逐条对照见 `docs/02-solution.md` §6。

## 已知的问题

1. 检索是词面的，`synonyms.json` 覆盖不到的说法就中不了。问「票据有没有时效要求」很可能只捞到一句泛泛的适用范围。
2. 冲突只认「同主题 + 同 key」。两份文档用不同措辞说同一件事（比如「报销期限」和「报销时效」），系统会当成两个话题各自作答，不会报冲突。这是故意的：漏报还能被用户发现，误报会直接给出一个假的矛盾。
3. claim 抽取依赖 `键：值` 这种行。真实制度里常见「员工应在三十天内提交」这种长句，抽不到，也就没有冲突检测。
4. 消解只有三级（取代、优先级、判不了）。「新法优于旧法但旧法更具体」「上位制度优先」都没做。
5. 真实模型跑过多轮，步数在 35 到 38 之间浮动，路径也不完全一样。没做稳定性统计，样本太小。
6. mock 是规则策略，不是模型。它写进 trace 的 `provider` 字段，避免和真实模型混在一起看。
7. 单轮对话。CLI 不存会话，多轮要靠调用方把 `history` 传进来（C5 是这种形态）。

## 提交前的检查项

题号、仓库地址、commit SHA 和评审权限见开头的「提交信息」。以下是材料本身的检查结果：

- 无密钥。`.env` 在 `.gitignore` 第 3 行，仓库历史里从未出现过；`.env.example` 只有变量名和占位值。可以用 `git log --all --full-history -- .env` 核对，输出为空。
- 无真实业务数据。9 份制度、11 组用例全部合成，公司名、人名、金额均为虚构。
- 无外部附件。运行记录、trace、输出都在 `examples/`，没有依赖网盘或截图的内容。
- 命令行可复现。`## 运行` 里的命令可直接复制执行，不依赖前面的交互步骤。

还有一处需要提交人自己决定：本仓库的 commit 作者是真实姓名加一个 163 邮箱，push 到 GitHub 后公开可见。求职场景通常无妨，如果要隐藏邮箱，可以在 GitHub 设置里启用 noreply 地址后重新配置 `user.email`，注意只影响后续提交。

## 接真实模型

```bash
cp .env.example .env    # 填 OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL
python run.py --provider llm ask "报销期限是多久？"
```

OpenAI 兼容的端点都能用（DeepSeek、Qwen、vLLM），只要支持 `/chat/completions`。
