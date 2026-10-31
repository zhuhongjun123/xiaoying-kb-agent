# 03 评测

## 1 怎么跑，跑出什么

```bash
cd xiaoying-kb-agent
python run.py run-cases --strict        # 11 组用例，断言不过退出码 1
python -m unittest discover -s tests    # 37 条单元测试
```

实测结果：11 组用例全过，37 条测试全过，引用核验没有一条问题。用例一共消耗 30 步、20 次工具调用。

控制台的原始输出，包括每条用例的 PASS/FAIL，在 `examples/test-results.txt`。

确定性验到了克隆这一级：`git clone` 到一个新目录，跑 `run-cases` 和 `unittest`，`git status` 是空的。也就是说 `examples/` 里那些文件都是跑出来的，评审在自己机器上重跑会得到同一份，不是手写快照。

## 2 用例和实测结果

跑之前先写清期望。每条用例的完整期望在 `data/cases.jsonl` 的 `expect` 字段里：要求的状态、必须引用哪份文档、答案必须含哪些字样、不得含哪些字样。跑完由 `check_expect` 逐条比对，所以下表「预期」那一列是跑之前就定好的，不是事后按结果回填的。

| 用例 | 场景 | 预期 | 实测 | 步/工具 | 引用 | 记录 |
| --- | --- | --- | --- | --- | --- | --- |
| C1 | 明确答案，单文档可答 | answered | answered | 2 / 1 | D03 | `transcripts/C1.md` |
| C2 | 文档冲突，30 天 vs 15 天 | conflict | conflict | 4 / 3 | D01,D02 | `C2.md` |
| C3 | 资料外问题 | out_of_scope | out_of_scope | 3 / 2 | 无 | `C3.md` |
| C4 | 改写后的相似问题 | conflict | conflict | 4 / 3 | D01,D02 | `C4.md` |
| C5 | 上下文追问，指代缺失 | answered | answered | 2 / 1 | D03 | `C5.md` |
| C6 | 工具失败，可恢复 | answered | answered | 3 / 2 | D05 | `C6.md` |
| C7 | 工具失败，不可恢复 | insufficient | insufficient | 3 / 2 | 无 | `C7.md` |
| C8 | 冲突可消解，取代声明 | answered | answered | 4 / 3 | D08 ×2 | `C8.md` |
| C9 | 预算耗尽 | unfinished | unfinished | 1 / 1 | 无 | `C9.md` |
| C10 | 明确答案，产品侧 | answered | answered | 2 / 1 | D04 | `C10.md` |
| C11 | 检索改进，标题加权 | answered | answered | 2 / 1 | D10 | `C11.md` |

完整 trace 在 `examples/traces/C*.json`，里面有每一步的动作、参数、返回摘要、决策依据和预算消耗。可复跑的输入在 `examples/inputs/`，结论在 `examples/outputs/`。

失败也记下来。真实模型通道首次接入时 11 组挂 7 个，原因与处置见 §9.3。规则策略这边从第一条可跑版本起就是 11/11，因为它按模板产答案，不会犯引用错误，这一点在 §9.1 单独说了。两类通道的成败不能互相替代。

单条复跑：

```bash
python -m src.main --input examples/inputs/C2-conflict.input.json
python -m src.main --input examples/inputs/C7-tool-down.input.json --out examples/outputs/C7-local.output.json
```

## 3 六个必验点在哪看

| 验收点 | 看什么 | 位置 |
| --- | --- | --- |
| 明确答案 | 引用整节原文，答案条目逐条来自原文 | `C1.md`、`C10.md`、`C11.md` |
| 文档冲突 | 两个数值并列、查证结论、请求确认、`next_actions` | `C2.md`，以及 `traces/C2.json` 的 `steps[1..3]` |
| 资料外问题 | 无引用，`gaps` 说明库内没这个主题，答案里没有编造的比例 | `C3.md` |
| 改写后的相似问题 | 和 C2 同状态、同引用集 | `C4.md` 对照 `C2.md` |
| 上下文追问 | 第一步的检索词是「设备借用可以借多久？ 那续借呢？」，问题本身不足以检索，靠 history 补全 | `C5.md` 第一步的参数 |
| 工具失败 | C6 两步检索（失败后改写重试），C7 连续失败后拒答 | `C6.md`、`C7.md` |
| 预算上限 | `budget.steps_used=1`，未完成态，说明已检索到什么 | `C9.md` |

## 4 审核要点：改文档，看结论变不变

题目要求「替换文档内容，核对答案与动作是否改变」。`tests/test_agent.py::TestDataDriven` 用临时目录复制一份知识库再改，每条测试锁一种变化：

