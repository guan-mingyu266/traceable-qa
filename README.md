# traceable-qa

把财报 PDF 与业务表格，变成「每句话都能点到第几页、第几行」的问答入口。

> **状态：骨架（2026-11-16）。** 目录、依赖、编排、接口占位已就位；业务逻辑按下方路线图分阶段实现，当前
> `/api/*` 一律返回 `501`。

## 边界

本仓库是**应用层**，不含任何上游源码副本。

- 运行时：[PowerContext](https://github.com/oceanbase/powercontext)（Apache-2.0），以依赖形式引入，
  版本固定在 `guan-mingyu266/powercontext@resume`。
- 该 fork 中由本人实现的四个模块：CSV 行级采集、PDF 页级解析与证据检索、HTTP 重排、
  Embedding 请求合并与缓存。其余代码属于上游。
- 本仓库负责：检索编排、上下文按字节预算组装、生成与引用挂载、三层拒答、Web、评测、部署。

## 架构

```
┌──────────────────────────────────────────────┐
│ web/        上传 · 问答 · 引用跳转（pdf.js）    │
├──────────────────────────────────────────────┤
│ api.py      POST /api/upload /api/ask         │
│             GET  /api/documents /api/health   │
├──────────────────────────────────────────────┤
│ answer.py   生成 → 引用挂载 → 后置校验 → 拒答   │
│ pack.py     max_bytes 装箱 · 片段 start/end   │
│ retrieve.py 多路 query → RRF(fts, vector) → 重排│
│ ingest.py   PDF/CSV → Source（复用 fork 解析）  │
├──────────────────────────────────────────────┤
│ pc 容器     PowerContext Server（SQLite + 卷）  │
└──────────────────────────────────────────────┘
```

## 目录

```
src/qa/          ingest · retrieve · pack · answer · api
src/qa/web/      单页前端（上传 / 问答 / 引用跳转）
eval/            黄金集与 bench（Recall@10 / 引用可回查率 / 拒答率 / p95）
tests/           行为测试
compose.yaml     pc + app（+ 可选 ollama）
```

## 路线图

- [ ] **P3 检索桥**：`ingest.py` `retrieve.py` `pack.py`；验收 `make probe` 打印 Recall@10 与每问字节数
- [ ] **P4 生成**：`answer.py`；验收引用编号全部落在候选集内、超纲问题该拒则拒
- [ ] **P5 全栈**：`web/`；验收浏览器里上传 → 提问 → 点 `[1]` 跳到正确页并高亮
- [ ] **P6 评测**：`eval/` + `make bench`；输出 `bench.json` 与对照表
- [ ] **P7 交付**：`docker compose up` 一键起、README 指标表、演示 GIF

## 开发

```bash
uv sync                 # 只装应用层依赖，可离线
uv sync --extra runtime # 额外装 PowerContext（需 fork 的 resume 分支已推送）
uv run pytest -q
```

`pc` 服务使用从 fork 构建的镜像（见 `Makefile` 的 `pc-image` 目标），因此仓库需与本地的
`powercontext` 检出目录并列，或改用已发布的镜像并相应调整 `compose.yaml`。

## 许可

Apache-2.0，见 [LICENSE](LICENSE)。上游 PowerContext 同样以 Apache-2.0 发布。
