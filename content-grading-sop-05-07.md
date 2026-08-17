# Content Grading SOP / Runbook（Training Sample 强化版）


## 0. SOP 概览

### 任务目标

对每个完整 slide deck 的**内容质量**进行相对评分。只提交：

1. 一个 `1–7` 分的单一分数；
2. 一个 `Brief feedback` rationale。

界面虽然只有一个分数，但判断时必须综合四个内容维度：

1. Writing Quality；
2. Organization and Storytelling；
3. Comprehensiveness and Substance；
4. Groundedness and Accuracy。

### 核心原则

> **Content only, never aesthetics.**

只评价 deck 的文字内容，不评价视觉设计。只有当视觉元素直接导致内容难以理解时，才可以提及。

### 标准执行流程

```text
识别 batch / task type
        ↓
检查是否为外语任务 ── 是 → Escalate；不要翻译
        ↓ 否
读 prompt、user request、source material 与所有 responses
        ↓
按四个内容维度比较整个 batch
        ↓
先排出明确的 best 与 worst
        ↓
按 batch size 分配 1–7，检查 tie 规则
        ↓
为每个 response 独立手写 ≥5 个完整、扎实的句子
        ↓
检查 slide、原文引用、criterion、影响、修改建议与优点
        ↓
最终检查分数极值、范围、rationale 独立性与内容边界
        ↓
Submit
```

---

## 1. 输入、输出与时间限制

### 输入

- Feather 中的 task header；
- batch name；
- User message / request；
- Review instructions；
- 可能存在的 reference material；
- 同一 task 中的全部 response decks。

### 输出

每个 response 需要：

- 一个整体 `1–7` 分；
- 一个 `Brief feedback`；
- feedback 至少五个 robust、完整句子；
- 每个 rationale 必须针对该 response 独立撰写。

### 时间限制

- Content Grading 限时：`45 分钟`；
- 计时从 claim task 时开始；
- 超时后 Feather 会自动释放任务，而且无法继续该任务；
- 如果时间不够，应主动 release，不要提交空 rationale；
- 反复留空可能导致 warning，最终可能 offboarding。

---

## 2. Step 1 — 识别 Content Grading 任务

### 识别方法

最快的判断信号是 **batch name**。任务类型位于 batch name 开头。

Batch name 可以在两个位置找到：

1. task list 右侧的 `Task batch` 列；
2. 打开 task 后右上角、campaign name 旁边的 batch chip；header 中也会出现，并带有 instruction set 链接。

Content Grading 对应位置为：

- Campaign / batch：`HL Content Grading (Les Artistes)` campaign；
- Task type：`Content Grading`；
- 标准时限：`45 min`。

### 外语任务处理

如果 task 是外语任务：

1. 必须 escalate；
2. 即使你看得懂该语言，也必须 escalate；
3. 不得翻译后自行评分。

---

## 3. Step 2 — 锁定评分范围

### 必须评价

- 语言是否清楚、自然、适合目标读者；
- idea 的组织和逻辑推进；
- deck 内容是否连贯、容易跟随；
- 内容是否有足够的相关信息和合适深度；
- 是否存在重复、空泛、公式化、不自然的 AI slop；
- claim 是否忠实于题目提供的 source material。

### 禁止评价

- 视觉美感、视觉 polish；
- typography、color palette、style fidelity；
- layout、formatting、image choice 等视觉元素；
- instruction following 或 prompt adherence，除非 Feather scorecard 明确将其列为评分 criterion；
- 通过外部知识或网页搜索进行事实核查；
- image / figure 的审美质量；只可把它们作为内容，判断是否 relevant、useful；
- 外语内容的翻译。

### 唯一视觉例外

只有在 layout、formatting、image 或其他视觉元素**直接妨碍文字内容的理解**时，才可以提及；关注点仍然必须是 understandability，而不是 aesthetics。

---

## 4. Step 3 — 按四个维度评价内容

### 4.1 Writing Quality（avoids AI slop）

判断文字是否清晰、自然、直接、适合目标读者。

| Low signal | High signal |
|---|---|
| confusing、vague、awkward 或过度复杂 | direct、precise、natural |
| filler、boilerplate、empty slogans | 信息具体且用词朴素 |
| unadapted placeholders | 内容完整、针对当前主题 |
| self-referential 或重复表达 | 每句话都推动读者理解 |
| nonsensical / contradictory phrasing | 逻辑和含义清楚 |

