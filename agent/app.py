"""A2A bookstore: structured DataPart actions, or English text through LangGraph."""

import base64
import json
import logging
import os
import time
from collections.abc import Sequence
from contextlib import asynccontextmanager

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentProvider,
    AgentSkill,
    DataPart,
    FilePart,
    FileWithBytes,
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

from actions import ACTIONS, skill_description
from analytics import PAGE_ORIGIN, Recorder, authorize, build_report, deliver_report, parse_day, public_stats
from bookstore import Bookstore
from quota import LIMITED_METHODS, ActionQuota, MessageQuota, message_for, rpc_error
from tools import build_mcp, langchain_tools, make_functions

DEFAULT_PUBLIC_URL = "https://agent.agiq.top"
DEFAULT_MODEL = "gpt-4o-mini"
DOCS_URL = "https://agiq.top"
ICON_URL = "https://agiq.top/icon.svg"
CARD_PATHS = frozenset({
    "/.well-known/agent-card.json",
    "/.well-known/agent.json",
})
SYSTEM_PROMPT = (
    "You are AGIQ Books, an agent-to-agent bookstore. Use the bookstore tools for the catalog, "
    "customers, carts, checkout, sandbox payment, invoices, and order email. Never invent books, "
    "prices, or stock. Payments are a sandbox: pass a card number only to the pay tool and never "
    "repeat the full number in your reply. Reply in English."
)
WORKING_ACTIONS = frozenset({"checkout", "pay", "generate_invoice", "notify_order"})
log_analytics = logging.getLogger("agiq.analytics")


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
        reply = AIMessage(content=f"echo: {text}")
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


def build_graph(store: Bookstore):
    functions = make_functions(store)
    return create_react_agent(build_model(), tools=langchain_tools(functions), prompt=SYSTEM_PROMPT), functions


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


def structured_payload(message) -> dict | None:
    if message is None:
        return None
    for part in message.parts:
        root = part.root
        if isinstance(root, DataPart) and isinstance(root.data, dict):
            return root.data
    return None


