"""P3 检索桥的下半段：问题 → 多路 query × (FTS, 向量) → 加权 RRF → 候选列表。

plan 的方案 B：融合发生在应用层，而不是交给运行时的 ``hybrid``。

1. **拆查询**：原问题 + 去功能词后按 6 个实词一组的 facet，最多 6 路；
2. **双通道**：每路 query 分别以 ``mode="fts"`` 和 ``mode="vector"`` 各搜一次，
   两路各自有名次，分数不跨路比较；
3. **加权 RRF**：``score += w / (k + rank)``，``w_fts = 1 - alpha``、``w_vec = alpha``，
   ``k = 60``。α 就是 plan 里要扫的那个参数，α=0 退化为纯 FTS，α=1 退化为纯向量
   （这两种极端会直接省掉没用的那一路请求）。
4. **降级**：向量通道在没配 embedding / 旧 entry 缺向量时返回 422
   ``capability_not_supported``；此时保留 FTS 结果并把 ``degraded`` 标为真，
   不静默假装融合成功——bench 里看到 ``degraded`` 就知道这组数字不能当融合结果。

单独成型的对照模式（E2 用，跑同一份黄金集）：

- ``mode="fts"``：只走词法；
- ``mode="vector"``：只走向量，不可用时直接报 ``vector_unavailable``；
- ``mode="hybrid"``：一次 ``search(mode="hybrid")``，交给运行时自己融（对照组，
  和本文件的 RRF 不是同一件事，不能混着报）；
- ``mode="auto"``：一次 ``search(mode="auto")``，即 P3 早先的行为。

刻意没做的：重排（分批、跨批次全局排序、usage 记账）与概念扩展词表，都留到候选稳定之后。
"""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Final, Literal

from powercontext.client import PowerContextClient
from powercontext.client.errors import ClientError, ServerResponseError
from powercontext.http import CreateScopeRequest, MemorySearchMode, SearchMemoryRequest

__all__ = [
    "Candidate",
    "DEFAULT_ALPHA",
    "Retrieval",
    "RetrieveError",
    "RetrieveService",
    "SearchMode",
    "plan_queries",
]

SearchMode = Literal["rrf", "fts", "vector", "hybrid", "auto"]

# 一路 facet 最多 6 个实词单元（plan 第 6 节），最多 6 路查询。
WINDOW: Final = 6
MAX_QUERIES: Final = 6
# RRF 的平滑常数，论文与 plan 都用 60。
RRF_K: Final = 60
# 向量通道的权重；0 = 纯 FTS，1 = 纯向量。
DEFAULT_ALPHA: Final = 0.5
# 与 ingest 的 _SCOPE_KEY 必须一致：两层服务要落在同一个 scope 里。
_SCOPE_KEY: Final = "traceable-qa.scope.v1"

_SEGMENT = re.compile(r"[A-Za-z]+|\d+(?:\.\d+)?|[\u4e00-\u9fff]+")
# 中文没有词边界：先用单字虚词断开（的/了/是…），再从两端削掉多字疑问短语。
_CJK_FUNCTION = re.compile(r"[的了是在和与或吗呢啊]")
_STOPWORDS: Final = frozenset(
    {
        "的", "了", "是", "在", "和", "与", "或", "吗", "呢", "啊",
        "请问", "帮我", "查一下", "查查", "是多少", "多少", "什么", "怎么", "怎么样",
        "为什么", "为何", "如何", "是否", "哪些", "哪个", "以及", "还有", "这个", "那个", "一下",
        "the", "a", "an", "of", "in", "on", "for", "to", "and", "or", "is", "are", "was", "were",
        "what", "how", "which", "please", "tell", "me", "about", "with", "by", "from", "that", "this",
    }
)
_PHRASE_STOPWORDS: Final = tuple(
    sorted((word for word in _STOPWORDS if not word.isascii() and len(word) > 1), key=len, reverse=True)
)


class RetrieveError(Exception):
    """检索失败。``code`` 稳定，调用方按它映射状态码。"""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail or code)