#### 应奖励

- 直接、精确、不绕弯的语言；
- 符合读者背景的自然措辞和术语；
- 在保留含义的前提下简洁、不重复。

#### 应扣分

- confusing、awkward、vague 或不必要地 elaborate；
- generic / repetitive AI filler、empty slogans；
- self-referential text；
- jargon、big words、公式化表达或过度标点；
- 用复杂语言表达本可简单说明的内容。

### 4.2 Organization and Storytelling

判断 deck 是否把内容组织成一份有清晰结构的 presentation。

| Low signal | High signal |
|---|---|
| sections disconnected | 清楚的 through-line |
| abrupt transitions | 逻辑自然的 progression |
| fact dumps | ideas 有明确优先级 |
| 关键观点被埋没 | 关键观点突出且前后衔接 |

### 4.3 Comprehensiveness and Substance

判断内容是否覆盖重要信息，并对目标读者提供合适深度和真正有用的材料。

| Low signal | High signal |
|---|---|
| important omissions | key concepts 覆盖完整 |
| shallow / underdeveloped | 解释有足够深度 |
| padding 只增加篇幅、不增加价值 | figures 与材料承载真实信息 |

### 4.4 Groundedness and Accuracy

判断 claims 是否受到已提供 source material 的支持，并忠实表达来源中的 claim、evidence 和 certainty。

| Low signal | High signal |
|---|---|
| unsupported / invented claims | source-supported claims |
| misleading 或 contradictory | 忠实表达来源 |
| misrepresented sources | 保留原来源的 evidence 与 certainty |

执行边界：

- 只对 prompt、user request 和提供的 source material 做内部比对；
- 不使用 web search；
- 不利用外部知识独立 fact-check。

---

## 5. AI Slop 识别表

### 常见类型

| 类型 | 定义 | 典型信号 |
|---|---|---|
| Filler | 表面 polished，但信息量很低 | “unlock growth”“drive meaningful impact” 等空泛好处 |
| Boilerplate | 几乎可套用于任何公司或主题 | 泛泛谈 innovation、quality、customer satisfaction |
| Placeholders | 未完成的模板文字 | `[Insert company name]`、`[audience segment]` |
| Nonsensical / contradictory | 语言流畅但逻辑不成立 | 结论互相矛盾或因果关系错误 |
| Self-referential | 文字讨论文档自己，而不是推进理解 | “This presentation highlights…” |
| Repetitive phrasing | 用不同措辞重复同一观点 | efficiency / productivity / work effectively 的同义堆叠 |

### 重点识别信号

| Criterion | Bad pattern | 应达到的方向 |
|---|---|---|
| Formulaic or slogan-like | stock phrases；强行使用 “it’s more than…” | 直接描述具体变化或结果 |
| Rhythm over meaning | 为节奏而堆叠分号、排比、重复 | 标点服务逻辑与含义 |
| Reads like AI | “delve”“pivotal”“tapestry”“underscore”“testament”等 stock AI words；机械式 bold-label bullets | 使用目标读者日常使用的 plain terms |
| Claims without evidence | 宣称 benefit / result / source，但不给依据 | 给出 source 中的具体证据 |
| Meaning you cannot recover | 未定义术语、stacked nouns、缺失关键细节 | 给出可理解的明确限制、数字或关系 |
| More words than needed | corporate padding、hedging、冗长措辞 | 删除无信息量的铺垫 |
| Buries the point | 先做背景铺陈、复述、总结或格式说明才进入重点 | 尽早说明核心决定或结论 |

> 此列表不是穷尽清单。其他会让语言变得 generic、unnatural、repetitive 或 meaningless 的模式，也应纳入判断。

---

### 四类 Failure Mode 分类器

训练 task 将专业写作中的主要失败归纳为四类。先判断句子对读者造成的**主要损失**，再选择 primary category；不要只因为看到某个标点或单词就机械分类。

#### A. Formulaic, slogan-like, or figurative language

**何时 flag：** underlying claim 尚可理解，但被包装成 stock formula、slogan、staged cadence、canned emotional phrase 或 strained metaphor。重复使用 colon、semicolon、em dash 来制造节奏或强调，而不是澄清逻辑，也属于此类。

