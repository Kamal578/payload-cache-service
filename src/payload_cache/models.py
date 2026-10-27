"""API contracts shared by the service and CLI."""

from pydantic import BaseModel, ConfigDict, model_validator


class PayloadInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    list_1: list[str]
    list_2: list[str]

    @model_validator(mode="after")
    def validate_lists(self) -> "PayloadInput":
        if len(self.list_1) != len(self.list_2):
            raise ValueError("list_1 and list_2 must have the same length")
        for value in self.list_1 + self.list_2:
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("strings must contain valid Unicode") from exc
        return self


class PayloadCreated(BaseModel):
    id: str


class PayloadOutput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    output: str
