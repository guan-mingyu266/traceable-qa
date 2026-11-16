# traceable-qa

把财报 PDF 与业务表格，变成「每句话都能点到第几页、第几行」的问答入口。

> **状态（2026-11-16）**：骨架完成。HTTP 契约、compose 编排、依赖与边界已就位；`/api/*` 一律返回
> `501`。业务逻辑按第 5 节分阶段落地。

---

## 1. 这是什么 / 不做什么

**是什么**：面向内部员工的可溯源问答。输入财报 PDF 与业务 CSV，输出带**页级 / 行级**引用的答案；
证据不足时明确拒答。

**不做什么**：

1. 不做通用闲聊，超纲一律拒答。
2. 不做 Agent / 工具调用 / 写操作，只读。
3. 不做扫描件 OCR（只处理可提取文本的 PDF）。
4. 不做多租户 RBAC，只做文档级 tag。
5. 不做分布式与高可用，单机 + SQLite 卷。
6. 不做模型微调，embedding / reranker 用现成模型。

## 2. 边界：谁的代码

本仓库是**应用层**，不含任何上游源码副本。

| 层 | 归属 | 位置 |
| --- | --- | --- |
| 运行时（存储 / Memory / Context / HTTP） | 上游 OceanBase PowerContext（Apache-2.0） | 依赖引入 |
| CSV 行级采集、PDF 页级解析与证据检索、HTTP 重排、Embedding 合并 | 本人（竞赛题二/四/五/八） | `guan-mingyu266/powercontext@resume` |
| 检索编排、字节预算组装、生成与引用挂载、拒答、Web、评测、部署 | 本人 | 本仓库 |

运行时 fork 的边界证据：

```bash
git -C ../powercontext_competition diff --stat upstream/2026-competition HEAD
# 4 个新增文件 +1293/−0（另有 1 条 README 声明）
```

**硬性规则**：

- 本仓库**不得复制** `powercontext` 的源码，包括上面那 4 个模块。需要复用就 `import`。
- `pack.py` / `retrieve.py` 必须自己实现，不得搬运上游或竞赛其他题目的实现。这是本仓库全部
  归属声明的前提，破了这条，边界就不成立了。

## 3. 系统架构

```
┌──────────────────────────────────────────────┐
│ web/        上传 · 问答 · 引用跳转（pdf.js）    │
├──────────────────────────────────────────────┤
│ api.py      POST /api/upload /api/ask         │
│             GET  /api/documents /health       │
├──────────────────────────────────────────────┤
│ answer.py   生成 → 引用挂载 → 后置校验 → 拒答   │
│ pack.py     max_bytes 装箱 · 片段 start/end   │
│ retrieve.py 多路 query → 融合 → 重排            │
│ ingest.py   PDF/CSV → Source（复用 fork 解析）  │
├──────────────────────────────────────────────┤
│ pc 容器     PowerContext Server（SQLite + 卷）  │
└──────────────────────────────────────────────┘
```

四段链路，每段一个验收点：

| 链路 | 输入 | 输出 | 验收点 |
| --- | --- | --- | --- |
| 摄取 | PDF / CSV 字节 | 页级 / 行级 Source | 全页覆盖、重复提交幂等 |
| 召回 | query | 候选（带通道标记） | Recall@10、延迟 |
| 组装 | 候选 + 预算 | 片段 + 精确引用 | 不超预算、`text == 原文[start:end]` |
| 生成 | 片段 + 问题 | 答案 + 引用 + 拒答 | 引用准确率、拒答率 |

## 4. 四个设计决策

> 注意归属：D1 是本项目要**证明**的东西；D2/D3/D4 的约束来自运行时与竞赛题面，本人做的是在约束下的
> 取舍与实测。对外表述不要说成"我选择了"。

**D1 · 引用粒度到页 / 行，不到 chunk**

chunk 是切分算法的产物，改一次 `chunk_size` 历史引用全部失效。因此：

- PDF 页身份 = `(document_id, sha256, page_number)`，物理页，从 1 起。
- CSV 行身份 = `csv:{document_id}:{primary_key_value}`。
- 片段引用带 `start` / `end`，`text == entry.text[start:end]` 逐字一致。

代价：页可能很长（一页 800+ 词），检索粒度粗，需要更强的重排与后处理。

**D2 · 存储：运行时用 SQLite + 卷，单进程**

代价：跨库没有事务。用「先写元数据（pending）→ 写运行时 → 回写状态（ready/failed）」的补偿流程，失败可重放。

**D3 · 上下文按字节预算组装，不按 top-k**

给定 `max_bytes`，按「相关度 × 证据密度」选原文片段，输出严格不超预算（含引用与格式开销）。
代价：计量必须精确（`len(s.encode("utf-8"))`，含 envelope），算错会截断引用。

**D4 · 失败可撤回，不物理删除**

来源撤回 → 关联记忆置为 `forgotten`（不参与召回，保留版本与引用链）；恢复 → 重新激活。

