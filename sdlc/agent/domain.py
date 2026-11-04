"""Provider-neutral Agent contracts; only public answers and execution facts are stored."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class AgentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ToolCall(AgentModel):
    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=120)
    arguments: dict[str, Any]


class UserMessage(AgentModel):
    role: Literal["user"] = "user"
    content: str


class AssistantMessage(AgentModel):
    role: Literal["assistant"] = "assistant"
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=64)
    # Provider continuity tokens may be opaque. Private reasoning text is never requested.
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolResult(AgentModel):
    role: Literal["tool"] = "tool"
    tool_call_id: str = Field(min_length=1, max_length=200)
    content: str
    is_error: bool = False


Message = Annotated[UserMessage | AssistantMessage | ToolResult, Field(discriminator="role")]
_message_adapter = TypeAdapter(Message)


class Session(AgentModel):
    id: str = Field(min_length=1, max_length=200)
    messages: list[Message] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    revision: int = Field(default=0, ge=0)

    def append(self, message: Message | dict[str, Any]) -> None:
        self.messages.append(_message_adapter.validate_python(message))
        self.revision += 1


class Decision(AgentModel):
    should_allow: bool
    reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelSettings(AgentModel):
    provider: str = "google"
    model_id: str = Field(min_length=1, max_length=200)
    effort: str = "medium"
    max_tokens: int = Field(default=8192, ge=1, le=131072)
    timeout_seconds: float = Field(default=120, gt=0, le=600)


class Tool(ABC):
    """A narrow capability; execute must enforce its own business authorization."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]: ...

    @abstractmethod
    def execute(self, arguments: dict[str, Any]) -> str: ...

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "parameters": self.parameters}


T = TypeVar("T", bound=BaseModel)


@dataclass
class Context(Generic[T]):
    instruction: str
    tools: list[Tool]
    messages: list[Message]
    output_type: type[T]
    memory: str = ""
    agents_md: str = ""


@dataclass
class Assistant:
    instruction: str
    tools: list[Tool] = field(default_factory=list)
    memory: str = ""
    agents_md: str = ""

    def context(self, session: Session) -> Context[AssistantMessage]:
        return Context(self.instruction, list(self.tools),
                       [m.model_copy(deep=True) for m in session.messages],
                       AssistantMessage, self.memory, self.agents_md)


@dataclass
class Reviewer:
    instruction: str
    memory: str = ""
    agents_md: str = ""

    def context(self, session: Session, call: ToolCall) -> Context[Decision]:
        messages = [m.model_copy(deep=True) for m in session.messages]
        messages.append(UserMessage(content=json.dumps({
            "review_tool_call": call.model_dump(),
            "policy": "Return a public allow/deny decision. Do not output private reasoning.",
        }, ensure_ascii=False)))
        return Context(self.instruction, [], messages, Decision, self.memory, self.agents_md)


ToolCallReviewer = Reviewer