常见信号：

- “not just X — it is Y”“not only X, but also Y”等 inflated contrast；
- “Faster, smarter, and more intuitive”式 rhetorical triad；
- “One team. One vision. Limitless possibilities.”式 slogan fragments；
- “From X to Y”式 transformation arc，却没有说明实际变化；
- 为节奏堆叠 semicolon、colon、em dash；
- strained metaphors、canned empathy、synthetic balance；
- 把普通事实夸大成 legacy、identity、pivotal moment 或 broader trend；
- 没有明确来源的 “Experts argue”“Observers note”“Research shows”；
- 密集使用 `delve`、`pivotal`、`robust`、`tapestry`、`underscore`、`showcase`、`foster`、`intricate`、`landscape`、`testament`、`vibrant`；
- 在没有实际用途时，机械重复 `**Label:** explanation` 格式。

**不要误判：** parallel structure、contrast 或 punctuation 如果确实表达了真实区别、列表或逻辑关系，就不应 flag。

- 可接受：`The bug is in the parser, not the tokenizer.`
- 可接受：`The red light means stop, and the green light means go.`

#### B. Vague, inflated, or unsupported substance

**何时 flag：** reader 无法判断具体发生了什么、benefit 为什么成立、claim 有什么 evidence，或 decision 的 reason 是什么。问题核心是 evidence、causality、actor 或 observable meaning 缺失，导致具体含义无法恢复。

常见信号：

- empty abstraction：`unlock value`、`foster alignment`、`drive meaningful impact`；
- tacked-on benefit：先写一个事实，再无依据地追加 “ensuring a seamless experience”；
- inflated significance：`This represents a profound shift.`；
- unnamed authority：没有 source 的 `Research consistently shows...`；
- shorthand 隐藏真正含义，例如 “doing the heavy lifting”“resets the bar higher”；
- 描述 review / alignment process，却不说实际 decision reason；
- oversimplification 删除了必要限定，使原 claim 的含义改变。

**不要误判：** claim 如果有 concrete result、identified source、constraint 或 approval requirement 支持，就不属于这一类。

- 可接受：`The change removes one approval step.`
- 可接受：`The 12 June accessibility audit found 14 missing labels.`
- 可接受：`Legal and Security must approve the exception before release.`

#### C. Wordy, jargon-filled, or indirect language

**何时 flag：** rationale / meaning 可以理解，但在不损失必要含义或真实 qualification 的前提下，本可写得更短、更直接。问题包括 bureaucratic、hedged、verbose、compressed、indirect、jargon-heavy 或过度复杂的句法。

常见信号：

- bureaucratic phrasing：`operational implications associated with this transition`；
- stacked hedging：`may potentially be worth considering whether...`；
- 无意义的 verbosity：`at this point in time`、`commence the process of`；
- compressed abstraction：把简单限制压缩成难懂的抽象名词堆叠；
- unexplained jargon：`cross-functional enablement layer for downstream value realization`；
- overcomplicated sentence structure：一句中叠加多个 contrast、definition 和 clause。

**不要误判：** accurate technical term、legal condition 或 explained uncertainty 不等于 jargon slop。

- 可接受：`The API returns 429 when the client exceeds the rate limit.`
- 可接受：`The estimate is preliminary because two regions have not reported.`
- 可接受：`The vendor may terminate only after giving 30 days' written notice.`

#### D. Unnecessary framing, repetition, or structure

**何时 flag：** setup、repetition 或 formatting 推迟重点，或让文档更难 scan。

常见信号：

- generic scene-setting：`In today's fast-paced digital landscape...`；
- restating the request，而不是回答；
- meta-announcement：`Below is a polished and comprehensive rewrite...`；
- `In conclusion...` 后只重复已经说过的结论；
- 很短的内容拆成过多 headings 和 bullets。

**不要误判：** framing 如果用于缩小范围、纠正 request、解释 omission，或帮助读者查阅 reference material，就是有功能的结构。

- 可接受：`This memo covers the two launch decisions due Friday.`
- 可接受：`Each API endpoint uses Request, Response, and Errors headings for lookup.`

### Primary Category 决策树

以下是把训练材料转成可执行流程的 operational synthesis，不新增评分 criterion：

