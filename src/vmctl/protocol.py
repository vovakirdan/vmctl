"""Versioned, allowlisted SSH wire schema. Requests travel only through stdin."""

import json
from dataclasses import fields
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from vmctl.errors import VmctlError
from vmctl.models import CreateRequest
from vmctl.operations import ActionPreview, DeletePreview, LifecycleAction

PROTOCOL_VERSION: Literal[1] = 1
MAX_MESSAGE_BYTES = 1024 * 1024
RequestID = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)]
MUTATING_OPERATIONS = frozenset({"create", "delete", "start", "shutdown", "reboot"})


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @field_validator("protocol_version", mode="before", check_fields=False)
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Protocol version must be an integer")
        return value


class CreateParameters(WireModel):
    name: str = Field(min_length=1, max_length=63)
    template: str = Field(min_length=1, max_length=128)
    preset: str = Field(min_length=1, max_length=128)
    cpu: int | None = Field(default=None, gt=0, le=512)
    memory: str | None = None
    disk: str | None = None
    ip: str = "auto"
    public_key: str = Field(min_length=16, max_length=65536)
    modules: tuple[str, ...] = ()
    versions: dict[str, str] = Field(default_factory=dict)
    start: bool = True
    description: str = Field(default="", max_length=65536)
    wait_seconds: int = Field(default=0, ge=0, le=86400)
    system_features: tuple[str, ...] = ()
    without_system: tuple[str, ...] = ()
    desktop_password_hash: SecretStr | None = Field(default=None, repr=False)
    no_desktop_password: bool = False

    @classmethod
    def from_domain(cls, request: CreateRequest) -> "CreateParameters":
        try:
            return cls.model_validate(
                {field.name: getattr(request, field.name) for field in fields(CreateRequest)}
            )
        except ValidationError as exc:
            raise VmctlError(
                "Invalid creation request; supply public-key contents and valid parameters"
            ) from exc

    def to_domain(self) -> CreateRequest:
        return CreateRequest(**{name: getattr(self, name) for name in type(self).model_fields})


class EmptyParameters(WireModel):
    pass


class ReferenceParameters(WireModel):
    reference: str = Field(min_length=1, max_length=128)


class StatsParameters(ReferenceParameters):
    history: bool = False


class DeleteParameters(WireModel):
    vmid: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=63)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_preview(cls, preview: DeletePreview) -> "DeleteParameters":
        return cls(vmid=preview.vm.vmid, name=preview.vm.name, fingerprint=preview.fingerprint)


class PlanActionParameters(ReferenceParameters):
    action: LifecycleAction


class ActionParameters(WireModel):
    vmid: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=63)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: str = Field(min_length=1, max_length=32)

    @classmethod
    def from_preview(cls, preview: ActionPreview) -> "ActionParameters":
        return cls(
            vmid=preview.vm.vmid,
            name=preview.vm.name,
            status=preview.vm.status,
            fingerprint=preview.fingerprint,
        )


class RequestBase(WireModel):
    protocol_version: Literal[1] = PROTOCOL_VERSION
    request_id: RequestID


class CreateMessage(RequestBase):
    operation: Literal["create", "plan_create"]
    parameters: CreateParameters


class ReferenceMessage(RequestBase):
    operation: Literal["info", "plan_delete"]
    parameters: ReferenceParameters


class StatsMessage(RequestBase):
    operation: Literal["stats"] = "stats"
    parameters: StatsParameters


class DeleteMessage(RequestBase):
    operation: Literal["delete"] = "delete"
    parameters: DeleteParameters


class PlanActionMessage(RequestBase):
    operation: Literal["plan_action"] = "plan_action"
    parameters: PlanActionParameters


class ActionMessage(RequestBase):
    operation: LifecycleAction
    parameters: ActionParameters


class QueryMessage(RequestBase):
    operation: Literal["list", "templates", "presets", "validate_config", "catalog"]
    parameters: EmptyParameters = Field(default_factory=EmptyParameters)


Request = Annotated[
    CreateMessage
    | ReferenceMessage
    | StatsMessage
    | DeleteMessage
    | PlanActionMessage
    | ActionMessage
    | QueryMessage,
    Field(discriminator="operation"),
]
REQUEST_ADAPTER: TypeAdapter[Request] = TypeAdapter(Request)


class Event(WireModel):
    protocol_version: Literal[1] = PROTOCOL_VERSION
    request_id: RequestID
    kind: Literal["hello", "progress", "result", "error"]
    data: JsonValue = None
    message: str = ""
    code: str = ""
    vmid: int | None = Field(default=None, gt=0)


def encode_request(request: Request) -> bytes:
    """The only serializer permitted to unwrap a credential for the SSH pipe."""
    data = request.model_dump(mode="json")
    if isinstance(request, CreateMessage) and request.parameters.desktop_password_hash is not None:
        data["parameters"]["desktop_password_hash"] = (
            request.parameters.desktop_password_hash.get_secret_value()
        )
    encoded = (json.dumps(data, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise VmctlError("Request exceeds protocol size limit")
    return encoded
