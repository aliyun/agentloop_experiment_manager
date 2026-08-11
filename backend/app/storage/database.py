"""SQLite engine/session setup with the mandatory safety PRAGMAs."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Connection, Engine, create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable

from app.config import ensure_private_data_dir

from .models import Base

SQLITE_BUSY_TIMEOUT_MS = 5_000
CURRENT_SCHEMA_VERSION = 5


class Database:
    """Own the SQLite engine used by repositories.

    Use :meth:`from_path` in production so that the application data directory
    and database file receive restrictive permissions.  URL construction is
    retained for tests and dependency injection.
    """

    def __init__(self, url: str, *, file_path: Path | None = None) -> None:
        parsed = make_url(url)
        if parsed.drivername != "sqlite":
            raise ValueError("the local launcher storage must use SQLite")

        engine_options: dict[str, object] = {
            "future": True,
            "connect_args": {"check_same_thread": False},
        }
        if parsed.database in (None, "", ":memory:"):
            # All sessions must share one in-memory database.
            engine_options["poolclass"] = StaticPool

        self.url = url
        self.file_path = file_path
        self.engine = create_engine(url, **engine_options)
        self._install_sqlite_pragmas(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            autoflush=False,
            expire_on_commit=False,
        )

    @classmethod
    def from_path(cls, path: str | Path) -> "Database":
        """Create a database backed by a dedicated, permission-hardened path."""

        raw_path = Path(path).expanduser()
        if raw_path.is_symlink():
            raise ValueError("database file must not be a symbolic link")
        file_path = raw_path.resolve()
        directory = file_path.parent
        directory = ensure_private_data_dir(directory)
        file_path = directory / file_path.name
        return cls(f"sqlite:///{file_path}", file_path=file_path)

    @staticmethod
    def _install_sqlite_pragmas(engine: Engine) -> None:
        @event.listens_for(engine, "connect")
        def set_sqlite_pragmas(dbapi_connection: object, connection_record: object) -> None:
            del connection_record
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            try:
                cursor.execute("PRAGMA journal_mode = WAL")
                cursor.execute("PRAGMA foreign_keys = ON")
                cursor.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
            finally:
                cursor.close()

    def initialize(self) -> None:
        """Create or migrate the schema, then harden database file permissions."""

        tables_before = set(inspect(self.engine).get_table_names())
        Base.metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            stored = connection.execute(
                text("SELECT version FROM schema_version WHERE id = 1")
            ).scalar_one_or_none()
            if stored is None:
                # Databases created by older builds contain the three original
                # control-plane tables but no schema-version marker.
                current = 1 if "launch_intents" in tables_before else CURRENT_SCHEMA_VERSION
                connection.execute(
                    text(
                        "INSERT INTO schema_version (id, version, updated_at) "
                        "VALUES (1, :version, :updated_at)"
                    ),
                    {"version": current, "updated_at": _utc_timestamp()},
                )
            else:
                current = int(stored)
            if current > CURRENT_SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {current} is newer than supported {CURRENT_SCHEMA_VERSION}"
                )
            if current < 2:
                _migrate_v1_to_v2(connection)
                current = 2
                connection.execute(
                    text(
                        "UPDATE schema_version SET version = :version, updated_at = :updated_at "
                        "WHERE id = 1"
                    ),
                    {"version": current, "updated_at": _utc_timestamp()},
                )
            if current < 3:
                _migrate_v2_to_v3(connection)
                current = 3
                connection.execute(
                    text(
                        "UPDATE schema_version SET version = :version, updated_at = :updated_at "
                        "WHERE id = 1"
                    ),
                    {"version": current, "updated_at": _utc_timestamp()},
                )
        if current < 4:
            _migrate_v3_to_v4(self.engine)
            current = 4
        if current < 5:
            with self.engine.begin() as connection:
                _migrate_v4_to_v5(connection)
                connection.execute(
                    text(
                        "UPDATE schema_version SET version = :version, updated_at = :updated_at "
                        "WHERE id = 1"
                    ),
                    {"version": 5, "updated_at": _utc_timestamp()},
                )
            current = 5
        if self.file_path is not None and self.file_path.exists():
            os.chmod(self.file_path, 0o600)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{self.file_path}{suffix}")
                if sidecar.exists():
                    os.chmod(sidecar, 0o600)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Open a transaction, committing on success and rolling back on error."""

        with self.session_factory() as session:
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    def dispose(self) -> None:
        self.engine.dispose()


def in_memory_database() -> Database:
    """Create an initialized shared in-memory database for unit tests."""

    database = Database("sqlite:///:memory:")
    database.initialize()
    return database


