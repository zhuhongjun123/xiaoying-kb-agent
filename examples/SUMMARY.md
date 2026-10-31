# examples：验证材料索引

三样东西：输入、实际输出、运行记录。全部由 `python run.py run-cases` 生成，重跑结果一致。

## 目录

```text
examples/
├── inputs/C*-*.input.json      可复跑输入（question / history / tool_faults / budget）
├── outputs/C*-*.output.json    实际结论，不含逐步 trace，可 diff
├── traces/C*.json              完整 trace（mock）：动作、参数、返回摘要、决策依据、预算
├── transcripts/C*.md           人类可读运行记录（mock）
├── llm/                        真实模型的同构四件套
│   └── inputs/ outputs/ traces/ transcripts/
├── SUMMARY.md                  本文件
└── test-results.txt            原始控制台记录（unittest -v 加 run-cases）
```

## 怎么跑

```bash
# 单条，输入是文件
python -m src.main --input examples/inputs/C2-conflict.input.json

# 全量，重新生成上面所有文件
python run.py run-cases --strict

# 真实模型通道，同样的用例写到 examples/llm/
python run.py --provider llm run-cases --out examples/llm
```

两条通道用同一批用例、同一套断言，各 11/11 通过。对照分析在 `docs/03-evaluation.md` §9。

确定性：`git clone` 到新目录后跑 `run-cases` 和 `unittest`，`git status` 是空的。这些文件是跑出来的，不是手写快照。真实模型那边不确定，多轮实测 35–38 步、25–28 次工具调用，路径每轮可能不同。

## 用例索引

| 用例 | 覆盖的验收点 | 输入 | 实测状态 | 结论要点 |
| --- | --- | --- | --- | --- |
| C1 | 明确答案 | 「设备借用一次最长可以借多久？」 | answered | 引用 D03 §2，答案含 7 个工作日 |
| C2 | 文档冲突（题目参考实例） | 「报销期限是多久？」 | conflict | 30 天与 15 天并列，说明无替代、无优先级、适用范围不构成区分，请求确认 |
| C3 | 资料外问题 | 「公司年会的抽奖规则是什么？中奖率多少？」 | out_of_scope | 两轮检索内容词命中为 0，拒答，并列出库覆盖的主题 |
| C4 | 改写后的相似问题 | 「出差回来报销单能拖多久才交？」 | conflict | 结论与 C2 一致 |
| C5 | 上下文追问 | 「那续借呢？」加 history | answered | 检索词还原成「设备借用可以借多久？ 那续借呢？」，命中 D03 §2 |
| C6 | 工具失败，可恢复 | 「病假证明材料要几天内提交？」加首轮超时 | answered | 改写查询重试成功，引用 D05 |
| C7 | 工具失败，不可恢复 | 同一问题，检索全程超时 | insufficient | 连续失败后拒答，不凭常识作答 |
| C8 | 冲突可消解 | 「调休必须在多久内用完？」 | answered | 60 天，依据「D08 声明取代 D07」，并引用修订说明原文 |
| C9 | 预算上限 | 「采购金额 5 万元需要谁审批？」加 `max_steps=1` | unfinished | 明确未完成，说明已检索到 D09 |
| C10 | 明确答案，产品侧 | 「云剪单次导出最长可以多长？4K 要什么版本？」 | answered | 引用 D04 §1，含 30 分钟与专业版 |
| C11 | 检索改进 | 「出差住宿一晚最多能报多少钱？」 | answered | 标题加权后命中 D10「住宿上限」，不再被 D01 泛段落压过 |

## 一份 trace 怎么读

以 `traces/C2.json` 为例，`steps[]` 里每步有四个字段：

- `action` 和 `args`：选了什么动作、给了什么参数，可以直接复现这次工具调用；
- `reason`：决策依据，一句话，说明为什么选这个动作，不是思维链；
- `observation.summary`：工具返回的摘要，给人看；`observation.data` 是结构化返回，给机器看。

`result` 是交付物（回答、引用、缺口、下一步、依据），`citation_check` 是引用核验的判卷结果。
