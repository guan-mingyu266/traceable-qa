"""P3 摄取：PDF 页 / CSV 行 → Source + 可检索的 Memory entry，并记账文档状态。

四步，顺序不能换：

1. 解析（fork 的 ``parse_pdf_document`` / ``parse_csv_document``），失败不留半条记录；
2. 一页 / 一行提交一条 Source，身份由 fork 的 SourceDefinition 在本地算出；
3. 一页 / 一行 ``remember`` 一条 entry，文本与 Source 逐字一致（``text == entry.text[start:end]``）；
4. 写 ``documents`` / ``document_units``：pending → ready/failed，失败可续跑。

为什么 entry 归摄取管：``search`` 只返回 Memory entry；运行时的自动管线走 ``extract``，
要生成模型，且抽出的句子不再逐字等于原文，会破掉引用不变量。P3 用“append 原文”的固定管线。

取舍：单页 > 8192 UTF-8 字节直接拒收（运行时 entry 上限），不截断、不跳过。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final
from uuid import UUID, uuid5

from powercontext.client import PowerContextClient
from powercontext.client.errors import ServerResponseError
from powercontext.http import (
    CreateScopeRequest,
    RegisterSourceDefinitionRequest,
    RememberMemoryRequest,
    SubmitSourceObservationRequest,
)
from powercontext.http import SourceDefinitionManifest as HttpSourceDefinitionManifest
from powercontext.http import SourceObservation as HttpSourceObservation
from powercontext.sources import SourceDefinitionRegistry, manifest_for_definition, project_source_for_transport
from powercontext.sources.csv_rows import CSV_ROW_SOURCE_DEFINITION, CSVDocumentInput, CSVRowInput, parse_csv_document
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    PDFPageInput,
    parse_pdf_document,
)

__all__ = [
    "DocumentKind",
    "DocumentRef",
    "DocumentStatus",
    "DocumentStore",
    "IngestError",
    "IngestRejected",
    "IngestResult",
    "IngestService",
]

MAX_ENTRY_BYTES: Final = 8192

# 同一个 (kind, 文件名, sha256) 必须每次派生出同一个 document_id，重试才不会变成新文档。
_DOCUMENT_NAMESPACE: Final = UUID("5b0e4f4c-6d5a-5a1e-9a3c-6f1f0f9d2a71")
_SCOPE_KEY: Final = "traceable-qa.scope.v1"

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    doc_key     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    status      TEXT NOT NULL,
    units       INTEGER NOT NULL,
    entries     INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    updated_at  TEXT NOT NULL,
    UNIQUE (doc_key, kind, sha256)
);

CREATE TABLE IF NOT EXISTS document_units (
    document_id TEXT NOT NULL,
    unit_key    TEXT NOT NULL,
    entry_id    TEXT,
    status      TEXT NOT NULL,
    PRIMARY KEY (document_id, unit_key)
);
"""


class DocumentKind(StrEnum):
    PDF = "pdf"
    CSV = "csv"


class DocumentStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class IngestError(Exception):
    """摄取失败。``code`` 稳定，调用方按它映射状态码。"""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail or code)


class IngestRejected(IngestError):
    """输入本身不可用（解析失败、超限、主键不合法）。"""


@dataclass(frozen=True, slots=True)
class DocumentRef:
    """``documents`` 表的一行，也是 /api/documents 的输出形状。"""

    document_id: str
    name: str
    kind: DocumentKind
    status: DocumentStatus
    units: int
    entries: int
    error: str | None


@dataclass(frozen=True, slots=True)
class IngestResult:
    document: DocumentRef
    reused: bool


@dataclass(frozen=True, slots=True)
class _Unit:
    key: str
    source_type: str
    text: str
    payload: PDFPageInput | CSVRowInput


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _canonical(value: str) -> str:
    """与运行时 ``normalize_text`` 对齐（NFC + strip），Source 与 entry 都用它。"""

    return unicodedata.normalize("NFC", value).strip()