| 测试 | 改什么 | 期望 | 结果 |
| --- | --- | --- | --- |
| `test_answer_follows_modified_value` | D01 的 30 天改成 45 天 | 答案出现 45 天，不再出现 30 天 | 通过 |
| `test_conflict_disappears_when_values_become_equal` | D02 的 15 天改成 30 天 | 状态从 conflict 变 answered | 通过 |
| `test_supersede_removed_falls_back_to_priority` | 删掉 D08 的 `supersedes` | 消解降到第二级，`basis` 提到优先级 | 通过 |
| `test_no_supersede_no_priority_becomes_conflict` | 取代和优先级都删掉 | 状态变 conflict | 通过 |
| `test_partial_resolution_still_reports_conflict` | 新增只取代 D01 的 D10 | 部分消解后仍是 conflict，答案含 10 天 | 通过 |
| `test_future_effective_date_breaks_resolution` | 把 D08 生效日期推到 2099 年 | 取代关系不成立，退回 conflict | 通过 |
| `test_new_topic_document_is_retrievable` | 新增一份门禁卡文档 | 不改代码就能被检索并作答 | 通过 |

结论不是写死在代码或提示词里，是文档内容的函数。这条我是拿测试证明的，不是嘴上说的。

## 5 引用可核验性

`citation_check` 对每条 `answered` 和 `conflict` 做两项机器校验：引用是不是原文子串，答案里的数字是不是都在引用里出现过。

11 组用例跑下来 0 条问题。反过来也验过：伪造引用（把 30 改成 45）会被 `test_fabricated_quote_is_caught` 抓住，数字没支撑会被 `test_number_without_support_is_caught` 抓住，`answered` 却给不出引用会被 `test_answered_without_citation_is_caught` 抓住。

校验不过时，如果状态是 `answered` 或 `conflict`，整条降级成 `insufficient` 并说明「引用无法与原文核对」。错误引用不会出现在交付结果里。

## 6 已修复的两个判定缺陷和一个排序缺陷

三条都是设计问题，不是编码笔误，修完都补了测试。

| 缺陷 | 表现 | 根因 | 处理 |
| --- | --- | --- | --- |
| 跨主题撞 key | 问设备借用，答成「调休 60 天」 | 「起算时点」在报销和调休文档里 key 相同值不同，被判成冲突；这个假冲突恰能被 D08 的取代声明消解，答案被消解结果劫持 | 证据集限定为「与最强命中同主题」，加两条测试 |
| 弱相关文档冒充冲突 | 问住宿标准，返回报销期限冲突 | 检索把 D01、D02 一起带回来，它们的真冲突污染了住宿问题 | 证据集加分数窗口（≥ 最高分 × 0.5） |
| 排序被泛段落压住 | 问住宿时 D01 的「目的与适用范围」排第一 | 那段正文里同时有出差、住宿、报销 | BM25 加标题命中加权，加测试 |

排序这条容易误判。症状表现为状态不对，根因却在排序，只看结果会一直往判定逻辑上找。

## 7 没做到、没验到的

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 真实模型通道 | 验了，样本小 | DeepSeek-V3 跑 11/11，见 §9。跑过若干轮，同一条用例不同轮次可能选不同（都合理的）路径，稳定性没有统计 |
| 冲突召回 | 保守 | 只认「同主题 + 同 key」。两份文档换个措辞说同一件事就不报冲突，会各自作答 |
| claim 抽取 | 只看 `键：值` 行 | 关键值写进长句的制度（「应在三十天内提交」）抽不到，冲突检测这一路就失效 |
| 消解规则 | 三级 | 「新法优于旧法」「上位制度优先」「特别规定优于一般规定」都没实现 |
| 多轮 | 单轮 + history 入参 | CLI 不存会话，真实产品需要 session |
| `needs_clarification` | 定义了没用上 | 留给「问题本身缺条件」这类（比如问年假几天却不说工龄），11 组用例没触发 |

## 8 一份 trace 长什么样

`examples/traces/C2.json` 节选：

```json
{
  "case_id": "C2",
  "provider": "mock-rule-policy",
  "budget": {"max_steps": 8, "max_tool_calls": 6, "steps_used": 4, "tool_calls_used": 3},
  "steps": [
    {"step": 1, "action": "search_docs",
     "args": {"query": "报销期限是多久？", "top_k": 6},
     "reason": "先检索资料库，确认是否存在可支撑回答的原文",
     "observation": {"ok": true, "summary": "检索「报销期限是多久？」命中 6 个片段，涉及 D01、D02…"}},
    {"step": 2, "action": "read_doc",
     "args": {"doc_id": "D01"},
     "reason": "「差旅费用报销期限」在 D01、D02 上取值不一致，读取 D01 全文核对适用范围与效力声明"},
    {"step": 3, "action": "read_doc", "args": {"doc_id": "D02"}, "reason": "…（另有 D02 待查）"},
    {"step": 4, "action": "ask_user",
     "args": {"status": "conflict", "question": "「差旅费用报销期限」在 D01、D02 中规定不一致，应以哪份为准？",
              "options": ["D01", "D02"]},
     "reason": "两份文档都没有替代或优先级声明，随机选一个等于编造，选择追问确认"}
  ],
  "result": {"status": "conflict", "citations": [{"doc_id": "D01"}, {"doc_id": "D02"}],
             "gaps": ["两份文档均未声明谁优先（无替代/废止条款）", "…"], "basis": "…"},
  "citation_check": {"ok": true, "problems": []}
}
```

