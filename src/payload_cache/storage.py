"""SQLite persistence and atomic cache population."""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from payload_cache.models import PayloadInput

Transformer = Callable[[str], str]


class Base(DeclarativeBase):
    pass


class Transformation(Base):
    __tablename__ = "transformations"

    source: Mapped[str] = mapped_column(Text, primary_key=True)
    result: Mapped[str] = mapped_column(Text)


class Payload(Base):
    __tablename__ = "payloads"

    id: Mapped[str] = mapped_column(primary_key=True)
    output: Mapped[str] = mapped_column(Text)


def uppercase(value: str) -> str:
    """Deterministic stand-in for the external transformer."""
    return value.upper()


def payload_id(request: PayloadInput) -> str:
    # JSON preserves list boundaries, order, and empty strings unambiguously.
    canonical = json.dumps(
        [request.list_1, request.list_2], ensure_ascii=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


class PayloadStore:
    def __init__(self, database: Path, transformer: Transformer = uppercase) -> None:
        self.transformer = transformer
        self.engine = create_engine(
            "sqlite+pysqlite:///" + str(database.resolve()),
            connect_args={"check_same_thread": False, "timeout": 30},
        )

    def initialize(self) -> None:
        Path(self.engine.url.database or "").parent.mkdir(parents=True, exist_ok=True)
        Base.metadata.create_all(self.engine)

    def close(self) -> None:
        self.engine.dispose()

    def create(self, request: PayloadInput) -> str:
        identifier = payload_id(request)
        with Session(self.engine) as session:
            # Take SQLite's write reservation BEFORE reading the cache. Other
            # writers (even in another process) wait, then see committed results.
            # Session.close rolls back on failure, including transformer failures.
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            if session.get(Payload, identifier) is not None:
                session.commit()
                return identifier

            transformed: list[str] = []
            for pair in zip(request.list_1, request.list_2, strict=True):
                for source in pair:
                    cached = session.get(Transformation, source)
                    if cached is None:
                        result = self.transformer(source)
                        if not isinstance(result, str):
                            raise TypeError("transformer must return a string")
                        cached = Transformation(source=source, result=result)
                        session.add(cached)
                        # Make duplicate sources in this request visible to get().
                        session.flush()
                    transformed.append(cached.result)
            session.add(Payload(id=identifier, output=", ".join(transformed)))
            session.commit()
        return identifier

    def read(self, identifier: str) -> str | None:
        with Session(self.engine) as session:
            payload = session.get(Payload, identifier)
            return None if payload is None else payload.output
