"""Transactional case persistence. Legacy JSON is imported once, never removed."""
import json
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import CasePatch, CaseRecord


class CaseStore:
    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / 'cases.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS cases (case_id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY)')
            db.execute('BEGIN IMMEDIATE')
            legacy = self.path.with_name('cases.json')
            if legacy.exists() and not db.execute("SELECT 1 FROM migrations WHERE name='json-v1'").fetchone():
                records = json.loads(legacy.read_text(encoding='utf-8'))
                cases = [CaseRecord.model_validate(row) for row in records.values()]
                backup = legacy.with_suffix('.json.pre-sqlite.bak')
                if not backup.exists():
                    shutil.copy2(legacy, backup)
                for case in cases:
                    db.execute('INSERT OR IGNORE INTO cases VALUES (?, ?)', (case.case_id, case.model_dump_json()))
                db.execute("INSERT INTO migrations VALUES ('json-v1')")

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            with db:
                yield db
        finally:
            db.close()

    def create(self, case: CaseRecord):
        with self._connection() as db:
            db.execute('INSERT INTO cases VALUES (?, ?)', (case.case_id, case.model_dump_json()))
        return case

    def get(self, case_id):
        with self._connection() as db:
            row = db.execute('SELECT payload FROM cases WHERE case_id=?', (case_id,)).fetchone()
        return CaseRecord.model_validate_json(row[0]) if row else None

    def list(self):
        with self._connection() as db:
            rows = db.execute('SELECT payload FROM cases ORDER BY case_id').fetchall()
        return [CaseRecord.model_validate_json(row[0]) for row in rows]

    def update(self, case_id, patch: CasePatch):
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM cases WHERE case_id=?', (case_id,)).fetchone()
            if not row:
                return None
            values = json.loads(row[0])
            values.update(patch.model_dump(exclude_unset=True))
            values['updated_at'] = datetime.now(timezone.utc)
            case = CaseRecord.model_validate(values)
            db.execute('UPDATE cases SET payload=? WHERE case_id=?', (case.model_dump_json(), case_id))
        return case

    def delete(self, case_id):
        with self._connection() as db:
            return db.execute('DELETE FROM cases WHERE case_id=?', (case_id,)).rowcount > 0