第二、三步是第一步的结果带出来的：检索看到同一条款两个数值，才决定去读原文；读完两份仍找不到可消解的依据，才决定追问。这是「选动作 → 执行 → 看结果 → 调下一步或结束」的闭环，不是预设的固定流程。

## 9 真实模型实测

`python run.py --provider llm run-cases --out examples/llm`，11 组全部通过，trace 和运行记录在 `examples/llm/`。

### 9.1 两条通道对照

| 指标 | mock（规则策略） | llm（DeepSeek-V3） |
| --- | --- | --- |
| 用例通过 | 11/11 | 11/11 |
| 总步数 / 工具调用 | 30 / 20，完全确定 | 35–38 / 25–28，多轮区间 |
| 引用核验问题 | 0 | 0 |
| 答案形态 | 抽取式：整节原文加条目 | 生成式：自然语言归纳，逐条给精确引用 |
| 冲突时的动作 | `ask_user(status=conflict)` | 同样，追问语由模型自己写 |
| 可复现 | 完全确定，无随机，trace 不含耗时 | 不确定，同一用例重跑可能换路径 |

产物位置是对称的：`examples/{inputs,outputs,traces,transcripts}/` 是 mock，`examples/llm/` 下同样四个子目录是真实模型，可以拿同一条用例的两份输出直接对着看。

模型多走的步数集中在检索和查证上（C3 多一轮检索，C7 多一轮重试，C11 多读一次原文），不是失控。

真实模型这边一共出现五种动作序列，步数 2 到 7 不等：

| 序列 | 用例 | 什么情况下走这条 |
| --- | --- | --- |
| `search` → `finish` | C1、C5、C10 | 一次命中，无冲突 |
| `search` → `read` → `read` → `ask_user` | C2、C4 | 读到两份原文仍判不了，追问 |
| `search` → `read` → `read` → `finish` | C8 | 读到取代声明，可消解 |
| `search` → `read` ×3 → `finish` | C11 | 多读一次原文 |
| `search` ×6 → `finish` | C7 | 检索连续失败，反复换词 |

同一批输入分出五条路径，说明下一步不是预先写死的。规则策略通道也是五种（C9 落在预算中止那一支）。

### 9.2 它自己走出来的路径

C2 的实际记录：

```text
1 search_docs {"query": "报销期限", "top_k": 6}   reason: 检索与报销期限相关的文档片段
2 read_doc    {"doc_id": "D01"}                  reason: 查证D01的适用范围、优先级与替代关系以解决冲突
3 read_doc    {"doc_id": "D02"}                  reason: 查证 D02 的适用范围、优先级与替代关系以消解冲突
4 ask_user    {"status": "conflict", "options": [...], "question": "…两者均为最新版本且适用范围相似，请确认应执行哪个期限？"}
```

路径和 mock 一样，但这些理由是模型自己写的，第二、三步也是它看到「同一条款两个数值」之后自己决定去读的。这算真实模型上的一次闭环，不是脚本。

### 9.3 接真实模型才暴露的四个缺陷

前三个是实现层问题，与模型能力无关。共同点是：规则策略跑通不代表真模型跑得通，模型会严格按提示词里给的证据格式去引用。

| # | 缺陷 | 现象 | 根因 | 修法 |
| --- | --- | --- | --- | --- |
| 1 | 证据渲染把原文拼坏了 | 7/11 被判成 `insufficient`，理由都是「引用原文找不到」 | `_render()` 用 `" / "` 把多行片段拼成一行喂给模型，模型照着拼成 quote，自然不是原文子串 | 逐行渲染并缩进；提示词写明 quote 必须是一段连续原文，多行就写多条 citations |
| 2 | 列表符号导致不匹配 | C1 引用还是对不上 | 模型复制时丢了行首 `- `，而归一化只去空白不去列表符号 | `norm()` 加上去行首列表符号。格式不是内容 |
| 3 | 模型不知道今天是几号 | C8 判成「D08 尚未生效」，走成 `ask_user` | 提示词里没有当前日期，凡是「生效日期 vs 今天」的判断都会错 | 状态里加当前日期；同时给判定层补 `_is_effective()`，未生效的修订版不能拿来消解 |
| 4 | 断言卡空格 | 5 条用例实际是 answered，断言却不过 | 断言用原文格式（`60 天`）去比模型输出（`60天`） | 断言改成空白不敏感，C5 的期望改成语义化（`续借` + `5 个工作日`） |

第 3 条牵连两处：提示词缺上下文、判定层缺规则。两边都得改。只改提示词，换个模型还会犯；只改判定层，模型会继续给出和判定结论相反的叙述。

### 9.4 一个还没解决的分歧

C8 上 mock 给「60 天」，真实模型给「按生效日期分段：D07 管 2026-06-01 之前的加班，D08 管之后的」。两个都站得住，但口径不一致。mock 的消解是整份文档取代，只看 `supersedes` 字段；模型的读法更接近「条款按时间分段适用」，这在真实制度解释里也说得通。

统一的做法是让 `supersedes` 支持条款级粒度（`D07#4.2`），并在答案里显式写出适用时间区间。本轮未实现，列入 `docs/05-retrospective.md` §4。
