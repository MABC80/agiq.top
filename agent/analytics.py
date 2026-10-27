"""Privacy-conscious request log and the daily owner report.

Raw IP addresses are not stored. Each UTC day has its own salt, and the
stored identifier is SHA-256 of that salt plus the address.
"""

import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import threading
import time
from collections import Counter
from datetime import date, datetime, time as clock, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select

from db import DailySalt, Visit, env_text, utcnow
from emailer import NOT_CONFIGURED
from invoicing import money
from quota import DEFAULT_DAILY_LIMIT, parse_limit, DAILY_LIMIT_ENV

log = logging.getLogger("agiq.analytics")

BERLIN = ZoneInfo("Europe/Berlin")
RETENTION_DAYS = 90
PAGE_ORIGIN = "https://agiq.top"
REPORT_MISSING = "not_sent: REPORT_EMAIL_TO is not configured"

# Curated from the public crawler-user-agents list, plus directory crawlers,
# uptime monitors, and generic HTTP clients. Matching is case-insensitive.
CRAWLER_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), label)
    for pattern, label in (
        (r"googlebot", "Googlebot"),
        (r"bingbot", "Bingbot"),
        (r"duckduckbot", "DuckDuckBot"),
        (r"baiduspider", "Baiduspider"),
        (r"yandexbot", "YandexBot"),
        (r"slurp", "Slurp"),
        (r"facebookexternalhit", "facebookexternalhit"),
        (r"twitterbot", "Twitterbot"),
        (r"linkedinbot", "LinkedInBot"),
        (r"slackbot", "Slackbot"),
        (r"discordbot", "Discordbot"),
        (r"telegrambot", "TelegramBot"),
        (r"whatsapp", "WhatsApp"),
        (r"applebot", "Applebot"),
        (r"ahrefsbot", "AhrefsBot"),
        (r"semrushbot", "SemrushBot"),
        (r"mj12bot", "MJ12bot"),
        (r"dotbot", "DotBot"),
        (r"bytespider", "Bytespider"),
        (r"petalbot", "PetalBot"),
        (r"gptbot", "GPTBot"),
        (r"chatgpt-user", "ChatGPT-User"),
        (r"oai-searchbot", "OAI-SearchBot"),
        (r"claudebot", "ClaudeBot"),
        (r"claude-web", "Claude-Web"),
        (r"amazonbot", "Amazonbot"),
        (r"ccbot", "CCBot"),
        (r"perplexitybot", "PerplexityBot"),
        (r"headlesschrome", "HeadlessChrome"),
        (r"uptimerobot", "UptimeRobot"),
        (r"pingdom", "Pingdom"),
        (r"statuscake", "StatusCake"),
        (r"better uptime|betteruptime", "Better Uptime"),
        (r"site24x7", "Site24x7"),
        (r"uptime-kuma", "Uptime-Kuma"),
        (r"kube-probe", "kube-probe"),
        (r"a2aregistry|a2a-registry", "a2aregistry"),
        (r"agentpeering", "AgentPeering"),
        (r"nanda", "NANDA"),
    )
)

GENERIC_CLIENTS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), label)
    for pattern, label in (
        (r"python-requests", "python-requests"),
        (r"\bhttpx\b", "httpx"),
        (r"\bcurl/", "curl"),
        (r"\bwget/", "wget"),
        (r"go-http-client", "Go-http-client"),
        (r"apache-httpclient", "Apache-HttpClient"),
        (r"\bokhttp\b", "okhttp"),
        (r"libwww-perl", "libwww-perl"),
    )
)

COUNTRY_HEADERS = (
    "cf-ipcountry",
    "x-vercel-ip-country",
    "cloudfront-viewer-country",
    "x-country-code",
    "x-appengine-country",
)
SKIP_COUNTRIES = {"", "XX", "T1", "ZZ"}
PRODUCT_TOKEN = re.compile(r"([A-Za-z][A-Za-z0-9._+-]{1,60})/\d")


def clip(value: str, limit: int) -> str:
    text = value.replace("\x00", "").strip()
    return text[:limit]


