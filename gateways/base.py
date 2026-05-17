from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


class GatewayConfigurationError(RuntimeError):
    """Raised when an enabled gateway is missing required configuration."""


@dataclass(frozen=True)
class GatewayAttachment:
    content_type: str
    url: str | None = None
    file_name: str | None = None
    size_bytes: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GatewayUser:
    gateway_user_id: str
    username: str | None = None
    display_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GatewayChat:
    gateway_chat_id: str
    title: str | None = None
    chat_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IncomingGatewayMessage:
    workspace_id: UUID
    user_id: UUID
    gateway_name: str
    gateway_user: GatewayUser
    gateway_chat: GatewayChat
    text: str
    gateway_message_id: str | None = None
    attachments: tuple[GatewayAttachment, ...] = ()
    raw_payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class OutgoingGatewayMessage:
    gateway_name: str
    text: str
    gateway_chat_id: str | None = None
    gateway_user_id: str | None = None
    reply_to_message_id: str | None = None
    attachments: tuple[GatewayAttachment, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GatewayResponse:
    text: str
    task_id: UUID | None = None
    approval_id: UUID | None = None
    should_reply: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GatewayHealth:
    status: str
    detail: str | None = None


class Gateway(ABC):
    name: str

    @abstractmethod
    async def start(self) -> None:
        ...

    @abstractmethod
    async def stop(self) -> None:
        ...

    @abstractmethod
    async def send_message(self, message: OutgoingGatewayMessage) -> None:
        ...

    @abstractmethod
    async def health_check(self) -> GatewayHealth:
        ...