```text
1. 读者能否恢复“谁做了什么、为什么、依据是什么”？
   └─ 不能 → Vague / inflated / unsupported substance

2. 含义基本清楚，但是否被 slogan、stock formula、metaphor 或人为节奏包装？
   └─ 是 → Formulaic / slogan-like / figurative

3. 含义清楚，但是否可在不损失必要含义的前提下明显缩短或直写？
   └─ 是 → Wordy / jargon-filled / indirect

4. 句子本身可能没错，但 setup、重复、总结或结构是否推迟了重点？
   └─ 是 → Unnecessary framing / repetition / structure

5. 都不是 → 不要为了“找 slop”而强行 flag
```

如果一句话同时触发多类，优先选择最能解释**主要 reader cost** 的类别：

- 看不懂具体 claim → substance 问题优先；
- claim 清楚但包装做作 → formulaic 优先；
- claim 清楚但表达过长 / 过度抽象 → wordy 优先；
- 信息没错但被铺垫或重复掩埋 → framing 优先。

### False-positive Guard：四个误报检查

在 flag 前逐项确认：

1. **Concrete distinction**：对比是否表达了真实、具体的区别？
2. **Audience fit**：术语是否是目标行业和读者真正使用的标准术语？
3. **Necessary qualification**：hedging 是否在解释真实的不确定性或法律限制？
4. **Observable support**：是否已经给出数字、source、constraint、approval requirement 或可观察结果？

任意一项成立时，不能只根据表面句式自动判 slop；需要回到上下文判断。

### 两遍扫描法

#### Pass 1 — Substance / meaning

对每个关键 claim 问：

- actor 是谁？
- action / change 是什么？
- reason / causality 是否说清楚？
- evidence 或 source 在哪里？
- 限制、certainty 和重要 qualifier 是否保留？
- headline 是否直接说明 finding，而不是只给 generic topic？

#### Pass 2 — Expression / delivery

再检查：

- 是否使用 stock formula、slogan 或 manufactured rhythm？
- 是否有可以删除的 bureaucratic wording、hedging 和 padding？
- 是否用 meta framing、request restatement 或 redundant conclusion 推迟重点？
- 是否有多余 headings / bullets，反而降低 scanability？
- 是否存在没有用途的 mechanical formatting？

#### Pass 3 — 误报复核

最后应用 False-positive Guard，避免把真实对比、行业术语、必要限定和证据充分的陈述误判为 slop。

### 行业标准校准

高质量专业写作不等于完全不用术语，而是术语要适合 intended audience、含义明确，并由结构或 evidence 支持。

| 领域 | 高质量信号 |
|---|---|
| Finance | title / subtitle 清楚；语言 concise、direct、evidence-based；investment rationale 易于理解，不堆不必要 jargon |
| Consulting | conclusion-first title 直接说 main finding；chart / annotation 支撑 takeaway；`margin expansion`、`valuation multiple compression` 等行业术语对目标读者可接受 |
| SWE | clear、objective、无 slogan；用 concrete facts 说明 scale；明确列出 numerics、performance、reliability 等实际 engineering concern；必要时提供 source links |

关键校准原则：

> **不要问“这个词听起来专业吗”，要问“目标读者是否能从中恢复具体含义，而且这个术语是否比普通替代词更准确”。**

### Context-first 示例校准

| 句子 | 结论 | 原因 |
|---|---|---|
| `From paper-bound practicals to a shared digital workspace.` | Flag | transformation arc 听起来有力度，但没有说明具体改变了什么 |
| `The bug is in the parser, not the tokenizer.` | 不 flag | contrast 表达了真实、具体的技术区别 |
| `The workflow operationalizes a cross-functional enablement layer for downstream value realization.` | Flag | 抽象 business terms 隐藏 actor 和 action |
| `The 12 June accessibility audit found 14 missing labels.` | 不 flag | 明确 source、date 和 observable result |
| `This represents a profound shift.` | Flag | 只宣称 significance，没有具体 change 或 evidence |
| `The API returns 429 when the client exceeds the rate limit.` | 不 flag | 准确技术术语说明 concrete condition |
| `In today's fast-paced digital landscape, effective communication is more important than ever.` | Flag | generic setup 推迟重点，未增加 useful context |

### 从句子 Flag 回到 Deck Score

Failure-mode 标记是证据，不是机械计数器。分配 deck-level score 时：