def is_browser(user_agent: str) -> bool:
    lowered = user_agent.lower()
    if "mozilla/5.0" not in lowered:
        return False
    return any(token in lowered for token in ("chrome/", "safari/", "firefox/", "edg/", "opr/"))


def known_crawler(user_agent: str, patterns=CRAWLER_PATTERNS) -> str | None:
    for pattern, label in patterns:
        if pattern.search(user_agent):
            return label
    return None


def product_token(user_agent: str) -> str | None:
    stripped = user_agent.strip()
    if not stripped or stripped.lower().startswith("mozilla/"):
        return None
    match = PRODUCT_TOKEN.match(stripped)
    if match:
        return match.group(1)
    return None


def classify(user_agent: str, *, beacon: bool, identity: str | None) -> tuple[str, str]:
    """Return (classification, reason)."""
    crawler = known_crawler(user_agent)
    if crawler:
        return "known_bot", f"known crawler: {crawler}"
    if beacon and is_browser(user_agent):
        return "human", "browser user-agent and JavaScript beacon"
    if identity:
        return "ai_agent", "declared agent identity"
    generic = known_crawler(user_agent, GENERIC_CLIENTS)
    if generic:
        return "known_bot", f"known crawler: {generic}"
    token = product_token(user_agent)
    if token:
        return "ai_agent", "user-agent product token"
    if is_browser(user_agent):
        return "human", "browser user-agent"
    return "unknown", "no browser, crawler, or agent identity matched"


def country_from(headers: dict[str, str]) -> str | None:
    for name in COUNTRY_HEADERS:
        raw = headers.get(name, "").strip().upper()
        if len(raw) == 2 and raw.isalpha() and raw not in SKIP_COUNTRIES:
            return raw
    return None


def kind_for(path: str) -> str:
    if path in {"/.well-known/agent-card.json", "/.well-known/agent.json"}:
        return "card_fetch"
    if path == "/health":
        return "monitor"
    if path == "/mcp" or path.startswith("/mcp/"):
        return "mcp"
    if path.startswith("/invoices/"):
        return "invoice"
    if path == "/analytics/pageview":
        return "page_view"
    if path.startswith("/admin/"):
        return "admin"
    if path == "/":
        return "a2a_message"
    return "other"


