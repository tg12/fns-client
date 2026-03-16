# FNS NOTAM Client -- A JS Labs Prototype
# Copyright (c) 2026 James Sawyer
# https://labs.jamessawyer.co.uk/ | https://github.com/tg12
#
# PROVIDED "AS IS" WITHOUT WARRANTY OF ANY KIND. NOT FOR OPERATIONAL
# AVIATION USE. See README.md and LICENSE for full terms.

"""Database persistence for NOTAM messages."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from sqlalchemy import (
    DateTime,
    MetaData,
    String,
    Table,
    Text,
    and_,
    create_engine,
    delete,
    func,
    inspect,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.sql import ColumnElement
from sqlalchemy.sql.schema import Column
from sqlalchemy.sql.sqltypes import BigInteger

from fns_client.config import DatabaseConfig
from fns_client.messages import FnsMessage, NotamStatus

LOGGER = logging.getLogger(__name__)


class NotamDb:
    """Persist and query NOTAM messages."""

    def __init__(self, config: DatabaseConfig) -> None:
        # Build the database engine and table metadata once at startup.
        self._config = config
        self._valid = False
        self._initializing = False
        self._engine = create_engine(self._to_sqlalchemy_url(config), future=True)
        self._metadata = MetaData(schema=None if self._is_sqlite else config.schema)
        self._table = Table(
            config.table,
            self._metadata,
            Column("fnsid", BigInteger, primary_key=True),
            Column("correlationid", BigInteger, nullable=False),
            Column("issuedtimestamp", DateTime(timezone=True), nullable=True),
            Column("storedtimestamp", DateTime(timezone=True), nullable=False),
            Column("updatedtimestamp", DateTime(timezone=True), nullable=True),
            Column("validfromtimestamp", DateTime(timezone=True), nullable=True),
            Column("validtotimestamp", DateTime(timezone=True), nullable=True),
            Column("classification", String(4), nullable=False, default=""),
            Column("locationdesignator", String(12), nullable=False, default=""),
            Column("notamaccountability", String(12), nullable=False, default=""),
            Column("notamtext", Text, nullable=False, default=""),
            Column("aixmnotammessage", Text, nullable=False),
            Column("status", String(12), nullable=False),
        )

    @property
    def _is_sqlite(self) -> bool:
        return self._config.connection_url.startswith(
            "jdbc:h2:"
        ) or self._config.connection_url.startswith("sqlite")

    @staticmethod
    def _to_sqlalchemy_url(config: DatabaseConfig) -> str:
        # Translate the legacy JDBC-style configuration into SQLAlchemy URLs.
        connection_url = config.connection_url
        if connection_url.startswith("jdbc:h2:"):
            path_part = connection_url.removeprefix("jdbc:h2:").split(";", 1)[0]
            db_path = Path(path_part).expanduser().resolve()
            return f"sqlite:///{db_path}"
        if connection_url.startswith("jdbc:postgresql://"):
            host_and_path = connection_url.removeprefix("jdbc:postgresql://")
            auth = ""
            if config.username:
                auth = quote_plus(config.username)
                if config.password:
                    auth = f"{auth}:{quote_plus(config.password)}"
                auth = f"{auth}@"
            return f"postgresql+psycopg://{auth}{host_and_path}"
        if connection_url.startswith("sqlite") or connection_url.startswith(
            "postgresql"
        ):
            return connection_url
        raise ValueError(f"Unsupported database URL: {connection_url}")

    def is_valid(self) -> bool:
        """Return whether the database is ready to serve traffic."""

        return self._valid

    def set_valid(self) -> None:
        """Mark the database ready for reads."""

        self._valid = True

    def set_invalid(self) -> None:
        """Mark the database unavailable."""

        self._valid = False

    def is_initializing(self) -> bool:
        """Return whether the FIL bootstrap is running."""

        return self._initializing

    def set_initializing(self, value: bool) -> None:
        """Update the initialization state."""

        self._initializing = value

    def notam_table_exists(self) -> bool:
        """Return whether the NOTAMS table already exists."""

        inspector = inspect(self._engine)
        return inspector.has_table(
            self._config.table, schema=None if self._is_sqlite else self._config.schema
        )

    def create_notam_table(self) -> None:
        """Create the NOTAMS table if needed."""

        self._metadata.create_all(self._engine, tables=[self._table])

    def drop_notam_table(self) -> None:
        """Drop the NOTAMS table when a full rebuild is required."""

        self._metadata.drop_all(self._engine, tables=[self._table], checkfirst=True)

    def get_last_correlation_id(self) -> tuple[int, datetime] | None:
        """Return the most recent stored correlation id."""

        query = (
            select(self._table.c.correlationid, self._table.c.storedtimestamp)
            .order_by(self._table.c.correlationid.desc())
            .limit(1)
        )
        with self._engine.begin() as connection:
            row = connection.execute(query).one_or_none()
        if row is None:
            return None
        return int(row.correlationid), row.storedtimestamp

    def check_if_notam_is_newer(self, message: FnsMessage) -> bool:
        """Return True when the incoming NOTAM is newer than the stored row."""

        query = select(self._table.c.updatedtimestamp).where(
            self._table.c.fnsid == message.fns_id
        )
        with self._engine.begin() as connection:
            row = connection.execute(query).one_or_none()
        if (
            row is None
            or row.updatedtimestamp is None
            or message.updated_timestamp is None
        ):
            return True
        return row.updatedtimestamp < message.updated_timestamp

    def put_notam(self, message: FnsMessage) -> None:
        """Insert or update one NOTAM."""

        if not self._initializing and not self.check_if_notam_is_newer(message):
            LOGGER.debug(
                "Discarding stale NOTAM fns_id=%s correlation_id=%s",
                message.fns_id,
                message.correlation_id,
            )
            return

        values = {
            "fnsid": message.fns_id,
            "correlationid": message.correlation_id,
            "issuedtimestamp": message.issued_timestamp,
            "storedtimestamp": datetime.now(UTC),
            "updatedtimestamp": message.updated_timestamp,
            "validfromtimestamp": message.valid_from_timestamp,
            "validtotimestamp": message.valid_to_timestamp,
            "classification": message.classification,
            "locationdesignator": message.location_designator,
            "notamaccountability": message.notam_accountability,
            "notamtext": message.notam_text,
            "aixmnotammessage": message.aixm_notam_message,
            "status": message.status.value,
        }

        update_values = values.copy()
        update_values.pop("fnsid")
        if self._engine.dialect.name == "sqlite":
            statement = sqlite_insert(self._table).values(**values)
            statement = statement.on_conflict_do_update(
                index_elements=[self._table.c.fnsid],
                set_=update_values,
            )
        elif self._engine.dialect.name == "postgresql":
            statement = postgresql_insert(self._table).values(**values)
            statement = statement.on_conflict_do_update(
                index_elements=[self._table.c.fnsid],
                set_=update_values,
            )
        else:
            raise ValueError(f"Unsupported SQL dialect: {self._engine.dialect.name}")

        with self._engine.begin() as connection:
            connection.execute(statement)

    def remove_old_notams(self) -> int:
        """Delete expired or non-active NOTAMs."""

        now = datetime.now(UTC)
        delete_expired = delete(self._table).where(
            self._table.c.validtotimestamp.is_not(None),
            self._table.c.validtotimestamp < now,
        )
        delete_non_active = delete(self._table).where(
            self._table.c.status != NotamStatus.ACTIVE.value
        )
        with self._engine.begin() as connection:
            expired = connection.execute(delete_expired).rowcount or 0
            inactive = connection.execute(delete_non_active).rowcount or 0
        return expired + inactive

    def get_by_location_designator(self, location_designator: str) -> list[str]:
        """Return active NOTAM payloads for one location designator."""

        return self._run_xml_query(
            select(self._table.c.aixmnotammessage)
            .where(
                self._table.c.locationdesignator == location_designator,
                self._active_notam_clause(),
            )
            .order_by(self._table.c.fnsid.asc())
        )

    def get_by_classification(self, classification: str) -> list[str]:
        """Return active NOTAM payloads for one classification."""

        return self._run_xml_query(
            select(self._table.c.aixmnotammessage)
            .where(
                self._table.c.classification == classification,
                self._active_notam_clause(),
            )
            .order_by(self._table.c.fnsid.asc())
        )

    def get_delta(self, delta_time: datetime) -> list[str]:
        """Return NOTAM payloads updated after the requested instant."""

        return self._run_xml_query(
            select(self._table.c.aixmnotammessage)
            .where(
                or_(
                    self._table.c.updatedtimestamp >= delta_time,
                    self._table.c.validtotimestamp.is_(None),
                )
            )
            .order_by(self._table.c.fnsid.asc())
        )

    def get_by_time_range(
        self, from_datetime: datetime, to_datetime: datetime
    ) -> list[str]:
        """Return active NOTAM payloads within the requested validity window."""

        return self._run_xml_query(
            select(self._table.c.aixmnotammessage)
            .where(
                self._table.c.validfromtimestamp >= from_datetime,
                or_(
                    self._table.c.validtotimestamp <= to_datetime,
                    self._table.c.validtotimestamp.is_(None),
                ),
                self._table.c.status == NotamStatus.ACTIVE.value,
            )
            .order_by(self._table.c.fnsid.asc())
        )

    def get_all_notams(self) -> list[str]:
        """Return all active NOTAM payloads."""

        return self._run_xml_query(
            select(self._table.c.aixmnotammessage)
            .where(self._active_notam_clause())
            .order_by(self._table.c.fnsid.asc())
        )

    def list_notams(
        self,
        *,
        location_designator: str | None = None,
        classification: str | None = None,
        updated_since: datetime | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """Return filtered NOTAM rows as dictionaries for the web UI and JSON API."""

        # Build filters incrementally so every query path uses the same
        # deterministic ordering.
        conditions: list[ColumnElement[bool]] = [self._active_notam_clause()]
        if location_designator:
            conditions.append(self._table.c.locationdesignator == location_designator)
        if classification:
            conditions.append(self._table.c.classification == classification)
        if updated_since is not None:
            conditions.append(self._table.c.updatedtimestamp >= updated_since)
        if valid_from is not None:
            conditions.append(self._table.c.validfromtimestamp >= valid_from)
        if valid_to is not None:
            conditions.append(
                or_(
                    self._table.c.validtotimestamp <= valid_to,
                    self._table.c.validtotimestamp.is_(None),
                )
            )

        query = (
            select(
                self._table.c.fnsid,
                self._table.c.correlationid,
                self._table.c.issuedtimestamp,
                self._table.c.updatedtimestamp,
                self._table.c.validfromtimestamp,
                self._table.c.validtotimestamp,
                self._table.c.classification,
                self._table.c.locationdesignator,
                self._table.c.notamaccountability,
                self._table.c.notamtext,
                self._table.c.status,
            )
            .where(*conditions)
            .order_by(self._table.c.fnsid.asc())
            .limit(max(limit, 1))
        )
        with self._engine.begin() as connection:
            rows = connection.execute(query).mappings().all()
        return [dict(row) for row in rows]

    def get_status_snapshot(self) -> dict[str, Any]:
        """Return a compact summary of the database state."""

        # Compute counts inside the database so the status endpoint stays lightweight.
        active_count_query = (
            select(func.count())
            .select_from(self._table)
            .where(self._active_notam_clause())
        )
        classification_query = (
            select(self._table.c.classification, func.count())
            .where(self._active_notam_clause())
            .group_by(self._table.c.classification)
            .order_by(self._table.c.classification.asc())
        )
        with self._engine.begin() as connection:
            active_count = int(connection.execute(active_count_query).scalar_one())
            classification_rows = connection.execute(classification_query).all()
        return {
            "database_valid": self._valid,
            "database_initializing": self._initializing,
            "active_notam_count": active_count,
            "classification_counts": {
                str(row[0]): int(row[1]) for row in classification_rows
            },
        }

    def get_validation_map(self) -> dict[str, datetime | None]:
        """Return the fns_id -> updated timestamp map for validation use."""

        query = select(self._table.c.fnsid, self._table.c.updatedtimestamp)
        with self._engine.begin() as connection:
            rows = connection.execute(query).all()
        return {str(row.fnsid): row.updatedtimestamp for row in rows}

    def _run_xml_query(self, query: Any) -> list[str]:
        # Keep raw XML reads in one helper so database access stays uniform.
        with self._engine.begin() as connection:
            rows = connection.execute(query).all()
        return [str(row.aixmnotammessage) for row in rows]

    def _active_notam_clause(self) -> ColumnElement[bool]:
        # Centralize the active-row predicate so all queries agree on visibility.
        now = datetime.now(UTC)
        return and_(
            self._table.c.status == NotamStatus.ACTIVE.value,
            or_(
                self._table.c.validtotimestamp > now,
                self._table.c.validtotimestamp.is_(None),
            ),
        )