class BookstoreExecutor(AgentExecutor):
    def __init__(self, graph, store: Bookstore) -> None:
        self._graph = graph
        self._store = store

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.message is None:
            raise ServerError(InvalidParamsError(message="The message has no content."))
        payload = structured_payload(context.message)
        task = context.current_task
        if task is None:
            task = new_task(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        if payload is not None:
            await self._structured(payload, task, updater)
            return
        user_text = context.get_user_input().strip()
        if not user_text:
            raise ServerError(InvalidParamsError(message="The message has no text."))
        try:
            result = await self._graph.ainvoke({"messages": [("user", user_text)]})
            reply = text_from_result(result) or "(no reply)"
        except Exception as exc:
            detail = str(exc).splitlines()[0][:500]
            await updater.failed(new_agent_text_message(f"I could not answer: {detail}", task.context_id, task.id))
            return
        await updater.add_artifact([Part(root=TextPart(text=reply))], name="reply")
        await updater.complete(new_agent_text_message(reply, task.context_id, task.id))

    async def _structured(self, payload: dict, task, updater: TaskUpdater) -> None:
        action = payload.get("action")
        if isinstance(action, str) and action in WORKING_ACTIONS:
            await updater.start_work(
                new_agent_text_message(f"Working on {action}.", task.context_id, task.id)
            )
        outcome = self._store.handle(payload, context_id=task.context_id)
        body = {"state": outcome.state, "message": outcome.message, **outcome.data}
        parts = [Part(root=DataPart(data=body))]
        for name, content, mime in outcome.files:
            parts.append(
                Part(
                    root=FilePart(
                        file=FileWithBytes(
                            bytes=base64.b64encode(content).decode("ascii"),
                            mime_type=mime,
                            name=name,
                        )
                    )
                )
            )
        message = new_agent_text_message(outcome.message, task.context_id, task.id)
        if outcome.state == "completed":
            await updater.add_artifact(parts, name="result")
            await updater.complete(message)
            return
        if outcome.state == "input-required":
            await updater.requires_input(message, final=True)
            return
        await updater.add_artifact(parts, name="result")
        await updater.failed(message)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise ServerError(UnsupportedOperationError(message="Cancellation is not available."))


def build_agent_card() -> AgentCard:
    base = public_url()
    skills = []
    for spec in ACTIONS:
        output_modes = ["application/json", "text/plain"]
        if spec["files"]:
            output_modes.extend(["application/xml", "application/pdf"])
        skills.append(
            AgentSkill(
                id=spec["id"],
                name=spec["name"],
                description=skill_description(spec),
                tags=["bookstore", spec["id"]],
                examples=[spec["example"], spec["utterance"]],
                input_modes=["application/json", "text/plain"],
                output_modes=output_modes,
            )
        )
    return AgentCard(
        name="AGIQ Books",
        description=(
            "Agent-to-agent bookstore for the top 10 Circana BookScan print bestsellers "
            "of the first half of 2026 (Publishers Weekly, period ended July 4, 2026). "
            "Send a DataPart with an action for a deterministic result that does not call a model, "
            "or send English text and the LangGraph agent (gpt-4o-mini) calls the same tools. "
            "Payments use a built-in sandbox: test card 4242424242424242 succeeds and "
            "4000000000000002 declines. The full card number is never stored. "
            "Set STRIPE_SECRET_KEY to use Stripe test mode instead. "
            f"The same tools are MCP streamable HTTP at {base}/mcp."
        ),
        url=base,
        version="0.2.0",
        documentation_url=DOCS_URL,
        icon_url=ICON_URL,
        provider=AgentProvider(organization="MABC80", url=DOCS_URL),
        protocol_version="0.3.0",
        preferred_transport="JSONRPC",
        capabilities=AgentCapabilities(
            streaming=False,
            push_notifications=False,
            extensions=[
                AgentExtension(
                    uri=base + "/mcp",
                    description="Bookstore tools over MCP streamable HTTP. Same service and action rate limit as DataPart calls.",
                    params={"transport": "streamable-http"},
                    required=False,
                )
            ],
        ),
        default_input_modes=["application/json", "text/plain"],
        default_output_modes=["application/json", "text/plain", "application/xml", "application/pdf"],
        skills=skills,
    )


def card_dict() -> dict:
    """Same object the JSON-RPC server returns for the agent card."""
    return build_agent_card().model_dump(exclude_none=True, by_alias=True)


def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


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


def classify_body(body: bytes, path: str) -> tuple[str, str | int | None]:
    """Return ('llm'|'action'|'other'|'plain', json-rpc id)."""
    if path == "/mcp":
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return "plain-action", None
        request_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(request_id, str | int):
            request_id = None
        return "action", request_id
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "plain", None
    if not isinstance(payload, dict) or "method" not in payload:
        return "plain", None
    request_id = payload.get("id")
    if not isinstance(request_id, str | int):
        request_id = None
    if payload.get("method") not in LIMITED_METHODS:
        return "other", request_id
    message = (payload.get("params") or {}).get("message") or {}
    parts = message.get("parts") or []
    for part in parts:
        if isinstance(part, dict) and part.get("kind") == "data":
            return "action", request_id
    return "llm", request_id


class QuotaMiddleware:
    """LLM caps for text. A higher cap for DataPart actions and MCP. Card and health stay open."""

    def __init__(self, app, llm_quota: MessageQuota, action_quota: MessageQuota) -> None:
        self.app = app
        self.llm_quota = llm_quota
        self.action_quota = action_quota

    async def __call__(self, scope, receive, send):
        path = scope.get("path")
        if scope["type"] != "http" or scope.get("method") != "POST" or path not in {"/", "/mcp"}:
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

        kind, request_id = classify_body(body, path)
        ip = client_ip(scope)
        scope.setdefault("state", {})
        if kind == "llm":
            reason = self.llm_quota.consume(ip)
            if reason:
                scope["state"]["quota_limited"] = True
                response = JSONResponse(rpc_error(request_id, reason, "message"))
                await response(scope, replay, send)
                return
        elif kind == "action":
            reason = self.action_quota.consume(ip)
            if reason:
                scope["state"]["quota_limited"] = True
                response = JSONResponse(rpc_error(request_id, reason, "action"))
                await response(scope, replay, send)
                return
        elif kind == "plain":
            reason = self.llm_quota.reason(ip)
            if reason:
                response = JSONResponse({"error": message_for(reason, "message")}, status_code=429)
                await response(scope, replay, send)
                return
        elif kind == "plain-action":
            reason = self.action_quota.reason(ip)
            if reason:
                response = JSONResponse({"error": message_for(reason, "action")}, status_code=429)
                await response(scope, replay, send)
                return
        await self.app(scope, replay, send)


class AnalyticsMiddleware:
    """Record the request after the response. Skip when the handler asks."""

    def __init__(self, app, recorder: Recorder) -> None:
        self.app = app
        self.recorder = recorder

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = {"code": 500}
        body = b""
        method = scope.get("method") or ""
        if method in {"POST", "PUT", "PATCH"}:
            chunks = []
            while True:
                message = await receive()
                chunks.append(message)
                if message["type"] != "http.request":
                    break
                body += message.get("body") or b""
                if not message.get("more_body", False):
                    break

            async def receive_call():
                if chunks:
                    return chunks.pop(0)
                return {"type": "http.disconnect"}
        else:
            receive_call = receive

        async def send_call(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive_call, send_call)
        finally:
            elapsed = int((time.perf_counter() - started) * 1000)
            try:
                self.recorder.record_request(scope, body, status["code"], elapsed)
            except Exception:
                log_analytics.exception("analytics record failed")


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


class Application(A2AStarletteApplication):
    def routes(self, *args, **kwargs):
        return [
            *super().routes(*args, **kwargs),
            Route("/health", health, methods=["GET"]),
        ]


def create_app(
    quota: MessageQuota | None = None,
    action_quota: MessageQuota | None = None,
    database_url: str | None = None,
    graph=None,
    store: Bookstore | None = None,
):
    bookstore = store or Bookstore(database_url=database_url, public_base=public_url())
    functions = make_functions(bookstore)
    if graph is None:
        graph, _functions = build_graph(bookstore)
    handler = DefaultRequestHandler(
        agent_executor=BookstoreExecutor(graph, bookstore),
        task_store=InMemoryTaskStore(),
    )
    mcp = build_mcp(functions)
    mcp_http = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        host="0.0.0.0",
    )

    @asynccontextmanager
    async def lifespan(_app):
        async with mcp_http.router.lifespan_context(mcp_http):
            yield

    server = Application(agent_card=build_agent_card(), http_handler=handler)
    application = server.build(lifespan=lifespan)
    recorder = Recorder(bookstore)
    application.routes.extend(mcp_http.routes)
    application.routes.append(Route("/invoices/{invoice_id}.xml", _invoice("xml", bookstore), methods=["GET"]))
    application.routes.append(Route("/invoices/{invoice_id}.pdf", _invoice("pdf", bookstore), methods=["GET"]))
    application.routes.append(Route("/analytics/pageview", _pageview(recorder), methods=["POST", "OPTIONS"]))
    application.routes.append(Route("/admin/daily-report", _daily_report(bookstore), methods=["POST"]))
    application.routes.append(Route("/admin/stats", _stats(bookstore), methods=["GET"]))
    wrapped = QuotaMiddleware(
        application,
        quota or MessageQuota.from_env(),
        action_quota or ActionQuota.from_env(),
    )
    return CardCorsMiddleware(AnalyticsMiddleware(wrapped, recorder))