def _meta_name(meta: dict) -> str | None:
    agent = meta.get("agent")
    if isinstance(agent, str) and agent.strip():
        return agent.strip()
    if isinstance(agent, dict):
        for key in ("name", "agentName"):
            value = agent.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for key in ("name", "agentName", "agent_name"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _meta_card(meta: dict) -> str | None:
    agent = meta.get("agent")
    if isinstance(agent, dict):
        for key in ("url", "agentCard", "agent_card"):
            value = agent.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for key in ("agentCard", "agent_card", "agentCardUrl", "agent_card_url"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def parse_request(path: str, body: bytes) -> dict:
    """Pull method, style, action, and identity. Never keep the raw body."""
    info = {
        "rpc_method": None,
        "style": None,
        "action_name": None,
        "text_length": None,
        "agent_name": None,
        "agent_card_url": None,
        "referer": None,
        "screen_w": None,
        "screen_h": None,
    }
    if not body:
        return info
    try:
        payload = json.loads(body[:65536].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return info
    if not isinstance(payload, dict):
        return info
    if path == "/analytics/pageview":
        referrer = payload.get("referrer") or payload.get("referer") or ""
        if isinstance(referrer, str):
            info["referer"] = clip(referrer, 500)
        screen = payload.get("screen") if isinstance(payload.get("screen"), dict) else {}
        info["screen_w"] = _small_int(screen.get("width") if screen else payload.get("screen_w"))
        info["screen_h"] = _small_int(screen.get("height") if screen else payload.get("screen_h"))
        page = payload.get("path")
        if isinstance(page, str) and page.strip():
            info["action_name"] = clip(page, 64)
        return info
    if path == "/mcp" or path.startswith("/mcp/"):
        method = payload.get("method")
        if isinstance(method, str):
            info["rpc_method"] = clip(method, 64)
        info["style"] = "mcp"
        if method == "tools/call":
            name = (payload.get("params") or {}).get("name") if isinstance(payload.get("params"), dict) else None
            if isinstance(name, str):
                info["action_name"] = clip(name, 64)
        return info
    method = payload.get("method")
    if isinstance(method, str):
        info["rpc_method"] = clip(method, 64)
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    message = params.get("message") if isinstance(params.get("message"), dict) else {}
    meta = {}
    if isinstance(message.get("metadata"), dict):
        meta.update(message["metadata"])
    if isinstance(params.get("metadata"), dict):
        meta.update(params["metadata"])
    info["agent_name"] = _meta_name(meta)
    info["agent_card_url"] = _meta_card(meta)
    action = None
    text_length = 0
    saw_text = False
    for part in message.get("parts") or []:
        if not isinstance(part, dict):
            continue
        if part.get("kind") == "data" and isinstance(part.get("data"), dict):
            candidate = part["data"].get("action")
            if isinstance(candidate, str) and candidate.strip():
                action = clip(candidate, 64)
        elif part.get("kind") == "text" and isinstance(part.get("text"), str):
            saw_text = True
            text_length += len(part["text"])
    if action:
        info["style"] = "structured"
        info["action_name"] = action
    elif saw_text:
        info["style"] = "natural_language"
        info["text_length"] = text_length
    return info


def _small_int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if 0 <= value <= 20000:
        return value
    return None


def header_map(scope) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers") or []
    }


def client_address(scope) -> str:
    headers = header_map(scope)
    forwarded = headers.get("x-forwarded-for", "")
    if forwarded.strip():
        return forwarded.split(",")[0].strip() or "unknown"
    client = scope.get("client")
    if client and client[0]:
        return client[0]
    return "unknown"


class PageviewLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        raw = env_text("PAGEVIEW_PER_HOUR") or "60"
        try:
            limit = int(raw)
        except ValueError:
            limit = 60
        if limit < 1:
            limit = 60
        now = time.time()
        with self._lock:
            recent = [stamp for stamp in self._hits.get(key, []) if now - stamp < 3600]
            if len(recent) >= limit:
                self._hits[key] = recent
                return False
            recent.append(now)
            self._hits[key] = recent
            return True


class Recorder:
    def __init__(self, store) -> None:
        self.store = store
        self.limiter = PageviewLimiter()
        self._salt_lock = threading.Lock()

    def hash_ip(self, address: str, moment: datetime | None = None) -> str:
        moment = moment or utcnow()
        day = moment.astimezone(timezone.utc).date().isoformat()
        with self.store.Session() as session:
            salt = self._salt(session, day)
            session.commit()
        return hashlib.sha256(f"{salt}:{address}".encode()).hexdigest()

    def _salt(self, session, day: str) -> str:
        with self._salt_lock:
            row = session.get(DailySalt, day)
            if row is None:
                row = DailySalt(day=day, salt=secrets.token_hex(16))
                session.add(row)
                session.flush()
            return row.salt

    def prune(self, session) -> None:
        cutoff = utcnow() - timedelta(days=RETENTION_DAYS)
        session.execute(delete(Visit).where(Visit.created_at < cutoff))

    def record_request(self, scope, body: bytes, status_code: int, latency_ms: int) -> None:
        state = scope.get("state") or {}
        if state.get("analytics_skip"):
            return
        path = scope.get("path") or "/"
        if scope.get("method") == "OPTIONS":
            return
        headers = header_map(scope)
        parsed = parse_request(path, body)
        identity = headers.get("x-agent-name") or parsed["agent_name"]
        card = headers.get("x-agent-card") or parsed["agent_card_url"]
        if not identity and headers.get("from"):
            identity = headers.get("from")
        user_agent = clip(headers.get("user-agent", ""), 400)
        beacon = path == "/analytics/pageview"
        classification, reason = classify(user_agent, beacon=beacon, identity=identity)
        if classification == "ai_agent" and not identity:
            identity = product_token(user_agent)
        address = client_address(scope)
        referer = parsed["referer"] if parsed["referer"] is not None else clip(headers.get("referer", ""), 500)
        with self.store.Session() as session:
            self.prune(session)
            visit = Visit(
                kind=kind_for(path),
                method=clip(scope.get("method") or "", 8),
                path=clip(path, 200),
                status_code=int(status_code),
                latency_ms=max(0, int(latency_ms)),
                user_agent=user_agent,
                ip_hash=hashlib.sha256(f"{self._salt(session, utcnow().date().isoformat())}:{address}".encode()).hexdigest(),
                country=country_from(headers),
                referer=referer or "",
                rpc_method=parsed["rpc_method"],
                style=parsed["style"],
                action_name=parsed["action_name"],
                text_length=parsed["text_length"],
                agent_name=clip(identity, 200) if identity else None,
                agent_card_url=clip(card, 300) if card else None,
                classification=classification,
                classifier_reason=clip(reason, 200),
                quota_limited=bool(state.get("quota_limited")),
                screen_w=parsed["screen_w"],
                screen_h=parsed["screen_h"],
            )
            session.add(visit)
            session.commit()


def berlin_window(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, clock.min, tzinfo=BERLIN)
    return start, start + timedelta(days=1)


def default_report_day(now: datetime | None = None) -> date:
    moment = now or datetime.now(BERLIN)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=BERLIN)
    return (moment.astimezone(BERLIN) - timedelta(days=1)).date()


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _in_window(moment: datetime, start: datetime, end: datetime) -> bool:
    stamp = _aware(moment)
    return start <= stamp < end


def build_report(store, day: date) -> dict:
    start, end = berlin_window(day)
    with store.Session() as session:
        visits = [row for row in session.scalars(select(Visit)).all() if _in_window(row.created_at, start, end)]
        from db import Cart, Customer, Invoice, Order, Payment

        customers = [row for row in session.scalars(select(Customer)).all() if _in_window(row.created_at, start, end)]
        carts = [row for row in session.scalars(select(Cart)).all() if _in_window(row.created_at, start, end)]
        orders = [row for row in session.scalars(select(Order)).all() if _in_window(row.created_at, start, end)]
        payments = [row for row in session.scalars(select(Payment)).all() if _in_window(row.created_at, start, end)]
        invoices = [row for row in session.scalars(select(Invoice)).all() if _in_window(row.created_at, start, end)]
        sandbox_cents = 0
        for payment in payments:
            if payment.status != "succeeded" or not payment.sandbox:
                continue
            order = session.get(Order, payment.order_id)
            if order is not None:
                sandbox_cents += order.total_cents
    traffic = [row for row in visits if row.kind not in {"monitor", "admin"}]
    classes = Counter(row.classification for row in traffic)
    kinds = Counter(row.kind for row in traffic)
    styles = Counter(row.style for row in traffic if row.style)
    actions = Counter(row.action_name for row in traffic if row.style == "structured" and row.action_name)
    tools = Counter(row.action_name for row in traffic if row.style == "mcp" and row.action_name)
    countries = Counter(row.country or "unknown" for row in traffic)
    pages = [row for row in traffic if row.kind == "page_view"]
    referrers = Counter((row.referer or "(direct)") for row in pages)
    unique = {row.ip_hash for row in traffic}
    actors = _actors(traffic)
    succeeded = [row for row in payments if row.status == "succeeded"]
    llm_calls = sum(
        1
        for row in traffic
        if row.style == "natural_language" and not row.quota_limited and 200 <= row.status_code < 300
    )
    stats = {
        "date": day.isoformat(),
        "timezone": "Europe/Berlin",
        "from": start.isoformat(),
        "until": end.isoformat(),
        "totals": {"requests": len(traffic), "unique_visitors": len(unique)},
        "classifications": {
            "human": classes.get("human", 0),
            "known_bot": classes.get("known_bot", 0),
            "ai_agent": classes.get("ai_agent", 0),
            "unknown": classes.get("unknown", 0),
        },
        "by_kind": dict(sorted(kinds.items())),
        "styles": {
            "structured": styles.get("structured", 0),
            "natural_language": styles.get("natural_language", 0),
            "mcp": styles.get("mcp", 0),
            "top_actions": [{"name": name, "count": count} for name, count in actions.most_common(10)],
            "top_tools": [{"name": name, "count": count} for name, count in tools.most_common(10)],
        },
        "actors": actors,
        "landing": {
            "views": len(pages),
            "human_views": sum(1 for row in pages if row.classification == "human"),
            "top_referrers": [{"referrer": name, "count": count} for name, count in referrers.most_common(10)],
        },
        "countries": [{"country": name, "count": count} for name, count in countries.most_common()],
        "funnel": {
            "customers_registered": len(customers),
            "carts": len(carts),
            "checkouts": len(orders),
            "payments_succeeded": len(succeeded),
            "payments_declined": sum(1 for row in payments if row.status == "declined"),
            "invoices": len(invoices),
            "sandbox_revenue_usd": money(sandbox_cents),
        },
        "llm": {"calls": llm_calls, "daily_cap": parse_limit(DAILY_LIMIT_ENV, DEFAULT_DAILY_LIMIT)},
        "errors": {
            "4xx": sum(1 for row in traffic if 400 <= row.status_code < 500),
            "5xx": sum(1 for row in traffic if row.status_code >= 500),
        },
        "monitors": sum(1 for row in visits if row.kind == "monitor"),
        "admin_calls": sum(1 for row in visits if row.kind == "admin"),
    }
    text = render_text(stats)
    return {**stats, "text": text, "html": render_html(stats)}


def _actors(rows: list) -> list[dict]:
    grouped: dict[tuple, dict] = {}
    for row in rows:
        if row.classification not in {"known_bot", "ai_agent"} and not row.agent_name:
            continue
        name = row.agent_name or row.user_agent[:80] or "(unnamed)"
        key = (name, row.classification, row.user_agent)
        bucket = grouped.get(key)
        if bucket is None:
            bucket = {
                "name": name,
                "user_agent": row.user_agent,
                "classification": row.classification,
                "first_seen": _aware(row.created_at),
                "last_seen": _aware(row.created_at),
                "requests": 0,
                "styles": set(),
            }
            grouped[key] = bucket
        bucket["requests"] += 1
        stamp = _aware(row.created_at)
        bucket["first_seen"] = min(bucket["first_seen"], stamp)
        bucket["last_seen"] = max(bucket["last_seen"], stamp)
        if row.style:
            bucket["styles"].add(row.style)
    actors = []
    for bucket in grouped.values():
        actors.append(
            {
                "name": bucket["name"],
                "user_agent": bucket["user_agent"],
                "classification": bucket["classification"],
                "first_seen": bucket["first_seen"].isoformat(),
                "last_seen": bucket["last_seen"].isoformat(),
                "requests": bucket["requests"],
                "styles": sorted(bucket["styles"]),
            }
        )
    actors.sort(key=lambda item: (-item["requests"], item["name"]))
    return actors[:40]


def render_text(stats: dict) -> str:
    lines = [
        f"AGIQ Books daily report {stats['date']} ({stats['timezone']})",
        f"Window: {stats['from']} until {stats['until']}",
        "",
        f"Requests: {stats['totals']['requests']}",
        f"Unique visitors (daily IP hash): {stats['totals']['unique_visitors']}",
        (
            "Humans {human}, known bots {known_bot}, AI agents {ai_agent}, unknown {unknown}."
        ).format(**stats["classifications"]),
        "",
        "By endpoint:",
    ]
    for name, count in stats["by_kind"].items():
        lines.append(f"  {name}: {count}")
    styles = stats["styles"]
    lines.extend(
        [
            "",
            f"Structured: {styles['structured']}",
            f"Natural language: {styles['natural_language']}",
            f"MCP: {styles['mcp']}",
            "Top actions: " + _pairs(styles["top_actions"]),
            "Top tools: " + _pairs(styles["top_tools"]),
            "",
            f"Landing views: {stats['landing']['views']} ({stats['landing']['human_views']} human)",
            "Top referrers: " + _pairs(stats["landing"]["top_referrers"], "referrer"),
            "Countries: " + _pairs(stats["countries"], "country"),
            "",
            "Purchase funnel:",
            f"  customers registered: {stats['funnel']['customers_registered']}",
            f"  carts: {stats['funnel']['carts']}",
            f"  checkouts: {stats['funnel']['checkouts']}",
            f"  payments succeeded: {stats['funnel']['payments_succeeded']}",
            f"  payments declined: {stats['funnel']['payments_declined']}",
            f"  invoices: {stats['funnel']['invoices']}",
            f"  sandbox revenue USD: {stats['funnel']['sandbox_revenue_usd']}",
            "",
            f"LLM calls: {stats['llm']['calls']} of daily cap {stats['llm']['daily_cap']}",
            f"Errors: {stats['errors']['4xx']} client, {stats['errors']['5xx']} server",
            f"Health checks excluded: {stats['monitors']}. Admin calls excluded: {stats['admin_calls']}.",
            "",
            "Named agents and bots:",
        ]
    )
    if not stats["actors"]:
        lines.append("  (none)")
    for actor in stats["actors"]:
        styles_used = ", ".join(actor["styles"]) or "none"
        lines.append(
            f"  {actor['name']} [{actor['classification']}] requests={actor['requests']} "
            f"styles={styles_used} first={actor['first_seen']} last={actor['last_seen']} "
            f"ua={actor['user_agent']}"
        )
    lines.append("")
    lines.append("IP addresses are stored only as a daily salted SHA-256 hash.")
    return "\n".join(lines)


def _pairs(rows: list[dict], label: str = "name") -> str:
    if not rows:
        return "(none)"
    return ", ".join(f"{row[label]} ({row['count']})" for row in rows)


def render_html(stats: dict) -> str:
    def cell(value) -> str:
        return html.escape(str(value))

    def table(headers: list[str], rows: list[list]) -> str:
        head = "".join(f"<th>{cell(item)}</th>" for item in headers)
        body = "".join("<tr>" + "".join(f"<td>{cell(item)}</td>" for item in row) + "</tr>" for row in rows)
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

    classes = stats["classifications"]
    funnel = stats["funnel"]
    actor_rows = [
        [
            actor["name"],
            actor["classification"],
            actor["requests"],
            ", ".join(actor["styles"]),
            actor["first_seen"],
            actor["last_seen"],
            actor["user_agent"],
        ]
        for actor in stats["actors"]
    ] or [["(none)", "", "", "", "", "", ""]]
    parts = [
        "<!DOCTYPE html><html><body>",
        f"<h1>AGIQ Books daily report {cell(stats['date'])}</h1>",
        f"<p>Timezone {cell(stats['timezone'])}. Window {cell(stats['from'])} until {cell(stats['until'])}.</p>",
        "<h2>Totals</h2>",
        "<ul>",
        f"<li>Requests: {cell(stats['totals']['requests'])}</li>",
        f"<li>Unique visitors by daily IP hash: {cell(stats['totals']['unique_visitors'])}</li>",
        f"<li>Humans: {cell(classes['human'])}</li>",
        f"<li>Known bots: {cell(classes['known_bot'])}</li>",
        f"<li>AI agents: {cell(classes['ai_agent'])}</li>",
        f"<li>Unknown: {cell(classes['unknown'])}</li>",
        "</ul>",
        "<h2>Endpoints</h2>",
        table(["Kind", "Requests"], [[name, count] for name, count in stats["by_kind"].items()] or [["(none)", 0]]),
        "<h2>Call styles</h2>",
        "<ul>",
        f"<li>Structured: {cell(stats['styles']['structured'])}</li>",
        f"<li>Natural language: {cell(stats['styles']['natural_language'])}</li>",
        f"<li>MCP: {cell(stats['styles']['mcp'])}</li>",
        "</ul>",
        "<h2>Top actions</h2>",
        table(["Action", "Count"], [[row["name"], row["count"]] for row in stats["styles"]["top_actions"]] or [["(none)", 0]]),
        "<h2>Top MCP tools</h2>",
        table(["Tool", "Count"], [[row["name"], row["count"]] for row in stats["styles"]["top_tools"]] or [["(none)", 0]]),
        "<h2>Landing page</h2>",
        f"<p>{cell(stats['landing']['views'])} views, {cell(stats['landing']['human_views'])} from browsers that ran the beacon.</p>",
        table(
            ["Referrer", "Count"],
            [[row["referrer"], row["count"]] for row in stats["landing"]["top_referrers"]] or [["(none)", 0]],
        ),
        "<h2>Countries</h2>",
        table(["Country", "Count"], [[row["country"], row["count"]] for row in stats["countries"]] or [["unknown", 0]]),
        "<h2>Purchase funnel</h2>",
        "<ul>",
        f"<li>Customers registered: {cell(funnel['customers_registered'])}</li>",
        f"<li>Carts: {cell(funnel['carts'])}</li>",
        f"<li>Checkouts: {cell(funnel['checkouts'])}</li>",
        f"<li>Payments succeeded: {cell(funnel['payments_succeeded'])}</li>",
        f"<li>Payments declined: {cell(funnel['payments_declined'])}</li>",
        f"<li>Invoices: {cell(funnel['invoices'])}</li>",
        f"<li>Sandbox revenue USD: {cell(funnel['sandbox_revenue_usd'])}</li>",
        "</ul>",
        "<h2>LLM and errors</h2>",
        f"<p>LLM calls {cell(stats['llm']['calls'])} of daily cap {cell(stats['llm']['daily_cap'])}.</p>",
        f"<p>Client errors {cell(stats['errors']['4xx'])}. Server errors {cell(stats['errors']['5xx'])}.</p>",
        f"<p>Health checks excluded: {cell(stats['monitors'])}. Admin calls excluded: {cell(stats['admin_calls'])}.</p>",
        "<h2>Named agents and bots</h2>",
        table(
            ["Name", "Classification", "Requests", "Styles", "First seen", "Last seen", "User-Agent"],
            actor_rows,
        ),
        "<p>IP addresses are stored only as a daily salted SHA-256 hash. This report does not include message text or card numbers.</p>",
        "</body></html>",
    ]
    return "".join(parts)


def public_stats(stats: dict) -> dict:
    hidden = {"html", "text"}
    return {key: value for key, value in stats.items() if key not in hidden}


def deliver_report(store, day: date, *, dry_run: bool) -> dict:
    stats = build_report(store, day)
    if dry_run:
        stats["email"] = {"status": "dry_run"}
        return stats
    recipient = env_text("REPORT_EMAIL_TO")
    provider = store.email
    if provider is None:
        stats["email"] = {"status": NOT_CONFIGURED}
        log.warning(NOT_CONFIGURED)
        return stats
    if not recipient:
        stats["email"] = {"status": REPORT_MISSING}
        log.warning(REPORT_MISSING)
        return stats
    try:
        provider.send(
            to=recipient,
            subject=f"AGIQ Books daily report {day.isoformat()}",
            text=stats["text"],
            html=stats["html"],
            reply_to=getattr(provider, "reply_to", None) or None,
        )
        stats["email"] = {"status": "sent", "to": recipient}
    except Exception:
        log.warning("daily report email failed", exc_info=True)
        stats["email"] = {"status": "not_sent: email provider returned an error"}
    return stats


def token_matches(given: str, expected: str) -> bool:
    return hmac.compare_digest(
        hashlib.sha256(given.encode()).digest(),
        hashlib.sha256(expected.encode()).digest(),
    )


def authorize(header: str | None) -> int | None:
    """Return an HTTP status when the caller is refused, else None."""
    expected = env_text("ADMIN_TOKEN")
    if not expected:
        return 404
    if not header or not header.startswith("Bearer "):
        return 401
    if not token_matches(header[7:].strip(), expected):
        return 401
    return None


def parse_day(raw: str | None, now: datetime | None = None) -> date:
    if not raw:
        return default_report_day(now)
    return date.fromisoformat(raw)