## 5. 开工计划

依赖顺序：P3 → P4 → P5 → P6 → P7。P6 可以与 P5 并行。

### P3 · 检索桥（3 天）

产出 `src/qa/ingest.py`、`retrieve.py`、`pack.py`。

- `ingest.py`：PDF 逐页 / CSV 逐行 → 提交为 Source；幂等键 `doc_key + sha256`；失败原子化。
- `retrieve.py`：原始问题 + 实词拆分 + 概念扩展多路查 `search`；应用层融合 FTS 与向量候选
  （RRF，`k=60`，α 可扫）；对 Top-N 调重排服务，**分批请求 + 跨批次全局排序 + usage 记账**。
- `pack.py`：候选片段 → 按 `max_bytes` 贪心装箱 → 装不下取起点对齐前缀 → 剩余预算向相邻句扩张合并
  → 输出 `{text, start, end, citation}`。

**验收**（`make probe`）：

- 每问输出字节数 ≤ `QA_MAX_BYTES`（含引用开销），1.4KB / 1.8KB 两档都不超。
- 每个片段满足 `text == source_text[start:end]`。
- 黄金集 20 条上打印 Recall@10。

### P4 · 生成 + 引用 + 拒答（4 天）

产出 `src/qa/answer.py`。

- Prompt 只允许使用给定片段，引用编号必须来自候选集，禁止自造。
- 后置校验：解析答案里的 `[n]`，凡编号不在候选集、或该片段不支持该句 → 剔除并记日志。
- 三层拒答：① 重排分低于阈值；② 问题实词与候选实体的重叠不足；③ 自检（答案中无法挂引用的
  事实句占比过高）。
- 输出结构：`{answer, citations[], refused: bool, refusal_reason}`。

**验收**：事实型 20 条，引用编号 100% 落在候选集内；超纲 10 条 ≥9 拒答；20 条正常问题误拒 ≤2。

### P5 · 全栈（5 天）

产出 `src/qa/web/`：

- 上传页：拖入 PDF/CSV → 进度 → 文档列表（状态 / 页数 / 撤回 / 恢复）。
- 问答页：提问 → 流式或整段答案 → 每句后的 `[n]` 可点 → 右侧 PDF 用 pdf.js 跳到 `#page=N` 并高亮
  `start/end` 区间；CSV 引用定位到行并高亮主键。
- 引用清单：文档名 / 页码 / 行号，可复制。

**验收**：浏览器里完成「上传 → 提问 → 点 `[1]` → 落到正确页并高亮」全流程。

### P6 · 评测（3 天）

产出 `eval/golden.yaml`（30 条）与 `eval/bench.py`。

| 类型 | 条数 | 说明 |
| --- | ---: | --- |
| 事实型（单页可答） | 10 | "2022 年净利润是多少" |
| 对比型（跨页） | 5 | "两年毛利率变化及原因" |
| 表格型（CSV） | 6 | "某机场跑道长度" |
| 跨文档型 | 3 | 需要两份文件 |
| 超纲型（应拒答） | 6 | 语料里没有的问题 |

每条标注 `query / 必须命中的页或行 / 参考答案要点`。**调参期间不看测试集。**

指标口径（写进脚本，禁止手改数字）：

- `Recall@10` = 命中标注页数 / 标注页总数，按 query 平均。
- `引用可回查率` = 引用页码确实包含该陈述的比例。
- `拒答准确率` / `误拒率`。
- `检索段 p95`、`端到端 p95`（标注硬件与模型）。

对照实验（每组只改一个变量）：E2 纯向量 / 纯 FTS / 融合；E3 无重排 / 有重排；E4 无预算裁剪 /
预算组装。

`make bench` → 写 `bench.json` + 打印对照表。Recall@10 低于基线 → 非零退出。

### P7 · 交付（2 天）

- `docker compose up` 一条命令起：`pc`（fork 镜像）+ `app`，SQLite 卷持久化。
- README 补：架构图、演示 GIF、指标表、已知缺陷。
- 验收：新机器 clone → up → 10 分钟内能问答。

### 时间线

| 版本 | 内容 | 天数 |
| --- | --- | ---: |
| 全量 | P3+P4+P5+P6+P7 | ≈18 |
| 压缩 | P5 砍到 2 天（只留上传/问答/跳转）、P6 只做 E2/E3、黄金集 20 条、P7 1 天 | ≈10 |

## 6. 实现原理（关键几条）

**逐页 / 逐行摄取的身份与幂等**：页身份用 `(document_id, sha256, page)`，改一页产生新身份，旧引用
仍指向旧修订。幂等键 `doc_key + sha256`，前端重试不产生重复文档。

**多路查询**：长问题直接搜会被运行时的 admission 过滤掉正确答案（命中
`max(2, ceil(0.25*n))` 个不同词才留下）。所以要拆：原始问题、去功能词后的实词（按 6 个一组切
facet）、概念扩展词，分别查再并集。

