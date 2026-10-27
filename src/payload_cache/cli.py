"""Pydantic Settings CLI that creates and reads payloads over HTTP."""

import json
import sys
from pathlib import Path

import httpx
from pydantic import (
    AnyHttpUrl,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    SettingsError,
)

from payload_cache.models import PayloadCreated, PayloadInput, PayloadOutput


class Arguments(BaseSettings):
    """Create/read a payload once per repeat; emit one JSON object per iteration."""

    model_config = SettingsConfigDict(
        cli_parse_args=True,
        cli_prog_name="cache-cli",
        cli_exit_on_error=False,
        case_sensitive=True,  # Keep -H (host) distinct from -h (help).
        cli_shortcuts={
            "host": "H",
            "repeat": "r",
            "input": "i",
            "json": "j",
            "output": "o",
        },
    )

    host: AnyHttpUrl = Field(
        default=AnyHttpUrl("http://127.0.0.1:8000"), description="Service base URL"
    )
    repeat: int = Field(default=1, ge=1, description="Number of create/read iterations")
    input_file: str | None = Field(
        default=None, alias="input", description="UTF-8 JSON file, or - for stdin"
    )
    json_input: str | None = Field(
        default=None, alias="json", description="Inline request JSON"
    )
    output_file: str = Field(
        default="-",
        alias="output",
        description="Output JSON Lines file, or - for stdout",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Only explicit CLI arguments and defaults affect this command.
        return (init_settings,)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username or value.password or value.query or value.fragment:
            raise ValueError(
                "host must not contain credentials, a query, or a fragment"
            )
        return value

    @model_validator(mode="after")
    def validate_input_source(self) -> "Arguments":
        if (self.input_file is None) == (self.json_input is None):
            raise ValueError("provide exactly one of --input or --json")
        return self


class ServiceError(Exception):
    """An unsuccessful or malformed service response."""


def run(arguments: Arguments) -> None:
    if arguments.json_input is not None:
        raw = arguments.json_input
    elif arguments.input_file == "-":
        raw = sys.stdin.read()
    else:
        assert arguments.input_file is not None
        raw = Path(arguments.input_file).read_text(encoding="utf-8")
    # Use Python JSON parsing + strict validation to match the HTTP contract.
    request = PayloadInput.model_validate(json.loads(raw))

    lines = []
    base = str(arguments.host).rstrip("/")
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        for _ in range(arguments.repeat):
            created_response = client.post(f"{base}/payload", json=request.model_dump())
            created_response.raise_for_status()
            try:
                created = PayloadCreated.model_validate(created_response.json())
            except ValueError as exc:
                raise ServiceError(
                    "service returned an invalid create response"
                ) from exc
            output_response = client.get(f"{base}/payload/{created.id}")
            output_response.raise_for_status()
            try:
                output = PayloadOutput.model_validate(output_response.json())
            except ValueError as exc:
                raise ServiceError(
                    "service returned an invalid payload response"
                ) from exc
            lines.append(output.model_dump_json() + "\n")
    # Delay output until every iteration succeeds, avoiding partial results on failure.
    content = "".join(lines)
    if arguments.output_file == "-":
        sys.stdout.write(content)
    else:
        Path(arguments.output_file).write_text(content, encoding="utf-8")


def main() -> int:
    try:
        arguments = Arguments()
        run(arguments)
    except (ValidationError, SettingsError, json.JSONDecodeError) as exc:
        if isinstance(exc, ValidationError):
            message = "; ".join(error["msg"] for error in exc.errors())
        else:
            message = str(exc)
        print(f"cache-cli: invalid input: {message}", file=sys.stderr)
        return 2
    except httpx.HTTPStatusError as exc:
        print(
            f"cache-cli: service returned HTTP {exc.response.status_code}",
            file=sys.stderr,
        )
        return 3
    except httpx.RequestError as exc:
        print(f"cache-cli: connection failed ({type(exc).__name__})", file=sys.stderr)
        return 3
    except (OSError, UnicodeError, ServiceError) as exc:
        print(f"cache-cli: {exc}", file=sys.stderr)
        return 3
    return 0
