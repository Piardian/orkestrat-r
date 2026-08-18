from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterator

from .model import GoalRecord
from .plan import GoalPlan
from .complexity import ComplexityAssessment
from .review import GoalReview


class GoalConcurrencyError(RuntimeError):
    """Raised when another worker changed a goal after it was read."""


class GoalStore:
    """Filesystem artifact store with atomic local goal-id reservation.

    Goal artifacts intentionally remain files because they are useful for debugging,
    reviews and handoffs. Production state can be promoted to PostgreSQL via
    ``build_goal_store`` without changing the artifact layout.
    """

    def __init__(self, base_dir: str | Path = "runtime/goals") -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.request_dir = self.base_dir / ".requests"
        self.request_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, goal_id: str) -> Path:
        return self.goal_dir(goal_id) / "goal.json"

    def goal_dir(self, goal_id: str) -> Path:
        return self.base_dir / goal_id

    def legacy_path_for(self, goal_id: str) -> Path:
        return self.base_dir / f"{goal_id}.json"

    def allocate_goal_id(self, day: str) -> str:
        """Reserve a unique GOAL id using atomic directory creation."""
        prefix = f"GOAL-{day}-"
        for sequence in range(1, 10000):
            candidate = f"{prefix}{sequence:04d}"
            try:
                self.goal_dir(candidate).mkdir(parents=False, exist_ok=False)
                return candidate
            except FileExistsError:
                continue
        raise RuntimeError(f"Goal id space exhausted for {day}")

    def _request_path(self, request_key: str) -> Path:
        digest = hashlib.sha256(request_key.encode("utf-8")).hexdigest()
        return self.request_dir / f"{digest}.txt"

    def lookup_idempotency_key(self, request_key: str) -> str | None:
        path = self._request_path(request_key)
        if not path.exists():
            return None
        value = path.read_text(encoding="utf-8").strip()
        return value or None

    def claim_idempotency_key(self, request_key: str, goal_id: str) -> str:
        """Atomically map a request key to one goal id on a local filesystem."""
        path = self._request_path(request_key)
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(goal_id + "\n")
            return goal_id
        except FileExistsError:
            existing = self.lookup_idempotency_key(request_key)
            if not existing:
                raise GoalConcurrencyError("Idempotency marker exists but is unreadable")
            return existing

    def save(self, record: GoalRecord) -> Path:
        target = self.path_for(record.goal_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(record.to_dict(), indent=2, ensure_ascii=False) + "\n"
        with tempfile.NamedTemporaryFile("w", delete=False, dir=target.parent, suffix=".tmp", encoding="utf-8") as tmp:
            tmp.write(payload)
            tmp_path = Path(tmp.name)
        tmp_path.replace(target)
        return target

    def save_transition(self, previous: GoalRecord, updated: GoalRecord) -> Path:
        """Filesystem fallback transition.

        The local backend is mainly for development. Atomic file replacement protects
        against partial writes; production concurrency control is provided by the
        PostgreSQL implementation below.
        """
        current = self.load(previous.goal_id)
        if current.to_dict() != previous.to_dict():
            raise GoalConcurrencyError(
                f"Goal {previous.goal_id} changed concurrently: expected {previous.status}, found {current.status}"
            )
        return self.save(updated)

    @contextmanager
    def goal_lock(self, goal_id: str) -> Iterator[None]:
        # PostgreSQL overrides this with an advisory lock. The local backend relies
        # on transition CAS and is intended for single-process development.
        yield

    def save_plan(self, goal_id: str, filename: str, payload: dict[str, Any]) -> Path:
        target = self.goal_dir(goal_id) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        with tempfile.NamedTemporaryFile("w", delete=False, dir=target.parent, suffix=".tmp", encoding="utf-8") as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        tmp_path.replace(target)
        return target

    def save_text(self, goal_id: str, filename: str, content: str) -> Path:
        target = self.goal_dir(goal_id) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", delete=False, dir=target.parent, suffix=".tmp", encoding="utf-8") as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        tmp_path.replace(target)
        return target

    def append_jsonl(self, goal_id: str, filename: str, payload: dict[str, Any]) -> Path:
        target = self.goal_dir(goal_id) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False)
        with tempfile.NamedTemporaryFile("w", delete=False, dir=target.parent, suffix=".tmp", encoding="utf-8") as tmp:
            if target.exists():
                tmp.write(target.read_text(encoding="utf-8"))
            tmp.write(line + "\n")
            tmp_path = Path(tmp.name)
        tmp_path.replace(target)
        return target

    def read_jsonl(self, goal_id: str, filename: str) -> list[dict[str, Any]]:
        path = self.goal_dir(goal_id) / filename
        if not path.exists():
            return []
        items: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            if isinstance(raw, dict):
                items.append(raw)
        return items

    def load(self, goal_id: str) -> GoalRecord:
        path = self.path_for(goal_id)
        if not path.exists():
            legacy = self.legacy_path_for(goal_id)
            if legacy.exists():
                path = legacy
        data = json.loads(path.read_text(encoding="utf-8"))
        return _record_from_dict(data)

    def list_goal_ids(self) -> list[str]:
        ids = {path.stem for path in self.base_dir.glob("GOAL-*.json")}
        ids.update(path.name for path in self.base_dir.iterdir() if path.is_dir() and path.name.startswith("GOAL-"))
        return sorted(ids)

    def save_plan_bundle(self, record: GoalRecord, search_plan: dict[str, Any], evidence: dict[str, Any], plan: GoalPlan) -> dict[str, Path]:
        return {
            "goal": self.save(record),
            "search_plan": self.save_plan(record.goal_id, "search_plan.json", search_plan),
            "evidence": self.save_plan(record.goal_id, "evidence.json", evidence),
            "plan": self.save_plan(record.goal_id, "plan.json", plan.to_dict()),
        }

    def save_review_bundle(self, record: GoalRecord, analyst_reviews: list[dict[str, Any]], review: GoalReview) -> dict[str, Path]:
        saved: dict[str, Path] = {
            "goal": self.save(record),
            "review": self.save_plan(record.goal_id, "review.json", review.to_dict()),
        }
        for index, item in enumerate(analyst_reviews, start=1):
            saved[f"analyst_{index}"] = self.save_plan(record.goal_id, f"analyst-{index}-review.json", item)
        return saved

    def save_complexity_bundle(self, record: GoalRecord, complexity: ComplexityAssessment) -> dict[str, Path]:
        return {
            "goal": self.save(record),
            "complexity": self.save_plan(record.goal_id, "complexity.json", complexity.to_dict()),
        }