**融合**：两路候选各自有分数但量纲不同，**不做加权**，用 RRF（按名次倒数求和）——无需校准、
对量纲不敏感。候选带通道标记，便于解释"这条为什么排前面"。

**重排**：HTTP 评分服务。按全局 rank 切片分批，**rank 不重新编号**；响应校验 rank 落本批、不重复、
分数有限、覆盖完整；用 `(-score, rank)` 做全排序，`usage.requests` 记实际请求数。

**字节预算装箱**：UTF-8 精确计量（`len(s.encode("utf-8"))`，含 citation 与 envelope 开销）。
先按边际收益贪心选整句；装不下时取**起点对齐**的前缀（二分字节预算，按 UTF-8 边界回退，保证不切
半个汉字）；最后用剩余预算向相邻句合并扩张，相邻片段合并成一条以省 citation 开销。

**引用不变量（写成测试）**：

1. `citation` 能唯一定位到一条 Source；
2. `text[start:end]` 与 Source 原文逐字一致；
3. 修订后旧 citation 仍解析到旧版本，不静默指向新内容。

## 7. 最小契约

```
POST /api/upload      multipart(file) → {document_id, pages|rows, status}
GET  /api/documents   → [{document_id, name, kind, pages, status, state}]
POST /api/ask         {question, max_bytes?} → {answer, citations[], refused, refusal_reason}
GET  /health          → {status}
```

`citations[]` 每项：`{source_type, document_id, document_sha256, page_number, primary_key_value,
entry_id, start, end}`。

SQLite 侧表（应用自有，与运行时库分离）：`documents`（含 `sha256` / `status` / `state`）、
`qa_logs`（问题、候选数、耗时、是否拒答）、`feedback`（可选，P2 以后）。

## 8. 已知约束（别重新踩）

| 约束 | 事实 |
| --- | --- |
| entry 大小上限 | 运行时拒绝 >8192 UTF-8 字节的 entry 文本。一页一条 entry 的固定 pipeline 成立的前提是单页 <8192 字节 |
| `search` admission | 命中 `max(2, ceil(0.25*n))` 个不同 query 词才留在候选；长问题必须切短 |
| 候选上限 | `candidate_limit = max(4*limit, 32)`，`limit` 别开太小 |
| FTS5 不分词干 | `unicode61` 分词，`operates` 不匹配 `operations`；词形只能在 query 侧补 |
| 源定义注册 | `/v1/source-definitions/register` 只收**声明式 manifest**，不加载插件代码；因此 PDF/CSV 的解析必须在本应用进程内完成，再把结果提交为 Source |
| 认证 | 非回环地址上的未认证 Server 拒绝启动；compose 只发布到 `127.0.0.1` |
| 页身份长度 | 来源 ID 超过 256 字符会退化哈希形式，反解时要走同一套 `_page_key` |

## 9. 风险与止损点

| 检查点 | 判据 | 不达标怎么办 |
| --- | --- | --- |
| D4 | `curl /api/ask` 能返回带引用的答案 | 停下 Web，先打通生成。没有它项目不成立 |
| D9 | `make bench` 有第一版数字 | 停下所有 UI 打磨，补评测。数字比界面值钱 |
| D12 | 引用可回查率 ≥0.90 | 达不到就改口径，写实测值，不要写"可溯源"这种绝对说法 |

三个常见失败模式：

1. **预算算漏**：citation 与 JSON envelope 的开销没计入，导致输出超预算或引用被截断。用边界测试
   钉死（刚好装满 / 差 1 字节）。
2. **拒答过猛**：只用一个分数阈值，误拒率飙高。三层拒答里第 ②③ 层要能各自单独关掉，便于归因。
3. **黄金集过拟合**：调参时反复看测试集。标完就锁，只在 `make bench` 时跑。

## 10. 新会话怎么接手

1. 读这份 README（尤其第 2、5、8 节）。
2. 确认两个仓库的现状：

   ```bash
   git -C ../powercontext_competition log --oneline -1     # 应为 resume 分支的声明 commit
   git log --oneline -1                        # 本仓库
   ```

3. 确认依赖与骨架能跑：

   ```bash
   uv sync && uv run pytest -q                 # 2 passed（health + 501 占位）
   ```

4. 从 **P3** 开始：先写 `ingest.py`，用一份真实财报跑通「上传 → 全页入库 → 回查页码」，再写
   `retrieve.py` 和 `pack.py`。**W1 结束必须有能跑的东西**，哪怕只有 5 份文档、没有重排、没有生成。
5. 每完成一个阶段，更新本 README 的验收数字。

## 11. 许可

Apache-2.0，见 [LICENSE](LICENSE)。上游 PowerContext 同样以 Apache-2.0 发布。

```
Built on OceanBase PowerContext (Apache-2.0). The four challenge modules referenced above are my
own work; this repository does not copy upstream source.
```
