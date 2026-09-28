"""Persistence for twins, runs, users, and tokens; Postgres is used when configured."""

import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from contracts_py.api import UserPublic
from contracts_py.events import Event, RunState
from contracts_py.package import DecisionPackage, HumanDecision
from contracts_py.twin import Twin

log = logging.getLogger(__name__)

SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
CONNECT_TIMEOUT_SECONDS = 10


class DuplicateEmail(ValueError):
    pass


@dataclass
class StoredUser:
    user: UserPublic
    password_hash: str


class Storage(Protocol):
    def save_twin(self, twin: Twin, *, active: bool = True) -> None: ...

    def load_active_twin(self, organization_id: str | None = None) -> Twin | None: ...

    def organization_ids(self) -> list[str]: ...

    def user_organization(self, user_id: str) -> str | None: ...

    def load_twin_version(self, version: str) -> Twin | None: ...

    def append_event(self, event: Event) -> None: ...

    def read_events(self, run_id: str) -> list[Event]: ...

    def save_state(self, state: RunState) -> None: ...

    def load_state(self, run_id: str) -> RunState | None: ...

    def save_package(self, run_id: str, package: DecisionPackage, package_hash: str) -> None: ...

    def load_package(self, run_id: str) -> tuple[DecisionPackage, str] | None: ...

    def save_decision(self, decision: HumanDecision) -> None: ...

    def create_user(self, user: UserPublic, password_hash: str, organization_id: str | None = None) -> None: ...

    def user_by_email(self, email: str) -> StoredUser | None: ...

    def user_by_id(self, user_id: str) -> UserPublic | None: ...

    def revoke_token(self, token_id: str, expires_at: datetime) -> None: ...

    def is_revoked(self, token_id: str) -> bool: ...

    def has_role(self, role: str) -> bool: ...

    def close(self) -> None: ...


def _user(user_id: str, email: str, display_name: str, role: str, created_at: Any) -> UserPublic:
    created = created_at if isinstance(created_at, datetime) else datetime.fromisoformat(created_at)
    return UserPublic(user_id=user_id, email=email, display_name=display_name, role=role, created_at=created)  # type: ignore[arg-type]


