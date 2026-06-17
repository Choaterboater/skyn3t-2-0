"""Connector / integration catalog (P2).

A curated, offline catalog of integrations the builder can wire into a generated
app from a plain-language brief. Each :class:`Connector` knows its category,
the Python packages it needs, the environment variables it expects, and a short
scaffold snippet. ``match_brief`` does keyword matching so "build a Stripe-backed
store with Postgres and email" resolves to the stripe, postgres, and email
connectors with the packages and env requirements collected.

Entirely static data — no network, no I/O at import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Connector:
    key: str
    name: str
    category: str          # database | payments | auth | email | storage | llm | messaging | observability
    description: str
    packages: tuple[str, ...] = ()
    env_vars: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    scaffold: str = ""
    docs_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "packages": list(self.packages),
            "env_vars": list(self.env_vars),
            "keywords": list(self.keywords),
            "docs_url": self.docs_url,
        }


# ---- curated catalog ----------------------------------------------------
_CONNECTORS: tuple[Connector, ...] = (
    Connector(
        key="postgres", name="PostgreSQL", category="database",
        description="Relational database via asyncpg/SQLAlchemy.",
        packages=("sqlalchemy[asyncio]", "asyncpg"),
        env_vars=("DATABASE_URL",),
        keywords=("postgres", "postgresql", "relational", "sql", "database", "db"),
        scaffold="from sqlalchemy.ext.asyncio import create_async_engine\nengine = create_async_engine(os.environ['DATABASE_URL'])",
        docs_url="https://www.postgresql.org/docs/",
    ),
    Connector(
        key="sqlite", name="SQLite", category="database",
        description="Zero-config embedded database (default for small apps).",
        packages=("sqlalchemy[asyncio]", "aiosqlite"),
        env_vars=(),
        keywords=("sqlite", "embedded", "local database", "lightweight db"),
        scaffold="from sqlalchemy.ext.asyncio import create_async_engine\nengine = create_async_engine('sqlite+aiosqlite:///app.db')",
        docs_url="https://www.sqlite.org/docs.html",
    ),
    Connector(
        key="redis", name="Redis", category="database",
        description="In-memory cache / queue.",
        packages=("redis",),
        env_vars=("REDIS_URL",),
        keywords=("redis", "cache", "caching", "queue", "session store"),
        scaffold="import redis.asyncio as redis\nr = redis.from_url(os.environ['REDIS_URL'])",
        docs_url="https://redis.io/docs/",
    ),
    Connector(
        key="stripe", name="Stripe", category="payments",
        description="Payments, subscriptions, checkout.",
        packages=("stripe",),
        env_vars=("STRIPE_API_KEY", "STRIPE_WEBHOOK_SECRET"),
        keywords=("stripe", "payment", "payments", "checkout", "subscription", "billing", "store", "ecommerce"),
        scaffold="import stripe\nstripe.api_key = os.environ['STRIPE_API_KEY']",
        docs_url="https://stripe.com/docs/api",
    ),
    Connector(
        key="oauth", name="OAuth / OIDC", category="auth",
        description="Third-party login (Google/GitHub) via authlib.",
        packages=("authlib", "httpx"),
        env_vars=("OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET"),
        keywords=("oauth", "login", "sign in", "auth", "authentication", "sso", "google login", "github login"),
        scaffold="from authlib.integrations.starlette_client import OAuth\noauth = OAuth()",
        docs_url="https://docs.authlib.org/",
    ),
    Connector(
        key="jwt", name="JWT Auth", category="auth",
        description="Stateless token auth with PyJWT.",
        packages=("pyjwt",),
        env_vars=("JWT_SECRET",),
        keywords=("jwt", "token auth", "bearer", "api auth", "authentication"),
        scaffold="import jwt\ntoken = jwt.encode({'sub': uid}, os.environ['JWT_SECRET'], algorithm='HS256')",
        docs_url="https://pyjwt.readthedocs.io/",
    ),
    Connector(
        key="email", name="Email (SMTP)", category="email",
        description="Transactional email over SMTP.",
        packages=("aiosmtplib",),
        env_vars=("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"),
        keywords=("email", "smtp", "mail", "notification email", "send email"),
        scaffold="import aiosmtplib\nawait aiosmtplib.send(msg, hostname=os.environ['SMTP_HOST'])",
        docs_url="https://aiosmtplib.readthedocs.io/",
    ),
    Connector(
        key="s3", name="Object Storage (S3)", category="storage",
        description="S3-compatible blob storage.",
        packages=("boto3",),
        env_vars=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "S3_BUCKET"),
        keywords=("s3", "storage", "file upload", "blob", "object storage", "uploads", "bucket"),
        scaffold="import boto3\ns3 = boto3.client('s3')",
        docs_url="https://boto3.amazonaws.com/v1/documentation/api/latest/index.html",
    ),
    Connector(
        key="openai", name="OpenAI", category="llm",
        description="LLM completions / embeddings.",
        packages=("openai",),
        env_vars=("OPENAI_API_KEY",),
        keywords=("openai", "gpt", "llm", "ai", "chatbot", "embeddings", "completion"),
        scaffold="from openai import AsyncOpenAI\nclient = AsyncOpenAI()",
        docs_url="https://platform.openai.com/docs",
    ),
    Connector(
        key="anthropic", name="Anthropic Claude", category="llm",
        description="Claude LLM completions and tool use.",
        packages=("anthropic",),
        env_vars=("ANTHROPIC_API_KEY",),
        keywords=("anthropic", "claude", "llm", "ai assistant", "chatbot"),
        scaffold="from anthropic import AsyncAnthropic\nclient = AsyncAnthropic()",
        docs_url="https://docs.anthropic.com/",
    ),
    Connector(
        key="slack", name="Slack", category="messaging",
        description="Send messages / build Slack bots.",
        packages=("slack_sdk",),
        env_vars=("SLACK_BOT_TOKEN",),
        keywords=("slack", "slack bot", "chat notification", "messaging"),
        scaffold="from slack_sdk.web.async_client import AsyncWebClient\nclient = AsyncWebClient(token=os.environ['SLACK_BOT_TOKEN'])",
        docs_url="https://api.slack.com/",
    ),
    Connector(
        key="telegram", name="Telegram", category="messaging",
        description="Telegram bot integration.",
        packages=("python-telegram-bot",),
        env_vars=("TELEGRAM_BOT_TOKEN",),
        keywords=("telegram", "telegram bot", "messaging"),
        scaffold="from telegram import Bot\nbot = Bot(os.environ['TELEGRAM_BOT_TOKEN'])",
        docs_url="https://python-telegram-bot.org/",
    ),
    Connector(
        key="discord", name="Discord", category="messaging",
        description="Discord bot integration.",
        packages=("discord.py",),
        env_vars=("DISCORD_BOT_TOKEN",),
        keywords=("discord", "discord bot", "messaging", "community bot"),
        scaffold="import discord\nclient = discord.Client(intents=discord.Intents.default())",
        docs_url="https://discordpy.readthedocs.io/",
    ),
    Connector(
        key="prometheus", name="Prometheus Metrics", category="observability",
        description="Expose app metrics for scraping.",
        packages=("prometheus_client",),
        env_vars=(),
        keywords=("prometheus", "metrics", "monitoring", "observability"),
        scaffold="from prometheus_client import Counter, make_asgi_app\nmetrics_app = make_asgi_app()",
        docs_url="https://prometheus.github.io/client_python/",
    ),
    Connector(
        key="fastapi", name="FastAPI", category="web",
        description="Async web framework for APIs.",
        packages=("fastapi", "uvicorn"),
        env_vars=(),
        keywords=("fastapi", "api", "rest", "web", "backend", "endpoint", "http server"),
        scaffold="from fastapi import FastAPI\napp = FastAPI()",
        docs_url="https://fastapi.tiangolo.com/",
    ),
)

_BY_KEY: dict[str, Connector] = {c.key: c for c in _CONNECTORS}


@dataclass
class ConnectorCatalog:
    """Lookup + brief-matching over the curated connector set."""

    connectors: tuple[Connector, ...] = field(default=_CONNECTORS)

    def all(self) -> list[Connector]:
        return list(self.connectors)

    def get(self, key: str) -> Connector | None:
        return _BY_KEY.get(key.lower())

    def by_category(self, category: str) -> list[Connector]:
        return [c for c in self.connectors if c.category == category.lower()]

    def categories(self) -> list[str]:
        return sorted({c.category for c in self.connectors})

    def match_brief(self, brief: str, *, limit: int | None = None) -> list[Connector]:
        """Return connectors whose keywords appear in ``brief`` (scored)."""
        text = (brief or "").lower()
        scored: list[tuple[int, Connector]] = []
        for c in self.connectors:
            score = sum(1 for kw in c.keywords if kw in text)
            if score:
                scored.append((score, c))
        scored.sort(key=lambda t: (-t[0], t[1].key))
        result = [c for _, c in scored]
        return result[:limit] if limit else result

    def wiring_plan(self, brief: str) -> dict[str, Any]:
        """Resolve a brief to a concrete wiring plan the builder can act on.

        Collects the union of required packages and env vars across the matched
        connectors plus per-connector scaffold snippets.
        """
        matched = self.match_brief(brief)
        packages: list[str] = []
        env_vars: list[str] = []
        for c in matched:
            for p in c.packages:
                if p not in packages:
                    packages.append(p)
            for e in c.env_vars:
                if e not in env_vars:
                    env_vars.append(e)
        return {
            "connectors": [c.key for c in matched],
            "packages": packages,
            "env_vars": env_vars,
            "snippets": {c.key: c.scaffold for c in matched if c.scaffold},
            "details": [c.to_dict() for c in matched],
        }


# Module-level convenience instance (immutable data, safe to share).
CATALOG = ConnectorCatalog()
