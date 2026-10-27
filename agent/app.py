"""Minimal A2A agent: LangGraph + OpenAI, JSON-RPC message/send."""

import json
import os
from collections.abc import Sequence

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentProvider,
    AgentSkill,
    InvalidParamsError,
    Part,
    TextPart,
    UnsupportedOperationError,
)
from a2a.utils import new_agent_text_message, new_task
from a2a.utils.errors import ServerError
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from quota import LIMITED_METHODS, MessageQuota, message_for, rpc_error

DEFAULT_PUBLIC_URL = "https://agent.agiq.top"
DEFAULT_MODEL = "gpt-4o-mini"
DOCS_URL = "https://agiq.top"
ICON_URL = "https://agiq.top/icon.svg"
CARD_PATHS = frozenset({
    "/.well-known/agent-card.json",
    "/.well-known/agent.json",
})
SYSTEM_PROMPT = (
    "Eres un asistente breve y útil. Responde en el idioma del usuario."
)


class FakeChatModel(BaseChatModel):
    """Echo model used only when A2A_FAKE_LLM=1 (local tests, no API key)."""

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001
        return self

    def _generate(
        self,
        messages: Sequence[BaseMessage],
        stop: list[str] | None = None,
        run_manager=None,  # noqa: ANN001
        **kwargs,
    ) -> ChatResult:
        text = ""
        for message in reversed(messages):
            if isinstance(message, HumanMessage) and isinstance(message.content, str):
                text = message.content.strip()
                break
        reply = AIMessage(content=f"eco: {text}")
        return ChatResult(generations=[ChatGeneration(message=reply)])


def use_fake_llm() -> bool:
    return os.environ.get("A2A_FAKE_LLM", "").strip().lower() in {"1", "true", "yes"}


def public_url() -> str:
    return os.environ.get("PUBLIC_URL", DEFAULT_PUBLIC_URL).strip().rstrip("/")


def model_name() -> str:
    return os.environ.get("OPENAI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def build_model() -> BaseChatModel:
    if use_fake_llm():
        return FakeChatModel()
    # ChatOpenAI reads OPENAI_API_KEY from the environment. Do not pass the key here.
    return ChatOpenAI(model=model_name())


def build_graph():
    return create_react_agent(build_model(), tools=[], prompt=SYSTEM_PROMPT)


def text_from_result(result: dict) -> str:
    content = result["messages"][-1].content
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, str):
                chunks.append(block)
            elif isinstance(block, dict):
                chunks.append(str(block.get("text") or ""))
        return "".join(chunks).strip()
    return str(content).strip()


class ChatExecutor(AgentExecutor):
    def __init__(self, graph) -> None:
        self._graph = graph

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_text = context.get_user_input().strip()
        if not user_text or context.message is None:
            raise ServerError(InvalidParamsError(message="El mensaje no tiene texto."))

        task = context.current_task
        if task is None:
            task = new_task(context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue, task.id, task.context_id)
        try:
            result = await self._graph.ainvoke({"messages": [("user", user_text)]})
            reply = text_from_result(result) or "(sin respuesta)"
        except Exception as exc:
            detail = str(exc).splitlines()[0][:500]
            await updater.failed(
                new_agent_text_message(f"No pude responder: {detail}", task.context_id, task.id)
            )
            return

        await updater.add_artifact([Part(root=TextPart(text=reply))], name="respuesta")
        await updater.complete(new_agent_text_message(reply, task.context_id, task.id))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise ServerError(UnsupportedOperationError(message="Cancelación no disponible."))


def build_agent_card() -> AgentCard:
    return AgentCard(
        name="agiq",
        description="Agente de conversación general de agiq.top.",
        url=public_url(),
        version="0.1.0",
        documentation_url=DOCS_URL,
        icon_url=ICON_URL,
        provider=AgentProvider(organization="MABC80", url=DOCS_URL),
        protocol_version="0.3.0",
        preferred_transport="JSONRPC",
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="chat",
                name="Conversación",
                description="Conversación general en texto.",
                tags=["chat"],
                examples=["Hola, ¿qué puedes hacer?"],
                input_modes=["text/plain"],
                output_modes=["text/plain"],
            )
        ],
    )


def card_dict() -> dict:
    """Same object the JSON-RPC server returns for the agent card."""
    return build_agent_card().model_dump(exclude_none=True, by_alias=True)


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


class Application(A2AStarletteApplication):
    def routes(self, *args, **kwargs):
        return [
            *super().routes(*args, **kwargs),
            Route("/health", health, methods=["GET"]),
        ]


def client_ip(scope) -> str:
    headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers") or []
    }
    forwarded = headers.get("x-forwarded-for", "")
    if forwarded.strip():
        return forwarded.split(",")[0].strip() or "unknown"
    client = scope.get("client")
    if client and client[0]:
        return client[0]
    return "unknown"


def classify_body(body: bytes) -> tuple[str, str | int | None]:
    """Return ('limited'|'other'|'plain', json-rpc id)."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "plain", None
    if not isinstance(payload, dict) or "method" not in payload:
        return "plain", None
    request_id = payload.get("id")
    if not isinstance(request_id, str | int):
        request_id = None
    if payload.get("method") in LIMITED_METHODS:
        return "limited", request_id
    return "other", request_id


class QuotaMiddleware:
    """Count message/send and message/stream. Leave discovery and health alone."""

    def __init__(self, app, quota: MessageQuota) -> None:
        self.app = app
        self.quota = quota

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST" or scope.get("path") != "/":
            await self.app(scope, receive, send)
            return

        chunks = []
        body = b""
        while True:
            message = await receive()
            chunks.append(message)
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break

        async def replay():
            if chunks:
                return chunks.pop(0)
            return {"type": "http.disconnect"}

        kind, request_id = classify_body(body)
        ip = client_ip(scope)
        if kind == "limited":
            reason = self.quota.consume(ip)
            if reason:
                response = JSONResponse(rpc_error(request_id, reason))
                await response(scope, replay, send)
                return
        elif kind == "plain":
            reason = self.quota.reason(ip)
            if reason:
                response = JSONResponse(
                    {"error": message_for(reason)},
                    status_code=429,
                )
                await response(scope, replay, send)
                return
        await self.app(scope, replay, send)


class CardCorsMiddleware:
    """Allow any origin to read the agent card. Do not open POST from browsers."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") not in CARD_PATHS:
            await self.app(scope, receive, send)
            return
        if scope.get("method") == "OPTIONS":
            response = Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                    "Access-Control-Max-Age": "600",
                },
            )
            await response(scope, receive, send)
            return

        async def send_with_cors(message):
            if message["type"] == "http.response.start" and scope.get("method") in {"GET", "HEAD"}:
                headers = list(message.get("headers") or [])
                headers.append((b"access-control-allow-origin", b"*"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_cors)


def create_app(quota: MessageQuota | None = None):
    handler = DefaultRequestHandler(
        agent_executor=ChatExecutor(build_graph()),
        task_store=InMemoryTaskStore(),
    )
    server = Application(agent_card=build_agent_card(), http_handler=handler)
    app = QuotaMiddleware(server.build(), quota or MessageQuota.from_env())
    return CardCorsMiddleware(app)


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
