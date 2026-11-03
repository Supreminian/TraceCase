"""Bounded, resumable Agent execution over replaceable model and storage ports."""

from __future__ import annotations

import json
import hashlib
import time
from collections.abc import Callable
from typing import Any, Protocol
from uuid import uuid4

from jsonschema import Draft202012Validator
from pydantic import BaseModel

from .domain import (
    Assistant, AssistantMessage, Context, Decision, ModelSettings, Reviewer,
    Session, T, ToolCall, ToolResult, UserMessage,
)


class AgentError(RuntimeError):
    """Public-safe failure; provider exception strings must not be exposed."""


class SessionNotFoundError(AgentError):
    pass


class SessionConflictError(AgentError):
    pass


class AuthorizationError(AgentError):
    pass


class AgentBusyError(AgentError):
    pass


class PublicToolError(AgentError):
    """A trusted business validation failure that may be shown to the model/user."""

    def __init__(self, public_message: str):
        self.public_message = str(public_message).strip()[:4000] or "此工具暂时无法完成当前操作。"
        super().__init__(self.public_message)


# Compatible name for adapters that call the controlled error an execution failure.
ToolExecutionError = PublicToolError


class ModelUnavailableError(AgentError):
    """An upstream model failure: stop the turn instead of asking that model to retry."""


class UserInputRequired(AgentError):
    """A trusted tool needs a human decision before proceeding; this is not a failure."""

    def __init__(self, prompt: str, kind: str = "generation_plan", choices: list[str] | None = None,
                 plan_hash: str | None = None):
        self.prompt = str(prompt).strip()[:4000] or "请确认下一步操作。"
        self.kind = str(kind)[:80]
        self.choices = [str(choice)[:200] for choice in (choices or [])[:8]]
        self.plan_hash = plan_hash
        super().__init__(self.prompt)


class ToolDelivered(AgentError):
    """A tool delivered a public artifact and the Agent chose to end this reply."""

    def __init__(self, content: str):
        self.content = content
        super().__init__('Tool result delivered')


class TaskHandoff(AgentError):
    """A trusted business tool has durably created a related task to continue in."""

    def __init__(self, child_task_id: str, content: str):
        if not isinstance(child_task_id, str) or not 1 <= len(child_task_id) <= 200:
            raise ValueError("关联任务标识无效。")
        self.child_task_id = child_task_id
        self.content = str(content).strip()[:4000] or "已建立关联任务，正在继续处理。"
        super().__init__(self.content)


class ModelClient(Protocol):
    def generate(self, context: Context[T], settings: ModelSettings) -> T: ...


class SessionStore(Protocol):
    def list_sessions(self) -> list[Session]: ...

    def save(self, session: Session, expected_revision: int | None = None) -> None: ...

    def get(self, session_id: str) -> Session | None: ...


EventHandler = Callable[[dict[str, Any]], None]
Authorizer = Callable[[Session, ToolCall | None], bool | Decision | None]