def _utc_timestamp() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _migrate_v1_to_v2(connection: Connection) -> None:
    """Add Agent connections and immutable launch-configuration snapshots."""

    schedule_columns = {
        "agent_connection_id": "TEXT REFERENCES agent_connections(id)",
        "agent_connection_name_snapshot": "TEXT",
        "version_mode": "TEXT NOT NULL DEFAULT 'AUTO'",
        "manual_experiment_version": "TEXT",
        "experiment_parameters_json": "TEXT NOT NULL DEFAULT '{}'",
        "record_name": "TEXT",
        "context_schema_version": "INTEGER NOT NULL DEFAULT 1",
        "launch_config_hash": "TEXT NOT NULL DEFAULT ''",
    }
    launch_columns = {
        "agent_connection_id": "TEXT REFERENCES agent_connections(id)",
        "agent_connection_name_snapshot": "TEXT",
        "version_mode": "TEXT NOT NULL DEFAULT 'AUTO'",
        "manual_experiment_version": "TEXT",
        "experiment_version": "TEXT",
        "experiment_parameters_json": "TEXT NOT NULL DEFAULT '{}'",
        "record_name": "TEXT",
        "context_schema_version": "INTEGER NOT NULL DEFAULT 1",
        "context_xml_template": "TEXT",
        "launch_config_hash": "TEXT NOT NULL DEFAULT ''",
    }
    _add_columns(connection, "schedules", schedule_columns)
    _add_columns(connection, "launch_intents", launch_columns)
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_schedules_agent_connection_id "
        "ON schedules (agent_connection_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_launch_intents_agent_connection_id "
        "ON launch_intents (agent_connection_id)"
    )
    _backfill_config_hashes(connection, "schedules", is_schedule=True)
    _backfill_config_hashes(connection, "launch_intents", is_schedule=False)


def _migrate_v2_to_v3(connection: Connection) -> None:
    """Add payload-free per-item Trace correlation audit state."""

    from .models import TraceCorrelation

    TraceCorrelation.__table__.create(bind=connection, checkfirst=True)


def _migrate_v3_to_v4(engine: Engine) -> None:
    """Add minute intervals and replace the SQLite cadence CHECK constraint.

    SQLite cannot alter a CHECK constraint in place.  Rebuild the table with
    foreign-key enforcement temporarily disabled on one dedicated connection,
    copy every existing row, then verify referential integrity before returning
    the connection to the pool.  Legacy hourly rows remain hourly.
    """

    from .models import Schedule

    required_v3_columns = {
        "id",
        "profile_id",
        "name",
        "plan_id",
        "cadence_type",
        "interval_hours",
        "daily_time",
        "timezone",
        "created_at",
        "updated_at",
    }
    with engine.connect() as connection:
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        connection.commit()
        try:
            with connection.begin():
                present = {
                    str(row[1])
                    for row in connection.exec_driver_sql('PRAGMA table_info("schedules")')
                }
                # A pre-versioning developer database may contain only the
                # original test stub.  It has no cadence CHECK to replace.
                if not required_v3_columns <= present:
                    _add_columns(
                        connection,
                        "schedules",
                        {"interval_minutes": "INTEGER"},
                    )
                else:
                    connection.exec_driver_sql("DROP TABLE IF EXISTS schedules_v4")
                    create_sql = str(
                        CreateTable(Schedule.__table__).compile(dialect=engine.dialect)
                    )
                    create_sql = create_sql.replace(
                        "CREATE TABLE schedules ", "CREATE TABLE schedules_v4 ", 1
                    )
                    connection.exec_driver_sql(create_sql)

                    target_columns = [column.name for column in Schedule.__table__.columns]
                    common_columns = [name for name in target_columns if name in present]
                    quoted = ", ".join(f'"{name}"' for name in common_columns)
                    connection.exec_driver_sql(
                        f'INSERT INTO "schedules_v4" ({quoted}) '
                        f'SELECT {quoted} FROM "schedules"'
                    )
                    connection.exec_driver_sql('DROP TABLE "schedules"')
                    connection.exec_driver_sql(
                        'ALTER TABLE "schedules_v4" RENAME TO "schedules"'
                    )
                    for index in Schedule.__table__.indexes:
                        index.create(bind=connection, checkfirst=True)

                violations = list(connection.exec_driver_sql("PRAGMA foreign_key_check"))
                if violations:
                    raise RuntimeError(
                        "foreign key check failed after schema v4 migration: "
                        f"{violations[:5]}"
                    )
                connection.execute(
                    text(
                        "UPDATE schema_version SET version = :version, updated_at = :updated_at "
                        "WHERE id = 1"
                    ),
                    {"version": 4, "updated_at": _utc_timestamp()},
                )
        finally:
            connection.commit()
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")


def _migrate_v4_to_v5(connection: Connection) -> None:
    """Add the payload-free per-Experiment-Plan dashboard reconciliation table."""

    from .models import PlanDashboard

    PlanDashboard.__table__.create(bind=connection, checkfirst=True)


def _add_columns(connection: Connection, table: str, columns: dict[str, str]) -> None:
    present = {str(row[1]) for row in connection.exec_driver_sql(f'PRAGMA table_info("{table}")')}
    for name, ddl in columns.items():
        if name not in present:
            connection.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl}')


def _backfill_config_hashes(connection: Connection, table: str, *, is_schedule: bool) -> None:
    from app.domain.launch_config import canonical_launch_config_hash

    fields = (
        "id, agent_connection_id, version_mode, manual_experiment_version, "
        "experiment_parameters_json, context_schema_version, launch_config_hash"
    )
    fields += ", record_name"
    rows = connection.execute(text(f'SELECT {fields} FROM "{table}"')).mappings()
    for row in rows:
        if row["launch_config_hash"]:
            continue
        digest = canonical_launch_config_hash(
            agent_connection_id=row["agent_connection_id"],
            version_mode=row["version_mode"],
            requested_version=row["manual_experiment_version"],
            experiment_parameters_json=row["experiment_parameters_json"],
            record_name=row["record_name"],
            context_schema_version=int(row["context_schema_version"]),
        )
        connection.execute(
            text(f'UPDATE "{table}" SET launch_config_hash = :digest WHERE id = :id'),
            {"digest": digest, "id": row["id"]},
        )