def _cors_headers(origin: str | None) -> dict[str, str]:
    if origin == PAGE_ORIGIN:
        return {
            "Access-Control-Allow-Origin": PAGE_ORIGIN,
            "Vary": "Origin",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "content-type",
            "Access-Control-Max-Age": "600",
        }
    return {}


def _skip(request: Request) -> None:
    request.scope.setdefault("state", {})["analytics_skip"] = True


def _pageview(recorder: Recorder):
    async def pageview(request: Request) -> Response:
        origin = request.headers.get("origin")
        headers = _cors_headers(origin)
        if request.method == "OPTIONS":
            _skip(request)
            if origin != PAGE_ORIGIN:
                return Response(status_code=403)
            return Response(status_code=204, headers=headers)
        if origin != PAGE_ORIGIN:
            _skip(request)
            return Response("Forbidden", status_code=403, media_type="text/plain")
        if not recorder.limiter.allow(recorder.hash_ip(client_ip(request.scope))):
            _skip(request)
            return Response(status_code=429, headers=headers)
        return Response(status_code=204, headers=headers)

    return pageview


def _admin_guard(request: Request) -> Response | None:
    status = authorize(request.headers.get("authorization"))
    if status is None:
        return None
    if status == 404:
        return Response(status_code=404)
    return Response("Unauthorized", status_code=401, media_type="text/plain")


def _report_day(request: Request):
    try:
        return parse_day(request.query_params.get("date"))
    except ValueError:
        return None


def _daily_report(store: Bookstore):
    async def daily_report(request: Request) -> Response:
        refused = _admin_guard(request)
        if refused is not None:
            return refused
        day = _report_day(request)
        if day is None:
            return JSONResponse({"error": "date must be YYYY-MM-DD"}, status_code=400)
        dry_run = request.query_params.get("dry_run", "").lower() in {"1", "true", "yes"}
        return JSONResponse(deliver_report(store, day, dry_run=dry_run))

    return daily_report


def _stats(store: Bookstore):
    async def stats(request: Request) -> Response:
        refused = _admin_guard(request)
        if refused is not None:
            return refused
        day = _report_day(request)
        if day is None:
            return JSONResponse({"error": "date must be YYYY-MM-DD"}, status_code=400)
        return JSONResponse(public_stats(build_report(store, day)))

    return stats


def _invoice(kind: str, store: Bookstore):
    async def download(request: Request) -> Response:
        found = store.invoice_download(request.path_params["invoice_id"], kind, request.query_params.get("token", ""))
        if found is None:
            return Response("Not found", status_code=404, media_type="text/plain")
        content, mime, filename = found
        return Response(content, media_type=mime, headers={"Content-Disposition": f'inline; filename="{filename}"'})

    return download


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