1. 看问题是否 repeated、central，并影响重要 claim；
2. 看读者是否仍能恢复核心 meaning、evidence 和 logical flow；
3. 区分一个 isolated awkward sentence 与贯穿 deck 的系统性 slop；
4. 同时 credit concrete、direct、source-grounded 的部分；
5. 最终仍综合四个主维度，而不是按 flag 数量直接换算分数。

---

## 6. Step 4 — 分配 1–7 分

### 基本规则

1. 在**当前 task 内部**进行相对排名：最差是 `1`，最好是 `7`；
2. 不要把不同 task 的分数做统一标准化；
3. 评价整个 deck，不要把各 slide 分数简单平均；
4. 分数要在当前 batch 内拉开差异；
5. 每个 task 必须有且只有一个明确的 `1` 和一个明确的 `7`；
6. `1` 和 `7` 均不得 tied；
7. 先识别 clear winner 与 clear loser，再分配中间分；
8. 对相近 response，用具体内容差异打破不必要的 tie。

### 按 response 数量分配

| Responses | 分配方式 | Tie 规则 |
|---:|---|---|
| `< 7` | 使用 gap 表示差距。例如明显领先可用 `{1,2,3,7}`；均匀差异可用 `{1,3,5,7}` | 不允许 tie；每个分数必须不同 |
| `7` | `1–7` 各用一次 | 不允许 tie |
| `8` | `1–7` 全部使用，并出现一次 tie | 只允许一对 tie；不得 tie 在 1 或 7 |
| `9+` | `1–7` 全部使用；每多一个 response，增加一对 tie | 只允许 pairwise tie；禁止 3-way tie；不得 tie 在 1 或 7 |

### 快速算法

1. 先把 responses 按综合内容质量从差到好排序；
2. 将最差设为 `1`、最好设为 `7`；
3. 根据相邻 responses 的真实差距分配中间整数；
4. 按 batch size 校验 tie 数量；
5. 再检查是否用了完整范围，以及 `1`/`7` 是否唯一。

---

## 7. Step 5 — 编写 Brief Feedback Rationale

### 强制要求

每个 response 的 rationale 必须：

- 至少 `5` 个 robust、完整句子；
- 使用完整句子，不使用 bullets；
- 为该 response 全新、独立撰写；
- 不得复制粘贴或重复使用相同句子 / 固定短语；
- 不使用第一人称；描述 artifact，不描述个人喜好；
- 只讨论 content quality，不讨论 visual design；

### 推荐句子顺序

1. 先用 `1–2` 句说明 writing / content 做得好的地方；
2. 其余句子说明决定该分数的具体内容问题；
3. 至少覆盖能解释该分数的关键 rubric dimensions；
4. 每个问题都说明对读者理解造成的具体影响；
5. 对有问题的措辞给出应该如何改写的 plain version；对优点说明应该保留什么。

### 每个句子的五个组件

每句话都应尽量形成以下证据链：

```text
Slide number / title
  + exact quoted wording
  + named criterion
  + reader impact / why it matters
  + plainer revision（或说明值得保留的做法）
```

必须落实的检查项：

1. **Name the criterion**：明确说出对应 criterion 或 rubric row，例如 `Formulaic or slogan-like`；
2. **Quote the wording**：用引号引用 slide 中可被 reviewer 定位的 exact phrase；
3. **Say how to revise it**：指出更清楚、朴素的写法；
4. **Cite the slide**：写明 slide number 或 title；
5. **Credit what works**：至少一句说明 deck 做对了什么、应保留什么；
6. **Stay in scope**：只判断文字内容。

### 第三人称要求

不要写：

> I don't like the title slide because it is crowded.

应该描述 artifact 本身。对于 Content Grading，句子应进一步聚焦到文字内容，例如：某 slide 的具体 phrase 是否清楚、是否空泛、是否有 source 支持，以及应如何改写。

### Content Grading 的合格标准

一句话只有同时包含以下信息，才具有足够证据：

- slide reference；
- specific content observation / exact wording；
- 与 Content Grading standard 的明确联系。

Section 07 对 Content Grading 的进一步要求是：每句话还应说明该措辞对读者造成什么影响，并在需要时给出更好的写法。

---

## 8. 禁止行为与高风险反模式

### 绝对禁止

