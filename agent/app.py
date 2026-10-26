"""Minimal A2A agent: LangGraph + OpenAI, JSON-RPC message/send."""

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
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

DEFAULT_PUBLIC_URL = "https://agent.agiq.top"
DEFAULT_MODEL = "gpt-4o-mini"
SYSTEM_PROMPT = (
    "Eres un asistente breve y útil. Responde en el idioma del usuario."
)
ALLOWED_ORIGINS = ["https://agiq.top", "https://www.agiq.top"]


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


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


class Application(A2AStarletteApplication):
    def routes(self, *args, **kwargs):
        return [
            *super().routes(*args, **kwargs),
            Route("/health", health, methods=["GET"]),
        ]


def create_app():
    handler = DefaultRequestHandler(
        agent_executor=ChatExecutor(build_graph()),
        task_store=InMemoryTaskStore(),
    )
    server = Application(agent_card=build_agent_card(), http_handler=handler)
    return server.build(
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=ALLOWED_ORIGINS,
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["*"],
            )
        ]
    )


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