def _document_key(name: str) -> str:
    key = unicodedata.normalize("NFC", name.replace("\\", "/").rsplit("/", 1)[-1]).strip()
    if not key:
        raise IngestRejected("empty_name", "文件名不能为空")
    return key


class DocumentStore:
    """应用自有的 SQLite 元数据（与运行时的库分开，见 plan 第 7 节）。"""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def find(self, *, doc_key: str, kind: DocumentKind, sha256: str) -> DocumentRef | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE doc_key = ? AND kind = ? AND sha256 = ?",
                (doc_key, kind.value, sha256),
            ).fetchone()
        return None if row is None else _document_ref(row)

    def list_documents(self) -> list[DocumentRef]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM documents ORDER BY updated_at DESC").fetchall()
        return [_document_ref(row) for row in rows]

    def open(self, *, document_id: str, doc_key: str, kind: DocumentKind, name: str, sha256: str, units: int) -> None:
        """置为 pending；已 ready 的行不降级（调用方在此之前判掉）。"""

        with self._connect() as connection:
            connection.execute(
                "INSERT INTO documents (document_id, doc_key, kind, name, sha256, status, units, entries, error, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, ?) "
                "ON CONFLICT(document_id) DO UPDATE SET status = excluded.status, units = excluded.units, "
                "error = NULL, updated_at = excluded.updated_at",
                (document_id, doc_key, kind.value, name, sha256, DocumentStatus.PENDING.value, units, _now()),
            )

    def units(self, document_id: str) -> dict[str, str]:
        """已完成的单元：``unit_key -> status``（ready / empty）。"""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT unit_key, status FROM document_units WHERE document_id = ?",
                (document_id,),
            ).fetchall()
        return {str(row["unit_key"]): str(row["status"]) for row in rows}

    def save_unit(self, *, document_id: str, unit_key: str, status: str, entry_id: str | None) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO document_units (document_id, unit_key, entry_id, status) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(document_id, unit_key) DO UPDATE SET "
                "entry_id = excluded.entry_id, status = excluded.status",
                (document_id, unit_key, entry_id, status),
            )

    def finish(self, document_id: str) -> DocumentRef:
        with self._connect() as connection:
            connection.execute(
                "UPDATE documents SET status = ?, error = NULL, updated_at = ?, "
                "entries = (SELECT COUNT(*) FROM document_units WHERE document_id = ? AND entry_id IS NOT NULL) "
                "WHERE document_id = ?",
                (DocumentStatus.READY.value, _now(), document_id, document_id),
            )
            row = connection.execute("SELECT * FROM documents WHERE document_id = ?", (document_id,)).fetchone()
        if row is None:  # pragma: no cover
            raise IngestError("store_inconsistent", "documents 行不存在")
        return _document_ref(row)

    def fail(self, document_id: str, detail: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE documents SET status = ?, error = ?, updated_at = ? WHERE document_id = ?",
                (DocumentStatus.FAILED.value, detail[:2000], _now(), document_id),
            )


def _document_ref(row: sqlite3.Row) -> DocumentRef:
    return DocumentRef(
        document_id=str(row["document_id"]),
        name=str(row["name"]),
        kind=DocumentKind(row["kind"]),
        status=DocumentStatus(row["status"]),
        units=int(row["units"]),
        entries=int(row["entries"]),
        error=None if row["error"] is None else str(row["error"]),
    )