@dataclass(frozen=True, slots=True)
class Candidate:
    """一条候选。``entry_id`` 是运行时 Memory entry 的身份，引用就靠它回查。"""

    entry_id: str
    entry_version_id: str
    text: str
    score: float
    rank: int
    matched_by: tuple[str, ...]
    queries: tuple[str, ...]
    modes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Retrieval:
    """一次检索的完整结果，供 probe / bench 打印。"""

    question: str
    queries: tuple[str, ...]
    candidates: tuple[Candidate, ...]
    modes: tuple[str, ...]
    failed_queries: tuple[str, ...]
    elapsed_ms: float
    alpha: float
    degraded: bool


@dataclass(slots=True)
class _Seen:
    """融合过程中的累加器。"""

    entry_version_id: str
    text: str
    score: float = 0.0
    best_rank: int = 0
    matched: set[str] = field(default_factory=set)
    queries: set[str] = field(default_factory=set)
    modes: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _List:
    """一路待融合的结果：哪个 query、哪个通道、权重多少。"""

    query: str
    channel: str
    weight: float
    hits: list


def _trim_phrases(piece: str) -> str:
    """从两端削掉「多少 / 是多少 / 请问」这类多字疑问短语。"""

    changed = True
    while changed:
        changed = False
        for word in _PHRASE_STOPWORDS:
            if piece.startswith(word):
                piece, changed = piece[len(word) :], True
            elif piece.endswith(word):
                piece, changed = piece[: -len(word)], True
    return piece


def _units(question: str) -> list[str]:
    """切出实词单元：中文串 / 英文词 / 数字，去掉功能词与疑问短语。"""

    normalized = unicodedata.normalize("NFC", question)
    units: list[str] = []
    for segment in _SEGMENT.findall(normalized):
        if segment.isascii():
            word = segment.casefold()
            if word not in _STOPWORDS and len(word) > 1:
                units.append(word)
            continue
        for piece in _CJK_FUNCTION.split(segment):
            piece = _trim_phrases(piece).strip()
            if len(piece) > 1:
                units.append(piece)
    return units


def plan_queries(question: str, *, window: int = WINDOW, max_queries: int = MAX_QUERIES) -> tuple[str, ...]:
    """原问题 + facet 查询，去重后不超过 ``max_queries`` 路。"""

    raw = unicodedata.normalize("NFC", question).strip()
    if not raw:
        return ()

    queries = [raw]
    units = _units(raw)
    chunks = (" ".join(units[start : start + window]) for start in range(0, len(units), window))
    for chunk in chunks:
        if chunk and chunk not in queries:
            queries.append(chunk)
        if len(queries) >= max_queries:
            break
    return tuple(queries)


def _channels(mode: SearchMode, alpha: float) -> tuple[tuple[str, float], ...]:
    """把模式与 α 翻译成「通道 + 权重」；两端点直接省掉白跑的那一路。"""

    if mode == "rrf":
        if alpha <= 0.0:
            return (("fts", 1.0),)
        if alpha >= 1.0:
            return (("vector", 1.0),)
        return (("fts", 1.0 - alpha), ("vector", alpha))
    return ((mode, 1.0),)


