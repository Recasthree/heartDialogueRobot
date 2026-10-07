"""Scoped, expiring local uploads. SQL stores metadata, never public evidence."""
import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from ..privacy import redact_text
from .worker import file_kind


class AttachmentError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


class AttachmentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    purpose: Literal["patient_context", "reference"] | None = None
    page_texts: dict[int, str] | None = None
    confirmed_pages: list[int] = Field(default_factory=list)
    retry: bool = False
    selected_pages: list[int] | None = Field(default=None, max_length=20)


class SaveAttachmentFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    target_case_id: str = Field(min_length=1, max_length=100)
    facts: list[str] = Field(min_length=1, max_length=10)


def scope_key(session_id, case_id=None, user_id=None):
    if not session_id or len(session_id) > 200:
        raise AttachmentError("session_id_required")
    return hashlib.sha256(json.dumps([session_id, case_id or "", user_id or ""], ensure_ascii=True).encode()).hexdigest()


def decorate_pages(pages, purpose):
    for page in pages:
        text = page["text"]
        critical = [line for line in text.splitlines() if re.search(
            r"\d|剂量|用药|药名|处方|服用|毫克|单位|检验|参考范围|血压|心率|mg\b|mmol", line, re.I)]
        low = any(b.get("confidence", 1) < .85 for b in page.get("blocks", []))
        # A whole-page review is the fallback when field localization is uncertain.
        # There is no validated offline medical NER capable of ruling out a
        # medication name without numbers. Conservatively confirm the whole page.
        required = True
        page.update(critical_lines=critical[:40], low_confidence=low,
                    requires_confirmation=required, confirmed=not required)
    return pages