class PostgresGoalStore(GoalStore):
    """PostgreSQL source-of-truth for goal state with filesystem artifacts."""

    def __init__(self, database_url: str, base_dir: str | Path = "runtime/goals") -> None:
        self.database_url = database_url
        super().__init__(base_dir)
        self._ensure_schema()

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - depends on optional production dependency
            raise RuntimeError("PostgreSQL backend requires psycopg[binary]") from exc
        return psycopg.connect(self.database_url)

    def _ensure_schema(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS agent_army_goals (
                goal_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                payload JSONB NOT NULL,
                version BIGINT NOT NULL DEFAULT 1,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS agent_army_goal_sequences (
                day TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS agent_army_goal_requests (
                request_key TEXT PRIMARY KEY,
                goal_id TEXT NOT NULL UNIQUE
            )
            """,
        ]
        with self._connect() as conn:
            with conn.cursor() as cur:
                for statement in statements:
                    cur.execute(statement)

    def allocate_goal_id(self, day: str) -> str:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO agent_army_goal_sequences(day, value)
                    VALUES (%s, 1)
                    ON CONFLICT(day) DO UPDATE
                    SET value = agent_army_goal_sequences.value + 1
                    RETURNING value
                    """,
                    (day,),
                )
                row = cur.fetchone()
        if not row:
            raise RuntimeError("PostgreSQL failed to allocate a goal sequence")
        goal_id = f"GOAL-{day}-{int(row[0]):04d}"
        self.goal_dir(goal_id).mkdir(parents=True, exist_ok=True)
        return goal_id

    def lookup_idempotency_key(self, request_key: str) -> str | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT goal_id FROM agent_army_goal_requests WHERE request_key = %s", (request_key,))
                row = cur.fetchone()
        return str(row[0]) if row else None

    def claim_idempotency_key(self, request_key: str, goal_id: str) -> str:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO agent_army_goal_requests(request_key, goal_id)
                    VALUES (%s, %s)
                    ON CONFLICT(request_key) DO NOTHING
                    RETURNING goal_id
                    """,
                    (request_key, goal_id),
                )
                row = cur.fetchone()
                if row:
                    return str(row[0])
                cur.execute("SELECT goal_id FROM agent_army_goal_requests WHERE request_key = %s", (request_key,))
                existing = cur.fetchone()
        if not existing:
            raise GoalConcurrencyError("Unable to resolve idempotency key after conflict")
        return str(existing[0])

    def save(self, record: GoalRecord) -> Path:
        payload = json.dumps(record.to_dict(), ensure_ascii=False)
        mirror = record
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO agent_army_goals(goal_id, status, payload)
                    VALUES (%s, %s, %s::jsonb)
                    ON CONFLICT(goal_id) DO UPDATE
                    SET payload = EXCLUDED.payload,
                        updated_at = NOW(),
                        version = agent_army_goals.version + 1
                    WHERE agent_army_goals.status = EXCLUDED.status
                    RETURNING payload
                    """,
                    (record.goal_id, record.status, payload),
                )
                row = cur.fetchone()
                if not row:
                    cur.execute("SELECT payload FROM agent_army_goals WHERE goal_id = %s", (record.goal_id,))
                    current = cur.fetchone()
                    if current:
                        mirror = _record_from_dict(_json_payload(current[0]))
        return super().save(mirror)

    def save_transition(self, previous: GoalRecord, updated: GoalRecord) -> Path:
        previous_payload = json.dumps(previous.to_dict(), ensure_ascii=False)
        updated_payload = json.dumps(updated.to_dict(), ensure_ascii=False)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE agent_army_goals
                    SET status = %s,
                        payload = %s::jsonb,
                        version = version + 1,
                        updated_at = NOW()
                    WHERE goal_id = %s
                      AND payload = %s::jsonb
                    RETURNING version
                    """,
                    (updated.status, updated_payload, previous.goal_id, previous_payload),
                )
                row = cur.fetchone()
        if not row:
            current = self.load(previous.goal_id)
            raise GoalConcurrencyError(
                f"Goal {previous.goal_id} changed concurrently: expected {previous.status}, found {current.status}"
            )
        return GoalStore.save(self, updated)

    def load(self, goal_id: str) -> GoalRecord:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT payload FROM agent_army_goals WHERE goal_id = %s", (goal_id,))
                row = cur.fetchone()
        if row:
            record = _record_from_dict(_json_payload(row[0]))
            GoalStore.save(self, record)
            return record
        # Seamless migration path for existing file-backed goals.
        record = GoalStore.load(self, goal_id)
        self.save(record)
        return record

    def list_goal_ids(self) -> list[str]:
        ids = set(GoalStore.list_goal_ids(self))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT goal_id FROM agent_army_goals ORDER BY goal_id")
                ids.update(str(row[0]) for row in cur.fetchall())
        return sorted(ids)

    @contextmanager
    def goal_lock(self, goal_id: str) -> Iterator[None]:
        """Cross-process advisory lock for one goal's long-running stage."""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (goal_id,))
            yield
        finally:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (goal_id,))
                conn.commit()
            finally:
                conn.close()