class FileStorage:
    """Test/local backend: run files plus SQLite users, tokens, and company twins."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.users_path = root / "users.sqlite3"
        self._packages: dict[str, tuple[DecisionPackage, str]] = {}
        root.mkdir(parents=True, exist_ok=True)
        with self._users() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, "
                "display_name TEXT NOT NULL, role TEXT NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS revoked_tokens (token_id TEXT PRIMARY KEY, expires_at TEXT NOT NULL)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS twins (twin_version TEXT PRIMARY KEY, organization_id TEXT NOT NULL, "
                "twin_json TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS organizations (organization_id TEXT PRIMARY KEY, twin_version TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS memberships (user_id TEXT PRIMARY KEY, organization_id TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO organizations SELECT organization_id, twin_version FROM twins WHERE active = 1")
            db.execute("INSERT OR IGNORE INTO memberships SELECT users.user_id, twins.organization_id "
                       "FROM users CROSS JOIN twins WHERE twins.active = 1")

    def _users(self) -> sqlite3.Connection:
        return sqlite3.connect(self.users_path)

    def _dir(self, run_id: str) -> Path:
        return self.root / run_id

    def save_twin(self, twin: Twin, *, active: bool = True) -> None:
        with self._users() as db:
            default = db.execute("SELECT organization_id FROM twins WHERE active = 1 LIMIT 1").fetchone()
            default_active = active and (default is None or default[0] == twin.organization.id)
            existing = db.execute("SELECT organization_id FROM twins WHERE twin_version = ?", (twin.version.twin_version,)).fetchone()
            if existing and existing[0] != twin.organization.id:
                raise ValueError("Twin version belongs to another organization")
            if default_active:
                db.execute("UPDATE twins SET active = 0")
            db.execute(
                "INSERT INTO twins VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(twin_version) DO UPDATE SET organization_id=excluded.organization_id, "
                "twin_json=excluded.twin_json, active=excluded.active, created_at=excluded.created_at",
                (twin.version.twin_version, twin.organization.id, twin.model_dump_json(), int(default_active),
                 twin.version.created_at.isoformat()),
            )
            if active:
                db.execute("INSERT INTO organizations VALUES (?, ?) ON CONFLICT(organization_id) "
                           "DO UPDATE SET twin_version=excluded.twin_version", (twin.organization.id, twin.version.twin_version))

    def load_active_twin(self, organization_id: str | None = None) -> Twin | None:
        with self._users() as db:
            row = db.execute("SELECT twin_json FROM twins JOIN organizations USING(twin_version) "
                             "WHERE organizations.organization_id = ?", (organization_id,)).fetchone() if organization_id else db.execute(
                "SELECT twin_json FROM twins WHERE active = 1 ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return Twin.model_validate_json(row[0]) if row else None

    def organization_ids(self) -> list[str]:
        with self._users() as db:
            return [row[0] for row in db.execute("SELECT organization_id FROM organizations ORDER BY organization_id")]

    def user_organization(self, user_id: str) -> str | None:
        with self._users() as db:
            row = db.execute("SELECT organization_id FROM memberships WHERE user_id = ?", (user_id,)).fetchone()
            row = row or db.execute("SELECT organization_id FROM twins WHERE active = 1 LIMIT 1").fetchone()
        return row[0] if row else None

    def load_twin_version(self, version: str) -> Twin | None:
        with self._users() as db:
            row = db.execute("SELECT twin_json FROM twins WHERE twin_version = ?", (version,)).fetchone()
        return Twin.model_validate_json(row[0]) if row else None

    def append_event(self, event: Event) -> None:
        with (self._dir(event.run_id) / "events.jsonl").open("a") as handle:
            handle.write(event.model_dump_json() + "\n")

    def read_events(self, run_id: str) -> list[Event]:
        path = self._dir(run_id) / "events.jsonl"
        if not path.is_file():
            return []
        return [Event.model_validate(json.loads(line)) for line in path.read_text().splitlines() if line.strip()]

    def save_state(self, state: RunState) -> None:
        directory = self._dir(state.run_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "state.json").write_text(state.model_dump_json(indent=2))

    def load_state(self, run_id: str) -> RunState | None:
        path = self._dir(run_id) / "state.json"
        return RunState.model_validate_json(path.read_text()) if path.is_file() else None

    # The file backend keeps the served package in memory, as before; the decision itself is in events.jsonl.
    def save_package(self, run_id: str, package: DecisionPackage, package_hash: str) -> None:
        self._packages[run_id] = (package, package_hash)

    def load_package(self, run_id: str) -> tuple[DecisionPackage, str] | None:
        return self._packages.get(run_id)

    def save_decision(self, decision: HumanDecision) -> None:
        pass

    def create_user(self, user: UserPublic, password_hash: str, organization_id: str | None = None) -> None:
        try:
            with self._users() as db:
                if organization_id and not db.execute("SELECT 1 FROM organizations WHERE organization_id = ?", (organization_id,)).fetchone():
                    raise ValueError("Organization not found")
                db.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?, ?)",
                           (user.user_id, user.email, user.display_name, user.role.value, password_hash,
                            user.created_at.isoformat()))
                org_id = organization_id or self.user_organization(user.user_id)
                if org_id:
                    db.execute("INSERT INTO memberships VALUES (?, ?)", (user.user_id, org_id))
        except sqlite3.IntegrityError as exc:
            raise DuplicateEmail(user.email) from exc

    def user_by_email(self, email: str) -> StoredUser | None:
        with self._users() as db:
            row = db.execute("SELECT user_id, email, display_name, role, created_at, password_hash FROM users "
                             "WHERE email = ?", (email,)).fetchone()
        return StoredUser(_user(*row[:5]), row[5]) if row else None

    def user_by_id(self, user_id: str) -> UserPublic | None:
        with self._users() as db:
            row = db.execute("SELECT user_id, email, display_name, role, created_at FROM users WHERE user_id = ?",
                             (user_id,)).fetchone()
        return _user(*row) if row else None

    def revoke_token(self, token_id: str, expires_at: datetime) -> None:
        with self._users() as db:
            db.execute("INSERT OR IGNORE INTO revoked_tokens VALUES (?, ?)", (token_id, expires_at.isoformat()))

    def is_revoked(self, token_id: str) -> bool:
        with self._users() as db:
            return db.execute("SELECT 1 FROM revoked_tokens WHERE token_id = ?", (token_id,)).fetchone() is not None

    def has_role(self, role: str) -> bool:
        with self._users() as db:
            return db.execute("SELECT 1 FROM users WHERE role = ? LIMIT 1", (role,)).fetchone() is not None

    def close(self) -> None:
        pass


POSTGRES_TABLES = [
    "CREATE TABLE IF NOT EXISTS canary_runs (run_id TEXT PRIMARY KEY, state JSONB NOT NULL, "
    "updated_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "CREATE TABLE IF NOT EXISTS canary_events (run_id TEXT NOT NULL, sequence INTEGER NOT NULL, type TEXT NOT NULL, "
    "event JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (run_id, sequence))",
    "CREATE TABLE IF NOT EXISTS canary_packages (run_id TEXT PRIMARY KEY, package JSONB NOT NULL, "
    "package_hash TEXT NOT NULL, served_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "CREATE TABLE IF NOT EXISTS canary_decisions (id BIGSERIAL PRIMARY KEY, run_id TEXT NOT NULL, "
    "package_id TEXT NOT NULL, decision JSONB NOT NULL, decided_at TIMESTAMPTZ NOT NULL)",
    "CREATE TABLE IF NOT EXISTS canary_users (user_id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, "
    "display_name TEXT NOT NULL, role TEXT NOT NULL, password_hash TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL)",
    "CREATE TABLE IF NOT EXISTS canary_revoked_tokens (token_id TEXT PRIMARY KEY, expires_at TIMESTAMPTZ NOT NULL)",
    "CREATE TABLE IF NOT EXISTS canary_twins (twin_version TEXT PRIMARY KEY, organization_id TEXT NOT NULL, "
    "twin JSONB NOT NULL, active BOOLEAN NOT NULL DEFAULT false, created_at TIMESTAMPTZ NOT NULL)",
    "CREATE TABLE IF NOT EXISTS canary_organizations (organization_id TEXT PRIMARY KEY, twin_version TEXT NOT NULL REFERENCES canary_twins(twin_version))",
    "CREATE TABLE IF NOT EXISTS canary_memberships (user_id TEXT PRIMARY KEY REFERENCES canary_users(user_id), organization_id TEXT NOT NULL REFERENCES canary_organizations(organization_id))",
    "INSERT INTO canary_organizations SELECT organization_id, twin_version FROM canary_twins WHERE active = true ON CONFLICT DO NOTHING",
    "INSERT INTO canary_memberships SELECT canary_users.user_id, canary_twins.organization_id "
    "FROM canary_users CROSS JOIN canary_twins WHERE canary_twins.active = true ON CONFLICT DO NOTHING",
]
POSTGRES_TABLE_NAMES = ["canary_runs", "canary_events", "canary_packages", "canary_decisions", "canary_users",
                        "canary_revoked_tokens", "canary_twins", "canary_organizations", "canary_memberships"]
DEFAULT_SCHEMA = "canary"
POSTGRES_OPEN_ATTEMPTS = 3
POSTGRES_RETRY_SECONDS = 2.0
# Supabase's Data API serves anon and authenticated; those roles (and PUBLIC) get nothing here. Row-level security
# with no policies denies every row to them as well, while the API's own owner or pooler role bypasses it.
REVOKE_API_ROLES = """
DO $$
DECLARE
    api_role text;
    table_name text;