class AttachmentStore:
    def __init__(self, root, model_dir, ttl=86400, timeout=120):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "attachments.sqlite3"
        self.model_dir, self.ttl, self.timeout = str(model_dir), ttl, timeout
        self.jobs = {}
        self.parsers = {}
        self.lock = asyncio.Semaphore(1)
        with self.connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS attachments(id TEXT PRIMARY KEY, scope TEXT NOT NULL, expires REAL NOT NULL, payload TEXT NOT NULL)")
            rows = conn.execute("SELECT id,payload FROM attachments").fetchall()
            for aid, raw in rows:
                item = json.loads(raw)
                if item["status"] in {"queued", "parsing"}:
                    item.update(status="failed", error="interrupted_restart_retry")
                    conn.execute("UPDATE attachments SET payload=? WHERE id=?", (json.dumps(item), aid))

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db, timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA secure_delete=ON")
            with conn:
                yield conn
        finally:
            conn.close()

    def _file(self, aid):
        if not re.fullmatch(r"att-[a-f0-9]{32}", aid):
            raise AttachmentError("attachment_not_found", 404)
        path = (self.root / (aid + ".bin")).resolve()
        if path.parent != self.root or path.is_symlink():
            raise AttachmentError("invalid_attachment_path")
        return path

    def _save(self, item):
        with self.connect() as conn:
            conn.execute("UPDATE attachments SET payload=? WHERE id=?", (json.dumps(item, ensure_ascii=False), item["id"]))

    def get(self, aid, scope):
        self._file(aid)
        with self.connect() as conn:
            row = conn.execute("SELECT scope,expires,payload FROM attachments WHERE id=?", (aid,)).fetchone()
        if not row or row[0] != scope:
            raise AttachmentError("attachment_not_found", 404)
        if row[1] <= time.time():
            raise AttachmentError("attachment_expired", 410)
        return json.loads(row[2])

    def create(self, data, filename, scope, purpose="patient_context", selected_pages=None):
        if not data or len(data) > 20 * 1024 * 1024:
            raise AttachmentError("file_size_limit")
        if purpose not in {"patient_context", "reference"}:
            raise AttachmentError("invalid_attachment_purpose")
        try:
            kind = file_kind(data)
        except ValueError as exc:
            raise AttachmentError(str(exc)) from exc
        aid = "att-" + uuid4().hex
        item = {"id": aid, "filename": redact_text(Path(filename or "attachment").name)[0][:150],
                "kind": kind, "purpose": purpose, "status": "queued", "version": 1,
                "sha256": hashlib.sha256(data).hexdigest(), "created_at": time.time(),
                "expires_at": time.time() + self.ttl, "pages": [], "selected_pages": selected_pages,
                "error": None, "byte_size": len(data)}
        self._file(aid).write_bytes(data)
        with self.connect() as conn:
            conn.execute("INSERT INTO attachments VALUES(?,?,?,?)", (aid, scope, item["expires_at"], json.dumps(item)))
        return item

    async def parse(self, aid, scope, pages=None, force_ocr=False):
        if aid in self.parsers:
            raise AttachmentError("attachment_still_parsing", 409)
        task = asyncio.create_task(self._parse(aid, scope, pages, force_ocr))
        self.parsers[aid] = task
        try:
            return await task
        except asyncio.CancelledError:
            # Deleting an attachment cancels its child parser, not the HTTP request.
            # Return a tool result so MCP can close its response stream normally.
            if not asyncio.current_task().cancelling():
                return {'id':aid, 'status':'failed', 'error':'parse_cancelled'}
            raise
        finally:
            if self.parsers.get(aid) is task:
                self.parsers.pop(aid, None)

    async def _parse(self, aid, scope, pages=None, force_ocr=False):
        async with self.lock:
            item = self.get(aid, scope)
            if item["status"] == "ready" and pages is None and not force_ocr:
                return {"id": aid, "status": "ready", "version": item["version"]}
            item.update(status="parsing", error=None)
            self._save(item)
            process = None
            try:
                env = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "PYTHONPATH"}}
                env["PYTHONIOENCODING"] = "utf-8"
                process = await asyncio.create_subprocess_exec(sys.executable, "-m", "app.attachments.worker",
                    str(self._file(aid)), self.model_dir, json.dumps(pages or item.get("selected_pages")), "1" if force_ocr else "0",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env,
                    **({"creationflags": 0x08000000} if os.name == "nt" else {}))
                stdout, _ = await asyncio.wait_for(process.communicate(), self.timeout)
                value = json.loads(stdout)
                if not value.get("ok"):
                    raise AttachmentError(value.get("error", "parse_failed"))
                parsed = value["data"]
                item.update(parsed, status="ready", pages=decorate_pages(parsed["pages"], item["purpose"]))
            except asyncio.CancelledError:
                item.update(status="failed", error="parse_cancelled")
                self._save(item)
                raise
            except Exception as exc:
                item.update(status="failed", error=exc.code if isinstance(exc, AttachmentError) else type(exc).__name__)
            finally:
                if process and process.returncode is None:
                    process.kill()
                    await process.wait()
            self._save(item)
            return {"id": aid, "status": item["status"], "version": item["version"], "error": item.get("error")}

    def patch(self, aid, scope, patch):
        item = self.get(aid, scope)
        if patch.version != item["version"]:
            raise AttachmentError("attachment_version_conflict", 409)
        if item["status"] in {"queued", "parsing"}:
            raise AttachmentError("attachment_still_parsing", 409)
        changed = bool(patch.page_texts is not None or patch.purpose and patch.purpose != item["purpose"])
        if patch.purpose:
            item["purpose"] = patch.purpose
        if patch.page_texts is not None:
            by_page = {p["page"]: p for p in item["pages"]}
            for number, text in patch.page_texts.items():
                if number not in by_page or not text.strip() or len(text) > 16000:
                    raise AttachmentError("invalid_page_revision")
                by_page[number]["text"] = text
                by_page[number]["method"] = "user_corrected"
        if changed:
            item["version"] += 1
            decorate_pages(item["pages"], item["purpose"])
            # Corrections require a separate confirmation of the NEW version.
            for page in item["pages"]:
                page["requires_confirmation"], page["confirmed"] = True, False
        else:
            valid = {p["page"] for p in item["pages"]}
            if not set(patch.confirmed_pages) <= valid:
                raise AttachmentError("invalid_confirmed_page")
            for page in item["pages"]:
                if page["page"] in patch.confirmed_pages:
                    page["confirmed"] = True
        if patch.retry:
            item.update(status="queued", error=None, pages=[], selected_pages=patch.selected_pages,
                        version=item["version"] + 1)
        self._save(item)
        return item

    def context(self, aids, scope):
        if len(aids) > 3 or len(set(aids)) != len(aids):
            raise AttachmentError("attachment_count_limit")
        items = [self.get(aid, scope) for aid in aids]
        if sum(len(i["pages"]) for i in items) > 20:
            raise AttachmentError("combined_page_limit")
        for item in items:
            if item["status"] != "ready":
                raise AttachmentError("attachment_not_ready", 409)
            if any(p["requires_confirmation"] and not p["confirmed"] for p in item["pages"]):
                raise AttachmentError("attachment_confirmation_required", 409)
        return items

    async def delete(self, aid, scope):
        self.get(aid, scope)
        await self._cancel(aid)
        self._remove(aid)

    async def _cancel(self, aid):
        tasks = {t for t in (self.jobs.pop(aid, None), self.parsers.get(aid)) if t and not t.done()}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _remove(self, aid):
        # Only registered single files directly under the attachment root.
        self._file(aid).unlink(missing_ok=True)
        with self.connect() as conn:
            conn.execute("DELETE FROM attachments WHERE id=?", (aid,))

    async def cleanup(self):
        with self.connect() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM attachments WHERE expires<=?", (time.time(),))]
        for aid in ids:
            await self._cancel(aid)
            self._remove(aid)
        # Secure-delete/checkpoint prevents expired OCR text lingering in the WAL.
        if ids:
            with self.connect() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return len(ids)

    async def close(self):
        tasks = list(set(self.jobs.values()) | set(self.parsers.values()))
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