def build_goal_store(base_dir: str | Path = "runtime/goals") -> GoalStore:
    backend = os.getenv("AGENT_ARMY_STATE_BACKEND", "auto").strip().lower()
    database_url = os.getenv("AGENT_ARMY_DATABASE_URL", "").strip() or os.getenv("DATABASE_URL", "").strip()
    if backend == "postgres" or (backend == "auto" and database_url):
        if not database_url:
            raise RuntimeError("AGENT_ARMY_STATE_BACKEND=postgres requires AGENT_ARMY_DATABASE_URL")
        return PostgresGoalStore(database_url=database_url, base_dir=base_dir)
    if backend not in {"auto", "file", "filesystem"}:
        raise ValueError(f"Unsupported AGENT_ARMY_STATE_BACKEND: {backend}")
    return GoalStore(base_dir)


def _json_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        raw = json.loads(value)
        if isinstance(raw, dict):
            return raw
    raise ValueError("Invalid PostgreSQL goal payload")


def _record_from_dict(data: dict[str, Any]) -> GoalRecord:
    return GoalRecord(
        goal_id=str(data["goal_id"]),
        goal=str(data["goal"]),
        repo=str(data["repo"]),
        status=str(data["status"]),
        created_at=str(data["created_at"]),
        updated_at=str(data["updated_at"]),
        phase=str(data.get("phase", "intake")),
        utc_timestamp=bool(data.get("utc_timestamp", True)),
        goal_type=str(data.get("goal_type", "CODE_MODIFICATION")),
        notes=[str(item) for item in data.get("notes", [])] if isinstance(data.get("notes"), list) else [],
    )