class AgentService:
    def __init__(self, model_client: ModelClient, session_store: SessionStore,
                 assistant: Assistant, reviewer: Reviewer,
                 assistant_settings: ModelSettings, reviewer_settings: ModelSettings,
                 max_iterations: int = 8, max_tool_calls: int = 24,
                 authorize: Authorizer | None = None, max_duration_seconds: float = 300,
                 pause_on_tool_error: bool = False,
                 tool_review_policy: Callable[[Session, ToolCall], Decision | None] | None = None):
        if not 1 <= max_iterations <= 100 or not 1 <= max_tool_calls <= 500:
            raise ValueError("Agent iteration and tool limits must be bounded positive values.")
        if not 0 < max_duration_seconds <= 3600:
            raise ValueError("Agent duration must be between 0 and 3600 seconds.")
        names = [tool.name for tool in assistant.tools]
        if len(set(names)) != len(names):
            raise ValueError("Tool names must be unique.")
        for tool in assistant.tools:
            # Reject remote references; parameter validation never makes network requests.
            self._check_local_schema(tool.parameters)
            Draft202012Validator.check_schema(tool.parameters)
        self.model_client = model_client
        self.session_store = session_store
        self.assistant = assistant
        self.reviewer = reviewer
        self.assistant_settings = assistant_settings
        self.reviewer_settings = reviewer_settings
        self.max_iterations = max_iterations
        self.max_tool_calls = max_tool_calls
        self.max_duration_seconds = max_duration_seconds
        self.authorize = authorize
        self.pause_on_tool_error = pause_on_tool_error
        self.tool_review_policy = tool_review_policy
        self._tools = {tool.name: tool for tool in assistant.tools}

    @staticmethod
    def _check_local_schema(value: Any) -> None:
        if isinstance(value, dict):
            for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
                reference = value.get(keyword)
                if reference is not None and (not isinstance(reference, str)
                                              or not reference.startswith("#")):
                    raise ValueError("Tool schemas may only use local references.")
            for child in value.values():
                AgentService._check_local_schema(child)
        elif isinstance(value, list):
            for child in value:
                AgentService._check_local_schema(child)

    def _authorize(self, session: Session, call: ToolCall | None = None) -> None:
        if self.authorize is None:
            return
        try:
            result = self.authorize(session.model_copy(deep=True), call)
        except Exception:
            raise AuthorizationError("当前账号无权继续此任务，请重新确认项目权限。") from None
        if result is False or isinstance(result, Decision) and not result.should_allow:
            raise AuthorizationError("当前账号无权继续此任务，请重新确认项目权限。")

    @staticmethod
    def _emit(on_event: EventHandler | None, event_type: str, session: Session,
              **fields: Any) -> None:
        if on_event is not None:
            try:
                on_event({"type": event_type, "session_id": session.id, **fields})
            except Exception:
                # Presentation callbacks cannot roll back or corrupt persisted execution.
                pass

    def _save(self, session: Session, expected_revision: int) -> None:
        self.session_store.save(session, expected_revision=expected_revision)

    def _append(self, session: Session, message: BaseModel) -> None:
        previous = session.revision
        session.append(message)
        self._save(session, previous)

    def _touch(self, session: Session) -> None:
        previous = session.revision
        session.revision += 1
        self._save(session, previous)

    def new_session(self, metadata: dict[str, Any] | None = None) -> Session:
        session = Session(id=uuid4().hex, metadata=dict(metadata or {}))
        self._authorize(session)
        self.session_store.save(session)
        return session

    def list_sessions(self) -> list[Session]:
        visible = []
        for session in self.session_store.list_sessions():
            try:
                self._authorize(session)
            except AuthorizationError:
                continue
            visible.append(session)
        return visible

    def get_session(self, session_id: str) -> Session:
        session = self.session_store.get(session_id)
        if session is None:
            raise SessionNotFoundError("未找到 Agent 任务。")
        self._authorize(session)
        return session

    @staticmethod
    def _pending(session: Session) -> list[ToolCall]:
        answered = {m.tool_call_id for m in session.messages if isinstance(m, ToolResult)}
        pending = []
        seen = set()
        for message in session.messages:
            if isinstance(message, AssistantMessage):
                for call in message.tool_calls:
                    if call.id not in answered and call.id not in seen:
                        pending.append(call)
                        seen.add(call.id)
        return pending

    def interrupt_orphaned_turn(self, session_id: str, expected_request_id: str) -> bool:
        """Settle abandoned protocol calls without replaying their side effects.

        The application must first prove that the owning durable job is terminal.
        Optimistic session revisions prevent concurrent recovery from overwriting work.
        """
        session = self.get_session(session_id)
        state = session.metadata.get('agent_turn', {})
        if state.get('status') != 'running' or state.get('request_id') != expected_request_id:
            return False
        self._authorize(session)
        for call in self._pending(session):
            self._append(session, ToolResult(tool_call_id=call.id, is_error=True,
                content='原执行请求已结束，此未完成调用不会自动重放；请依据用户新消息重新决定下一步。'))
        state.update(status='failed', error='原执行请求已中断，允许接收新的用户消息。')
        self._touch(session)
        return True

    def send_message(self, session_id: str, content: str,
                     on_event: EventHandler | None = None, *,
                     request_id: str | None = None) -> Session:
        if not isinstance(content, str) or not content.strip():
            raise ValueError("请输入任务目标或反馈。")
        if len(content) > 100_000:
            raise ValueError("消息超过允许的长度。")
        if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128):
            raise ValueError("请求标识无效。")
        session = self.get_session(session_id)
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        previous_request = session.metadata.get("agent_requests", {}).get(request_id) if request_id else None
        if previous_request:
            if previous_request["content_hash"] != content_hash:
                raise ValueError("请求标识已用于不同消息。")
            current = session.metadata.get("agent_turn", {})
            if current.get("id") != previous_request["turn_id"] or current.get("status") in {"completed", "limited", "waiting_confirmation"}:
                return session
            return self.resume(session_id, on_event=on_event)
        waiting = session.metadata.get("agent_turn", {}).get("status") == "waiting_confirmation"
        if (self._pending(session) and not waiting) or session.metadata.get("agent_turn", {}).get("status") == "running":
            raise AgentBusyError("当前任务尚未结束，请先恢复或等待当前步骤。")
        if waiting:
            # A new human instruction supersedes queued calls from the paused plan.
            # Settle their protocol results without executing their side effects.
            for call in self._pending(session):
                self._append(session, ToolResult(tool_call_id=call.id, is_error=True,
                    content="此调用在等待人工确认时暂停，未执行；请根据用户的新消息重新规划。"))
        session.metadata["agent_turn"] = {
            "id": uuid4().hex, "iterations": 0, "tool_calls": 0,
            "elapsed_seconds": 0.0, "status": "running",
        }
        if request_id:
            session.metadata["agent_turn"]["request_id"] = request_id
            session.metadata.setdefault("agent_requests", {})[request_id] = {
                "turn_id": session.metadata["agent_turn"]["id"], "content_hash": content_hash,
            }
        self._append(session, UserMessage(content=content))
        return self._run(session, on_event)

    def resume(self, session_id: str, on_event: EventHandler | None = None) -> Session:
        session = self.get_session(session_id)
        state = session.metadata.get("agent_turn")
        if not state:
            # Support sessions imported from a conversation store with interrupted tool calls.
            if not self._pending(session):
                return session
            session.metadata["agent_turn"] = {
                "id": uuid4().hex, "iterations": 0, "tool_calls": 0,
                "elapsed_seconds": 0.0, "status": "running",
            }
            self._touch(session)
        elif state["status"] == "waiting_confirmation":
            confirmation = state.get("confirmation")
            if isinstance(confirmation, dict):
                self._emit(on_event, "assistant_message", session,
                    message_id=confirmation["message_id"], content=confirmation["content"],
                    tool_calls=[], tool_count=0)
                self._emit(on_event, "waiting_confirmation", session, **confirmation)
            else:
                return self._wait_confirmation(session, on_event,
                    **state.get("pending_confirmation", {"error": state.get("error", "")}))
            return session
        elif state["status"] in {"completed", "limited"}:
            return session
        else:
            state["status"] = "running"
            state.pop("error", None)
            self._touch(session)
        return self._run(session, on_event)

    def _settings(self, settings: ModelSettings, remaining: float) -> ModelSettings:
        return settings.model_copy(update={"timeout_seconds": max(0.001, min(
            settings.timeout_seconds, remaining))})

    def _model_event(self, session: Session, result: BaseModel,
                     role: str, on_event: EventHandler | None) -> None:
        event = getattr(result, "metadata", {}).get("model_event")
        if not isinstance(event, dict):
            event = getattr(self.model_client, "last_event", None)
        if isinstance(event, dict):
            # Adapter events must contain usage/configuration only, never requests or secrets.
            allowed = {key: event[key] for key in (
                "provider", "model_id", "model", "request_id", "usage", "latency_ms",
                "input_tokens", "output_tokens", "cost", "finish_reason",
            ) if key in event}
            allowed["role"] = role
            # Sanitize the message copy as well as the aggregate execution event.
            getattr(result, "metadata", {})["model_event"] = dict(allowed)
            session.metadata.setdefault("model_events", []).append(allowed)
            self._emit(on_event, "model_usage", session, **allowed)

    def _finish(self, session: Session, status: str, content: str,
                on_event: EventHandler | None) -> Session:
        self._authorize(session)
        session.metadata["agent_turn"]["status"] = status
        message_id = uuid4().hex
        self._append(session, AssistantMessage(content=content, metadata={"public_message_id": message_id}))
        self._emit(on_event, "assistant_message", session, message_id=message_id,
                   content=content, tool_count=0, tool_calls=[])
        self._emit(on_event, status, session, content=content)
        return session

    def _wait_confirmation(self, session: Session, on_event: EventHandler | None,
                           *, tool: str = "", tool_call_id: str = "", error: str = "",
                           prompt: str = "", kind: str = "retry_generation",
                           choices: list[str] | None = None, plan_hash: str | None = None) -> Session:
        self._authorize(session)
        state = session.metadata["agent_turn"]
        prompt = prompt or ("这一步未能完成，已有成果已保留。" + ("原因：" + error if error else "")
                            + "\n请确认：重试这一步、补充需求后继续，或停止本次任务。")
        message_id = uuid4().hex
        state.update(status="waiting_confirmation", error=error, confirmation_kind=kind)
        confirmation = {"message_id": message_id, "content": prompt, "prompt": prompt,
            "kind": kind, "tool": tool, "tool_call_id": tool_call_id, "error": error,
            "choices": choices or [], "plan_hash": plan_hash,
            "suggested_next_action": "请回复重试、补充需求，或停止任务。" if error else "请确认计划，或说明需要调整的范围。"}
        state["confirmation"] = confirmation
        state.pop("pending_confirmation", None)
        # Close the original batch's remaining calls with explicit nonexecution
        # results before adding an assistant response (required by provider protocols).
        # A later user instruction starts a fresh plan, never an old hidden side effect.
        for pending_call in self._pending(session):
            self._append(session, ToolResult(tool_call_id=pending_call.id, is_error=True,
                content="此调用因等待人工确认而暂停，未执行；用户回复后必须重新规划调用。"))
        self._append(session, AssistantMessage(content=prompt, metadata={"public_message_id": message_id}))
        self._emit(on_event, "assistant_message", session, message_id=message_id,
                   content=prompt, tool_calls=[], tool_count=0)
        self._emit(on_event, "waiting_confirmation", session, **confirmation)
        return session

    def _run(self, session: Session, on_event: EventHandler | None) -> Session:
        state = session.metadata["agent_turn"]
        started = time.monotonic()
        prior_elapsed = float(state.get("elapsed_seconds", 0))

        def remaining() -> float:
            state["elapsed_seconds"] = prior_elapsed + time.monotonic() - started
            return self.max_duration_seconds - state["elapsed_seconds"]

        try:
            while True:
                self._authorize(session)
                pending = self._pending(session)
                if remaining() <= 0:
                    return self._stop_with_pending(session, "已达到本轮运行时长上限，已保留现有结果。", on_event)
                if pending:
                    for call in pending:
                        if remaining() <= 0 or state["tool_calls"] >= self.max_tool_calls:
                            return self._stop_with_pending(session, "已达到本轮工具或时长上限，已保留现有结果。", on_event)
                        self._execute_call(session, call, remaining, on_event)
                        if state["status"] in {"waiting_confirmation", "completed"}:
                            return session
                        if state["status"] == "limited":
                            return self._stop_with_pending(session, state["limit_reason"], on_event)
                    continue
                if state["iterations"] >= self.max_iterations:
                    return self._finish(session, "limited", "已达到本轮自动处理上限。请检查现有结果后补充目标或继续新一轮。", on_event)
                state["iterations"] += 1
                self._touch(session)
                self._emit(on_event, "model_started", session, role="assistant")
                message_id = uuid4().hex
                self._emit(on_event, "assistant_start", session, message_id=message_id)

                def on_delta(delta: str) -> None:
                    self._authorize(session)
                    if isinstance(delta, str) and delta:
                        self._emit(on_event, "assistant_delta", session, message_id=message_id, text=delta)

                stream = getattr(self.model_client, "generate_stream", None)
                context = self.assistant.context(session)
                configured = self._settings(self.assistant_settings, remaining())
                result = stream(context, configured, on_delta) if callable(stream) else self.model_client.generate(context, configured)
                remaining()
                self._authorize(session)
                if not isinstance(result, AssistantMessage):
                    raise TypeError("Assistant model returned an unexpected type")
                self._model_event(session, result, "assistant", on_event)
                result.metadata["public_message_id"] = message_id
                if not result.tool_calls:
                    state["status"] = "completed"
                self._append(session, result)
                self._emit(on_event, "assistant_message", session,
                           message_id=message_id, content=result.content, tool_count=len(result.tool_calls),
                           tool_calls=[{"id": call.id, "name": call.name} for call in result.tool_calls])
                if not result.tool_calls:
                    self._emit(on_event, "completed", session)
                    return session
        except (SessionConflictError, AuthorizationError):
            raise
        except UserInputRequired as exc:
            return self._wait_confirmation(session, on_event, prompt=exc.prompt,
                kind=exc.kind, choices=exc.choices, plan_hash=exc.plan_hash)
        except Exception:
            if self.pause_on_tool_error:
                remaining()
                return self._wait_confirmation(session, on_event,
                    error="模型服务或任务处理暂时不可用，本步骤未能完成。")
            state["status"] = "failed"
            state["error"] = "模型或任务执行暂时失败，历史已保存，可恢复当前任务。"
            remaining()
            self._touch(session)
            self._emit(on_event, "failed", session, error=state["error"])
            return session

    def _stop_with_pending(self, session: Session, reason: str,
                           on_event: EventHandler | None) -> Session:
        for call in self._pending(session):
            self._append(session, ToolResult(tool_call_id=call.id, content=reason, is_error=True))
        return self._finish(session, "limited", reason, on_event)

    def _tool_result(self, session: Session, call: ToolCall, result: ToolResult) -> None:
        state = session.metadata["agent_turn"]
        failures = state.setdefault("tool_failures", {})
        failures[call.name] = failures.get(call.name, 0) + 1 if result.is_error else 0
        if result.is_error and self.pause_on_tool_error:
            # Persist the barrier with the failed result, before emitting any UI event.
            # A crash in between must never silently continue the old plan.
            state.update(status="waiting_confirmation", error=result.content,
                         confirmation_kind="retry_generation")
            state["pending_confirmation"] = {"tool": call.name,
                "tool_call_id": call.id, "error": result.content}
        if failures[call.name] >= 2 and not self.pause_on_tool_error:
            state["status"] = "limited"
            state["limit_reason"] = (
                "同一工具连续处理失败，已停止自动重试。请查看具体校验信息，"
                "检查需求、参数或模型配置后继续。")
        self._append(session, result)

    def _execute_call(self, session: Session, call: ToolCall,
                      remaining: Callable[[], float], on_event: EventHandler | None) -> None:
        self._authorize(session, call)
        state = session.metadata["agent_turn"]
        state["tool_calls"] += 1
        ledger = session.metadata.setdefault("tool_executions", {})
        if call.id in ledger:
            # A crash after an external side effect has an unknown outcome: never execute twice.
            error = "此工具调用曾开始执行但结果未完整保存。为避免重复操作，请核对结果后发起新调用。"
            self._tool_result(session, call, ToolResult(tool_call_id=call.id, is_error=True, content=error))
            if self.pause_on_tool_error:
                self._wait_confirmation(session, on_event, tool=call.name, tool_call_id=call.id, error=error)
            return
        tool = self._tools.get(call.name)
        error = None
        if tool is None:
            error = "此工具不可用，请使用已授权的工具。"
        else:
            try:
                errors = sorted(Draft202012Validator(tool.parameters).iter_errors(call.arguments),
                                key=lambda item: str(list(item.path)))
                if errors:
                    # Values are intentionally omitted from validation errors.
                    locations = [".".join(str(p) for p in e.path) or "$" for e in errors[:5]]
                    error = "工具参数未通过结构校验：" + "、".join(locations)
            except Exception:
                error = "工具参数结构配置不可用，本次调用已拒绝。"
        if error is None:
            self._emit(on_event, "review_started", session, tool_call_id=call.id, tool=call.name)
            try:
                self._authorize(session)
                decision = self.tool_review_policy(session, call) if self.tool_review_policy else None
                policy_review = decision is not None
                if not policy_review:
                    decision = self.model_client.generate(
                        self.reviewer.context(session, call), self._settings(self.reviewer_settings, remaining()))
                remaining()
                self._authorize(session)
                if not isinstance(decision, Decision):
                    raise TypeError("Reviewer returned an unexpected type")
                if policy_review:
                    self._emit(on_event, "tool_policy_reviewed", session, tool=call.name,
                               tool_call_id=call.id, allowed=decision.should_allow, reason=decision.reason)
                else:
                    self._model_event(session, decision, "reviewer", on_event)
                if not decision.should_allow:
                    error = "工具审核未通过：" + (decision.reason or "此调用未获授权。")
            except AuthorizationError:
                raise
            except UserInputRequired:
                # Resource limits are a human decision, not a reviewer denial.
                raise
            except ModelUnavailableError:
                if not self.pause_on_tool_error:
                    raise
                error = "工具安全审核所需的模型服务暂时不可用，工具未执行。"
            except Exception:
                error = "工具审核暂时不可用，本次调用已拒绝。"
        if error is not None:
            self._tool_result(session, call, ToolResult(tool_call_id=call.id, content=error, is_error=True))
            self._emit(on_event, "tool_denied", session, tool_call_id=call.id,
                       tool=call.name, reason=error)
            if self.pause_on_tool_error:
                self._wait_confirmation(session, on_event, tool=call.name, tool_call_id=call.id, error=error)
            return
        self._authorize(session, call)
        if remaining() <= 0:
            error = "运行已超时，工具未执行。"
            self._tool_result(session, call, ToolResult(tool_call_id=call.id, is_error=True, content=error))
            if self.pause_on_tool_error:
                self._wait_confirmation(session, on_event, tool=call.name, tool_call_id=call.id, error=error)
            return
        ledger[call.id] = {"status": "started", "name": call.name}
        self._touch(session)  # Durable claim precedes the side effect.
        self._emit(on_event, "tool_started", session, tool_call_id=call.id, tool=call.name)
        try:
            content = tool.execute(dict(call.arguments))
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            if len(content) > 200_000:
                result = ToolResult(tool_call_id=call.id, is_error=True,
                                    content="工具返回内容过长，请缩小读取范围后重试。")
            else:
                result = ToolResult(tool_call_id=call.id, content=content)
        except ToolDelivered as exc:
            ledger[call.id]['status'] = 'completed'
            state['status'] = 'completed'
            self._append(session, ToolResult(tool_call_id=call.id, content=exc.content))
            for pending_call in self._pending(session):
                self._append(session, ToolResult(tool_call_id=pending_call.id, is_error=True,
                    content='Agent已选择直接展示结果并结束本轮，此排队调用未执行。'))
            self._emit(on_event, 'tool_completed', session, tool_call_id=call.id,
                       tool=call.name, is_error=False)
            self._emit(on_event, 'completed', session)
            return
        except TaskHandoff as exc:
            ledger[call.id]["status"] = "completed"
            state.update(status="completed", child_task_id=exc.child_task_id)
            self._append(session, ToolResult(tool_call_id=call.id,
                content=json.dumps({"child_task_id": exc.child_task_id, "message": exc.content}, ensure_ascii=False)))
            for pending_call in self._pending(session):
                self._append(session, ToolResult(tool_call_id=pending_call.id,
                    content="已转到关联任务，此调用未执行。"))
            message_id = uuid4().hex
            self._append(session, AssistantMessage(content=exc.content,
                metadata={"public_message_id": message_id}))
            self._emit(on_event, "tool_completed", session, tool_call_id=call.id,
                       tool=call.name, is_error=False)
            self._emit(on_event, "assistant_message", session, message_id=message_id,
                       content=exc.content, tool_count=0, tool_calls=[])
            self._emit(on_event, "completed", session)
            self._emit(on_event, "task_handoff", session, child_task_id=exc.child_task_id,
                       content=exc.content)
            return
        except UserInputRequired as exc:
            ledger[call.id]["status"] = "waiting_confirmation"
            state.update(status="waiting_confirmation", error="", confirmation_kind=exc.kind)
            state["pending_confirmation"] = {"tool": call.name, "tool_call_id": call.id,
                "prompt": exc.prompt, "kind": exc.kind, "choices": exc.choices, "plan_hash": exc.plan_hash}
            self._append(session, ToolResult(tool_call_id=call.id, content="等待用户确认：" + exc.prompt))
            self._wait_confirmation(session, on_event, tool=call.name, tool_call_id=call.id,
                                    prompt=exc.prompt, kind=exc.kind, choices=exc.choices,
                                    plan_hash=exc.plan_hash)
            return
        except ModelUnavailableError:
            # This controlled error states that no result was applied. Keep the call
            # pending for an explicit resume, without reusing an uncertain execution claim.
            if not self.pause_on_tool_error:
                del ledger[call.id]
                self._touch(session)
                raise
            result = ToolResult(tool_call_id=call.id, is_error=True,
                                content="生成所需的模型服务暂时不可用，此步骤没有产出可用结果。")
        except PublicToolError as exc:
            result = ToolResult(tool_call_id=call.id, is_error=True, content=exc.public_message)
        except Exception:
            result = ToolResult(tool_call_id=call.id, is_error=True,
                                content="工具执行失败，未保存异常详情；请检查任务状态后重试。")
        remaining()
        self._authorize(session)
        ledger[call.id]["status"] = "completed"
        self._tool_result(session, call, result)
        self._emit(on_event, "tool_completed", session, tool_call_id=call.id,
                   tool=call.name, is_error=result.is_error)
        if result.is_error and self.pause_on_tool_error:
            self._wait_confirmation(session, on_event, tool=call.name, tool_call_id=call.id,
                                    error=result.content)