class IngestService:
    """摄取入口。一个进程建一个实例，运行时状态懒加载。"""

    def __init__(
        self,
        *,
        client: PowerContextClient,
        store: DocumentStore,
        scope_title: str = "traceable-qa",
        scope_summary: str = "财报 PDF 与业务 CSV 的可溯源问答语料",
        source_concurrency: int = 8,
    ) -> None:
        self._client = client
        self._store = store
        self._scope_title = scope_title
        self._scope_summary = scope_summary
        self._registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION, CSV_ROW_SOURCE_DEFINITION))
        self._semaphore = asyncio.Semaphore(max(1, source_concurrency))
        self._entry_lock = asyncio.Lock()
        self._ready_lock = asyncio.Lock()
        self._scope_id: str | None = None
        self._definitions_registered = False
        self._store_ready = False

    # --- 对外 ---------------------------------------------------------------------

    def documents(self) -> list[DocumentRef]:
        self._ensure_store()
        return self._store.list_documents()

    async def ingest_pdf(self, *, name: str, content: bytes) -> IngestResult:
        return await self._ingest(kind=DocumentKind.PDF, name=name, content=content, primary_key=None)

    async def ingest_csv(self, *, name: str, content: bytes, primary_key: str) -> IngestResult:
        return await self._ingest(kind=DocumentKind.CSV, name=name, content=content, primary_key=primary_key)

    # --- 主流程 -------------------------------------------------------------------

    async def _ingest(self, *, kind: DocumentKind, name: str, content: bytes, primary_key: str | None) -> IngestResult:
        if not content:
            raise IngestRejected("empty_file", "文件内容为空")
        doc_key = _document_key(name)
        sha256 = hashlib.sha256(content).hexdigest()
        document_id = str(uuid5(_DOCUMENT_NAMESPACE, f"{kind.value}:{doc_key}:{sha256}"))

        # 先解析再落库：解析失败不留半条记录。
        units = self._parse(kind=kind, document_id=document_id, content=content, primary_key=primary_key)

        self._ensure_store()
        existing = self._store.find(doc_key=doc_key, kind=kind, sha256=sha256)
        if existing is not None and existing.status is DocumentStatus.READY:
            return IngestResult(document=existing, reused=True)

        self._store.open(
            document_id=document_id, doc_key=doc_key, kind=kind, name=doc_key, sha256=sha256, units=len(units)
        )
        try:
            scope_id = await self._ensure_runtime()
            await self._ingest_units(document_id=document_id, units=units, scope_id=scope_id)
        except IngestError as error:
            self._store.fail(document_id, f"{error.code}: {error.detail}" if error.detail else error.code)
            raise
        except ServerResponseError as error:
            self._store.fail(document_id, f"http_{error.status_code}")
            raise _translate(error) from error
        return IngestResult(document=self._store.finish(document_id), reused=False)

    def _parse(
        self, *, kind: DocumentKind, document_id: str, content: bytes, primary_key: str | None
    ) -> tuple[_Unit, ...]:
        if kind is DocumentKind.PDF:
            return self._parse_pdf(document_id=document_id, content=content)
        if not primary_key:
            raise IngestRejected("missing_primary_key", "CSV 需要指定 primary_key（列名）")
        return self._parse_csv(document_id=document_id, content=content, primary_key=primary_key)

    def _parse_pdf(self, *, document_id: str, content: bytes) -> tuple[_Unit, ...]:
        try:
            pages = parse_pdf_document(PDFDocumentInput(document_id=document_id, content=content))
        except ValueError as error:
            raise IngestRejected("pdf_unreadable", str(error)) from error

        units: list[_Unit] = []
        oversized: list[int] = []
        for page in pages:
            text = _canonical(page.text)
            if len(text.encode("utf-8")) > MAX_ENTRY_BYTES:
                oversized.append(page.page_number)
                continue
            units.append(
                _Unit(
                    key=str(page.page_number),
                    source_type="pdf-page",
                    text=text,
                    payload=PDFPageInput(
                        document_id=document_id,
                        document_sha256=page.document_sha256,
                        page_number=page.page_number,
                        text=text,
                    ),
                )
            )
        if oversized:
            listed = "、".join(str(number) for number in oversized[:10])
            raise IngestRejected(
                "page_too_large", f"第 {listed} 页超过 {MAX_ENTRY_BYTES} 字节上限；运行时不接受这么大的 entry"
            )
        return tuple(units)

    def _parse_csv(self, *, document_id: str, content: bytes, primary_key: str) -> tuple[_Unit, ...]:
        try:
            rows = parse_csv_document(
                CSVDocumentInput(document_id=document_id, primary_key=primary_key, content=content)
            )
        except ValueError as error:
            raise IngestRejected("csv_unreadable", str(error)) from error

        units: list[_Unit] = []
        for row in rows:
            # 与 fork 的 TextEvidence 投影同一份文本：字段顺序、原值、紧凑 JSON 都不动。
            row_text = json.dumps(
                dict(zip(row.columns, row.values, strict=True)),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            text = _canonical(row_text)
            if len(text.encode("utf-8")) > MAX_ENTRY_BYTES:
                raise IngestRejected("row_too_large", f"主键 {row.primary_key_value!r} 这一行超过上限")
            units.append(_Unit(key=row.primary_key_value, source_type="csv-row", text=text, payload=row))
        if not units:
            raise IngestRejected("empty_csv", "CSV 只有表头，没有数据行")
        return tuple(units)

    async def _ingest_units(self, *, document_id: str, units: Sequence[_Unit], scope_id: str) -> None:
        done = self._store.units(document_id)
        todo = [unit for unit in units if done.get(unit.key) not in {"ready", "empty"}]
        if not todo:
            return

        # Source 互相独立 → 有界并发；entry 是对同一个 Memory 头的 CAS → 串行、按页序。
        async def submit(unit: _Unit) -> None:
            async with self._semaphore:
                await self._submit_source(scope_id=scope_id, unit=unit)

        await asyncio.gather(*(submit(unit) for unit in todo))

        async with self._entry_lock:
            for unit in todo:
                if not unit.text:
                    # 扫描页 / 空行：Source 占位，但没有可检索文本，不写 entry。
                    self._store.save_unit(document_id=document_id, unit_key=unit.key, status="empty", entry_id=None)
                    continue
                entry_id = await self._remember(scope_id=scope_id, document_id=document_id, unit=unit)
                self._store.save_unit(document_id=document_id, unit_key=unit.key, status="ready", entry_id=entry_id)

    # --- 运行时交互 ----------------------------------------------------------------

    async def _ensure_runtime(self) -> str:
        async with self._ready_lock:
            if self._scope_id is None:
                scope = await self._client.create_scope(
                    CreateScopeRequest(title=self._scope_title, summary=self._scope_summary, idempotency_key=_SCOPE_KEY)
                )
                self._scope_id = scope.scope_id
            if not self._definitions_registered:
                for definition in self._registry.definitions:
                    manifest = manifest_for_definition(definition)
                    await self._client.register_source_definition(
                        RegisterSourceDefinitionRequest(
                            manifest=HttpSourceDefinitionManifest.model_validate(
                                manifest.model_dump(mode="json", by_alias=True)
                            )
                        )
                    )
                self._definitions_registered = True
            return self._scope_id

    async def _submit_source(self, *, scope_id: str, unit: _Unit) -> None:
        try:
            source = await self._registry.resolve(unit.payload)
            observation = project_source_for_transport(self._registry, source)
        except ValueError as error:  # 例如 CSV 主键太长，source_id 超过 256 字符
            raise IngestRejected("source_rejected", str(error)) from error
        await self._client.submit_source_observation(
            SubmitSourceObservationRequest(
                scope_id=scope_id,
                observation=HttpSourceObservation.model_validate(observation.model_dump(mode="json")),
            )
        )

    async def _remember(self, *, scope_id: str, document_id: str, unit: _Unit) -> str:
        response = await self._client.remember_memory(
            RememberMemoryRequest(
                scope_id=scope_id, kind=unit.source_type, text=unit.text, reason=f"{document_id}#{unit.key}"[:512]
            )
        )
        if response.entry is None:
            raise IngestError("entry_missing", "remember 返回了空 entry")
        return response.entry.citation.entry_id

    # --- 杂项 ---------------------------------------------------------------------

    def _ensure_store(self) -> None:
        if not self._store_ready:
            self._store.initialize()
            self._store_ready = True


def _translate(error: ServerResponseError) -> IngestError:
    """4xx 是请求本身不对；401/403 是部署问题。"""

    code = error.code or f"http_{error.status_code}"
    if error.status_code in {401, 403}:
        return IngestError("auth_failed", error.server_message or code)
    if 400 <= error.status_code < 500:
        return IngestRejected(code, error.server_message or code)
    return IngestError(code, error.server_message or code)