BEGIN
    FOREACH api_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = api_role) THEN
            IF current_schema() <> 'public' THEN
                EXECUTE format('REVOKE ALL ON SCHEMA %I FROM %I', current_schema(), api_role);
                EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I REVOKE ALL ON TABLES FROM %I',
                               current_schema(), api_role);
            END IF;
            FOREACH table_name IN ARRAY ARRAY['canary_runs', 'canary_events', 'canary_packages', 'canary_decisions',
                                              'canary_users', 'canary_revoked_tokens', 'canary_twins', 'canary_organizations', 'canary_memberships'] LOOP
                EXECUTE format('REVOKE ALL ON TABLE %I.%I FROM %I', current_schema(), table_name, api_role);
            END LOOP;
            EXECUTE format('REVOKE ALL ON SEQUENCE %I.canary_decisions_id_seq FROM %I', current_schema(), api_role);
        END IF;
    END LOOP;
END $$
"""


class PostgresStorage:
    def __init__(self, url: str, schema: str = DEFAULT_SCHEMA) -> None:
        from psycopg import sql
        from psycopg_pool import ConnectionPool

        if not SCHEMA_NAME.fullmatch(schema):
            raise ValueError("CANARY_DB_SCHEMA must be a lowercase SQL identifier")
        self.schema = schema
        search_path = sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))

        def configure(conn: Any) -> None:
            conn.execute(search_path)
            conn.commit()

        # Poolers such as Supabase's cap connections, so the pool stays small; prepared statements stay off
        # so a transaction-mode pooler also works.
        self.pool = ConnectionPool(url, min_size=1, max_size=4, configure=configure, open=False,
                                   kwargs={"prepare_threshold": None}, name="canary")
        with self._bootstrap(url) as conn, conn.transaction():
            # Two API processes starting together would race on CREATE ... IF NOT EXISTS and can fail with a
            # UniqueViolation; a transaction advisory lock per schema makes the bootstrap run one at a time and
            # is released at commit, so a pooler cannot leak it to another client.
            conn.execute("SET LOCAL lock_timeout = '30s'")
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"canary_bootstrap:{schema}",))
            conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
            conn.execute(search_path)
            for statement in POSTGRES_TABLES:
                conn.execute(statement)  # type: ignore[arg-type]
            for table in POSTGRES_TABLE_NAMES:
                conn.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(sql.Identifier(table)))
                conn.execute(sql.SQL("REVOKE ALL ON TABLE {} FROM PUBLIC").format(sql.Identifier(table)))
            if schema != "public":
                conn.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(schema)))
            conn.execute(REVOKE_API_ROLES)  # type: ignore[arg-type]
        self.pool.open(wait=True, timeout=CONNECT_TIMEOUT_SECONDS)
        log.info("postgres storage ready (schema %s)", schema)

    @staticmethod
    def _bootstrap(url: str) -> Any:
        import psycopg

        return psycopg.connect(url, autocommit=True, prepare_threshold=None, connect_timeout=CONNECT_TIMEOUT_SECONDS)

    def _run(self, query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self.pool.connection() as conn:
            cursor = conn.execute(query, params)  # type: ignore[arg-type]
            return cursor.fetchall() if cursor.description else []

    def save_twin(self, twin: Twin, *, active: bool = True) -> None:
        from psycopg.types.json import Jsonb

        # Activation and snapshot insertion must succeed or roll back together.
        with self.pool.connection() as conn, conn.transaction():
            # Serialize activation so two first inserts cannot both become the default.
            conn.execute("LOCK TABLE canary_twins IN SHARE ROW EXCLUSIVE MODE")
            default = conn.execute("SELECT organization_id FROM canary_twins WHERE active = true LIMIT 1").fetchone()
            default_active = active and (default is None or default[0] == twin.organization.id)
            existing = conn.execute("SELECT organization_id FROM canary_twins WHERE twin_version = %s", (twin.version.twin_version,)).fetchone()
            if existing and existing[0] != twin.organization.id:
                raise ValueError("Twin version belongs to another organization")
            if default_active:
                conn.execute("UPDATE canary_twins SET active = false WHERE active = true")
            conn.execute(
                "INSERT INTO canary_twins (twin_version, organization_id, twin, active, created_at) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (twin_version) DO UPDATE SET "
                "organization_id = EXCLUDED.organization_id, twin = EXCLUDED.twin, "
                "active = EXCLUDED.active, created_at = EXCLUDED.created_at",
                (twin.version.twin_version, twin.organization.id, Jsonb(twin.model_dump(mode="json")), default_active,
                 twin.version.created_at),
            )
            if active:
                conn.execute("INSERT INTO canary_organizations VALUES (%s, %s) ON CONFLICT(organization_id) "
                             "DO UPDATE SET twin_version=EXCLUDED.twin_version", (twin.organization.id, twin.version.twin_version))

    def load_active_twin(self, organization_id: str | None = None) -> Twin | None:
        rows = self._run("SELECT twin FROM canary_twins JOIN canary_organizations USING(twin_version) "
                         "WHERE canary_organizations.organization_id = %s", (organization_id,)) if organization_id else self._run(
                             "SELECT twin FROM canary_twins WHERE active = true ORDER BY created_at DESC LIMIT 1")
        return Twin.model_validate(rows[0][0]) if rows else None

    def organization_ids(self) -> list[str]:
        return [row[0] for row in self._run("SELECT organization_id FROM canary_organizations ORDER BY organization_id")]

    def user_organization(self, user_id: str) -> str | None:
        rows = self._run("SELECT organization_id FROM canary_memberships WHERE user_id = %s", (user_id,))
        rows = rows or self._run("SELECT organization_id FROM canary_twins WHERE active = true LIMIT 1")
        return rows[0][0] if rows else None

    def load_twin_version(self, version: str) -> Twin | None:
        rows = self._run("SELECT twin FROM canary_twins WHERE twin_version = %s", (version,))
        return Twin.model_validate(rows[0][0]) if rows else None

    def append_event(self, event: Event) -> None:
        from psycopg.types.json import Jsonb

        self._run("INSERT INTO canary_events (run_id, sequence, type, event) VALUES (%s, %s, %s, %s)",
                  (event.run_id, event.sequence, event.type.value, Jsonb(event.model_dump(mode="json"))))

    def read_events(self, run_id: str) -> list[Event]:
        rows = self._run("SELECT event FROM canary_events WHERE run_id = %s ORDER BY sequence", (run_id,))
        return [Event.model_validate(row[0]) for row in rows]

    def save_state(self, state: RunState) -> None:
        from psycopg.types.json import Jsonb

        self._run("INSERT INTO canary_runs (run_id, state, updated_at) VALUES (%s, %s, now()) "
                  "ON CONFLICT (run_id) DO UPDATE SET state = EXCLUDED.state, updated_at = now()",
                  (state.run_id, Jsonb(state.model_dump(mode="json"))))

    def load_state(self, run_id: str) -> RunState | None:
        rows = self._run("SELECT state FROM canary_runs WHERE run_id = %s", (run_id,))
        return RunState.model_validate(rows[0][0]) if rows else None

    def save_package(self, run_id: str, package: DecisionPackage, package_hash: str) -> None:
        from psycopg.types.json import Jsonb

        self._run("INSERT INTO canary_packages (run_id, package, package_hash) VALUES (%s, %s, %s) "
                  "ON CONFLICT (run_id) DO UPDATE SET package = EXCLUDED.package, "
                  "package_hash = EXCLUDED.package_hash, served_at = now()",
                  (run_id, Jsonb(package.model_dump(mode="json")), package_hash))

    def load_package(self, run_id: str) -> tuple[DecisionPackage, str] | None:
        rows = self._run("SELECT package, package_hash FROM canary_packages WHERE run_id = %s", (run_id,))
        return (DecisionPackage.model_validate(rows[0][0]), rows[0][1]) if rows else None

    def save_decision(self, decision: HumanDecision) -> None:
        from psycopg.types.json import Jsonb

        self._run("INSERT INTO canary_decisions (run_id, package_id, decision, decided_at) VALUES (%s, %s, %s, %s)",
                  (decision.run_id, decision.package_id, Jsonb(decision.model_dump(mode="json")), decision.decided_at))

    def create_user(self, user: UserPublic, password_hash: str, organization_id: str | None = None) -> None:
        from psycopg.errors import UniqueViolation

        try:
            with self.pool.connection() as conn, conn.transaction():
                if organization_id and not conn.execute("SELECT 1 FROM canary_organizations WHERE organization_id = %s", (organization_id,)).fetchone():
                    raise ValueError("Organization not found")
                conn.execute("INSERT INTO canary_users VALUES (%s, %s, %s, %s, %s, %s)",
                             (user.user_id, user.email, user.display_name, user.role.value, password_hash, user.created_at))
                default = conn.execute("SELECT organization_id FROM canary_twins WHERE active = true LIMIT 1").fetchone()
                org_id = organization_id or (default[0] if default else None)
                if org_id:
                    conn.execute("INSERT INTO canary_memberships VALUES (%s, %s)", (user.user_id, org_id))
        except UniqueViolation as exc:
            raise DuplicateEmail(user.email) from exc

    def user_by_email(self, email: str) -> StoredUser | None:
        rows = self._run("SELECT user_id, email, display_name, role, created_at, password_hash FROM canary_users "
                         "WHERE email = %s", (email,))
        return StoredUser(_user(*rows[0][:5]), rows[0][5]) if rows else None

    def user_by_id(self, user_id: str) -> UserPublic | None:
        rows = self._run("SELECT user_id, email, display_name, role, created_at FROM canary_users WHERE user_id = %s",
                         (user_id,))
        return _user(*rows[0]) if rows else None

    def revoke_token(self, token_id: str, expires_at: datetime) -> None:
        self._run("INSERT INTO canary_revoked_tokens VALUES (%s, %s) ON CONFLICT (token_id) DO NOTHING",
                  (token_id, expires_at))

    def is_revoked(self, token_id: str) -> bool:
        return bool(self._run("SELECT 1 FROM canary_revoked_tokens WHERE token_id = %s", (token_id,)))

    def has_role(self, role: str) -> bool:
        return bool(self._run("SELECT 1 FROM canary_users WHERE role = %s LIMIT 1", (role,)))

    def close(self) -> None:
        self.pool.close()


def database_url() -> str | None:
    url = os.environ.get("DATABASE_URL", "")
    return url if url.startswith(("postgres://", "postgresql://")) else None


_lock = threading.Lock()
_current: Storage | None = None


def current() -> Storage:
    global _current
    with _lock:
        if _current is None:
            url = database_url()
            if url:
                schema = os.environ.get("CANARY_DB_SCHEMA") or DEFAULT_SCHEMA
                for attempt in range(1, POSTGRES_OPEN_ATTEMPTS + 1):
                    try:
                        _current = PostgresStorage(url, schema)
                        break
                    except ValueError:
                        raise  # a bad schema name is a configuration error, not an outage
                    except Exception as exc:
                        # Only the exception type is logged: psycopg messages can name the host.
                        if attempt < POSTGRES_OPEN_ATTEMPTS:
                            log.warning("Postgres could not be opened (%s), attempt %d of %d; retrying",
                                        type(exc).__name__, attempt, POSTGRES_OPEN_ATTEMPTS)
                            time.sleep(POSTGRES_RETRY_SECONDS * attempt)
                        else:
                            log.error("DATABASE_URL is set but Postgres could not be opened after %d attempts (%s); "
                                      "FALLING BACK to file storage under the runs directory. Runs and users will "
                                      "not reach the database.", POSTGRES_OPEN_ATTEMPTS, type(exc).__name__)
            if _current is None:
                from canary_api.paths import runs_dir

                _current = FileStorage(runs_dir())
        return _current


def backend_name() -> str:
    return "postgres" if isinstance(current(), PostgresStorage) else "file"


def close() -> None:
    global _current
    with _lock:
        if _current is not None:
            _current.close()
            _current = None


class Writer:
    """One background thread runs storage writes in submission order, so publishing never waits on the database."""

    def __init__(self) -> None:
        self.jobs: queue.Queue[Callable[[], None]] = queue.Queue()
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.failures = 0

    def submit(self, job: Callable[[], None]) -> None:
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, name="canary-storage-writer", daemon=True)
                self.thread.start()
        self.jobs.put(job)

    def flush(self) -> None:
        self.jobs.join()

    def _loop(self) -> None:
        while True:
            job = self.jobs.get()
            try:
                job()
            except Exception as exc:
                with self.lock:
                    self.failures += 1
                log.error("storage write failed (%s)", type(exc).__name__)
            finally:
                self.jobs.task_done()


writer = Writer()
