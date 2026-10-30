"""HTTP boundary; blocking database and transformer work runs in worker threads."""

import json
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi import Path as PathParameter
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import SQLAlchemyError

from payload_cache.models import PayloadCreated, PayloadInput, PayloadOutput
from payload_cache.storage import PayloadStore, Transformer, uppercase

logger = logging.getLogger(__name__)
Identifier = Annotated[str, PathParameter(pattern=r"^[0-9a-f]{64}$")]


def create_app(
    database: Path | None = None, transformer: Transformer = uppercase
) -> FastAPI:
    store = PayloadStore(
        database
        if database is not None
        else Path(os.getenv("CACHE_DATABASE", "data/cache.db")),
        transformer,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        try:
            await run_in_threadpool(store.initialize)
            yield
        finally:
            await run_in_threadpool(store.close)

    application = FastAPI(title="Payload cache service", lifespan=lifespan)

    @application.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, exc: RequestValidationError
    ) -> Response:
        # Do not echo user input; escaped JSON also handles invalid surrogates.
        details = [
            {key: error[key] for key in ("type", "loc", "msg")}
            for error in exc.errors()
        ]
        return Response(
            json.dumps({"detail": details}, ensure_ascii=True),
            status_code=422,
            media_type="application/json",
        )

    @application.post("/payload", response_model=PayloadCreated)
    def create_payload(request: PayloadInput) -> PayloadCreated:
        try:
            return PayloadCreated(id=store.create(request))
        except SQLAlchemyError as exc:
            logger.exception("Payload database operation failed")
            raise HTTPException(
                503, "Payload storage is temporarily unavailable"
            ) from exc
        except Exception as exc:
            logger.exception("Payload transformation failed")
            raise HTTPException(502, "Payload transformation failed") from exc

    @application.get("/payload/{identifier}", response_model=PayloadOutput)
    def read_payload(identifier: Identifier) -> PayloadOutput:
        try:
            output = store.read(identifier)
        except SQLAlchemyError as exc:
            logger.exception("Payload database read failed")
            raise HTTPException(
                503, "Payload storage is temporarily unavailable"
            ) from exc
        if output is None:
            raise HTTPException(404, "Payload not found")
        return PayloadOutput(output=output)

    return application


app = create_app()