class RetrieveService:
    """检索入口。与 ingest 共用同一个 scope，``scope_id`` 可以显式传入以避免多一次 create。"""

    def __init__(
        self,
        *,
        client: PowerContextClient,
        scope_id: str | None = None,
        scope_title: str = "traceable-qa",
        scope_summary: str = "财报 PDF 与业务 CSV 的可溯源问答语料",
        mode: SearchMode = "rrf",
        alpha: float = DEFAULT_ALPHA,
        per_query_limit: int = 20,
        concurrency: int = 6,
    ) -> None:
        self._client = client
        self._scope_id = scope_id
        self._scope_title = scope_title
        self._scope_summary = scope_summary
        self._mode = mode
        self._alpha = alpha
        self._per_query_limit = per_query_limit
        self._semaphore = asyncio.Semaphore(max(1, concurrency))

    async def retrieve(
        self,
        question: str,
        *,
        limit: int = 10,
        mode: SearchMode | None = None,
        alpha: float | None = None,
    ) -> Retrieval:
        queries = plan_queries(question)
        if not queries:
            raise RetrieveError("empty_question", "问题为空")
        mode = self._mode if mode is None else mode
        alpha = self._alpha if alpha is None else alpha
        channels = _channels(mode, alpha)

        started = time.perf_counter()
        scope_id = await self._ensure_scope()
        requests = [
            (query, channel, weight, self._search(scope_id, query, channel))
            for query in queries
            for channel, weight in channels
        ]
        results = await asyncio.gather(*(item[3] for item in requests), return_exceptions=True)
        elapsed_ms = (time.perf_counter() - started) * 1000

        lists: list[_List] = []
        failures: list[str] = []
        used_modes: set[str] = set()
        capability_failures = 0
        for (query, channel, weight, _), result in zip(requests, results, strict=True):
            if isinstance(result, BaseException):
                failures.append(f"{query}｜{channel}：{_describe(result)}")
                if _is_capability_error(result):
                    capability_failures += 1
                continue
            lists.append(_List(query=query, channel=_resolved_mode(result, channel), weight=weight, hits=result.hits))
            used_modes.add(_resolved_mode(result, channel))

        if not lists:
            raise self._failure(failures, mode=mode, capability_failures=capability_failures)

        degraded = capability_failures > 0 and mode == "rrf"
        return Retrieval(
            question=question.strip(),
            queries=queries,
            candidates=self._fuse(lists, limit=limit),
            modes=tuple(sorted(used_modes)),
            failed_queries=tuple(failures),
            elapsed_ms=elapsed_ms,
            alpha=alpha,
            degraded=degraded,
        )

    async def _search(self, scope_id: str, query: str, channel: str):
        async with self._semaphore:
            return await self._client.search_memory(
                SearchMemoryRequest(
                    scope_id=scope_id,
                    query=query,
                    limit=self._per_query_limit,
                    mode=MemorySearchMode(channel),
                )
            )

    async def _ensure_scope(self) -> str:
        if self._scope_id is None:
            scope = await self._client.create_scope(
                CreateScopeRequest(
                    title=self._scope_title,
                    summary=self._scope_summary,
                    idempotency_key=_SCOPE_KEY,
                )
            )
            self._scope_id = scope.scope_id
        return self._scope_id

    def _fuse(self, lists: list[_List], *, limit: int) -> tuple[Candidate, ...]:
        seen: dict[str, _Seen] = {}
        for item in lists:
            for rank, hit in enumerate(item.hits, start=1):
                entry_id = hit.citation.entry_id
                current = seen.get(entry_id)
                if current is None:
                    current = _Seen(
                        entry_version_id=hit.citation.entry_version_id,
                        text=hit.text,
                        best_rank=rank,
                    )
                    seen[entry_id] = current
                current.best_rank = min(current.best_rank, rank)
                current.score += item.weight / (RRF_K + rank)
                current.matched.update(matched.value for matched in hit.matched_by)
                current.queries.add(item.query)
                current.modes.add(item.channel)

        ordered = sorted(seen.items(), key=lambda pair: (-pair[1].score, pair[1].best_rank, pair[0]))
        return tuple(
            Candidate(
                entry_id=entry_id,
                entry_version_id=value.entry_version_id,
                text=value.text,
                score=value.score,
                rank=rank,
                matched_by=tuple(sorted(value.matched)),
                queries=tuple(sorted(value.queries)),
                modes=tuple(sorted(value.modes)),
            )
            for rank, (entry_id, value) in enumerate(ordered[:limit], start=1)
        )

    @staticmethod
    def _failure(failures: list[str], *, mode: SearchMode, capability_failures: int) -> RetrieveError:
        if mode == "vector" and capability_failures and capability_failures == len(failures):
            return RetrieveError(
                "vector_unavailable",
                "向量通道不可用：确认 Server 配了 embedding 模型，且已有 entry 都带向量",
            )
        auth = any("401" in failure or "403" in failure for failure in failures)
        code = "auth_failed" if auth else "search_failed"
        return RetrieveError(code, "；".join(failures[:3]) or "没有可用的查询结果")


def _resolved_mode(response: object, requested: str) -> str:
    """响应里的 ``mode`` 是运行时实际用的通道；拿不到就退回请求的通道名。"""

    mode = getattr(response, "mode", None)
    value = getattr(mode, "value", None)
    return requested if value is None else str(value)


def _is_capability_error(error: BaseException) -> bool:
    if not isinstance(error, ServerResponseError):
        return False
    return error.code == "capability_not_supported" or error.status_code == 422


def _describe(error: BaseException) -> str:
    if isinstance(error, ServerResponseError):
        return f"http_{error.status_code}: {error.server_message or error}"
    if isinstance(error, ClientError):
        return f"{type(error).__name__}: {error}"
    return f"{type(error).__name__}: {error}"