- 把同一句或相同短语复制到多个 response；
- 把视觉审美当作 Content Grading 的评分依据；
- 用网页搜索或外部知识核查 claim；
- 翻译并处理外语任务；
- 在时间不足时提交空 rationale。

### 会被判弱或 flagged 的 rationale

- “Response B is better written.”
- 只有分数标签或一句泛泛评价；
- 有五句话，但缺少 criterion、quote、slide reference 或 revision；
- “The deck is clear and professional.” 但没有具体证据；
- deck-wide statement，没有 slide number；
- 第一人称喜好，例如 “I like / I don’t like…”；
- 多个 response 使用几乎相同的句式和 reasoning。

> 单纯达到“五句话”不等于合格；证据链的完整性才是关键。

---

## 9. 提交前双重检查

### A. Score 检查

- [ ] 所有 responses 都已完整阅读和比较；
- [ ] 评分对象是整个 deck，不是 slide 平均分；
- [ ] 有且只有一个 `1`；
- [ ] 有且只有一个 `7`；
- [ ] `1` 和 `7` 没有 tie；
- [ ] 分数覆盖完整范围，并体现 batch 内真实差距；
- [ ] tie 数量符合 batch size；
- [ ] 没有 3-way tie；
- [ ] score 只反映 content，不反映 aesthetics；
- [ ] 没有使用外部 fact-check。

### B. AI Slop 判定检查

- [ ] 先判断具体 meaning / evidence 是否可恢复，再看文风；
- [ ] primary category 对应的是主要 reader cost，而不是最显眼的表面词汇；
- [ ] 没有把真实 contrast 或正常 parallel structure 自动判为 formulaic；
- [ ] 没有把准确技术术语、法律条件或合理 uncertainty 自动判为 jargon；
- [ ] 已检查数字、source、constraint、approval requirement 等 observable support；
- [ ] 已区分 isolated awkward sentence 与 repeated / central failure；
- [ ] 没有按 slop flag 数量机械换算 deck score；
- [ ] 已 credit concrete、direct、source-grounded 的内容。

### C. 每个 Rationale 检查

- [ ] 至少五个 robust、完整句子；
- [ ] 开头 1–2 句 credit what works；
- [ ] 每句话包含 slide number 或 title；
- [ ] 每句话引用该 slide 的 exact wording；
- [ ] 每句话明确关联 criterion；
- [ ] 问题句说明 wording 对读者造成的影响；
- [ ] 问题句给出 plain revision；优点句说明应保留什么；
- [ ] 没有第一人称；
- [ ] 没有 visual-design 评论；
- [ ] 没有 instruction-following 评论，除非 scorecard 明确要求；
- [ ] 没有外部事实核查；
- [ ] 该 rationale 是针对该 deck 的全新 reasoning；
- [ ] 没有与其他 response 重复句子或固定短语；

### D. Escalation / Time 检查

- [ ] 外语 task 已 escalate，未翻译；
- [ ] 有足够时间完成所有 rationale；
- [ ] 若时间不足，已 release，而不是提交空内容。

---

## 10. 学习速记卡

### 一句话定义

Content Grading = 对同一 task 内的完整 decks 做**内容质量相对排序**，综合 writing、organization、substance、groundedness 给出一个 1–7 分，并为每个 response 写出有 slide 和原文证据的独立 rationale。

### 必背数字

- `45 min`：任务时限；
- `1–7`：评分范围；
- `1 × score 1`：唯一最差；
- `1 × score 7`：唯一最好；
- `≥ 5 sentences`：每个 Brief feedback 的最低要求；
- `0 external fact-check`：不得外部核查；

### 必背四类 Failure Mode

1. **Formulaic**：意思可懂，但被 slogan、stock formula 或 manufactured rhythm 包装；
2. **Substance**：具体 change、reason、actor、causality 或 evidence 无法恢复；
3. **Wordy**：意思可懂，但可在不损失必要含义的情况下明显缩短或直写；
4. **Framing**：setup、重复、总结或结构推迟了重点。

### 必背误报防线

> **Real contrast → Audience-fit term → Necessary qualification → Observable support**

### 必背五词证据链

> **Slide → Quote → Criterion → Impact → Revision**

### 最终心法

> 先比较、后打分；先证据、后结论；只看内容、不看美学；每个 response 都要有独立且可复核的理由。
