# SKILL.md

> Universal Python coding standards for all projects.
> All rules are BINDING. AI agents and human contributors MUST follow them.

---

## 1. Core Philosophy

- **Async everywhere, blocking nowhere.** Every I/O call MUST be awaited. The event loop MUST never be blocked.
- **Explicit types, no magic.** Every function signature MUST carry full type annotations.
- **Composition over inheritance.** Inheritance only for ABC, Pydantic, SQLAlchemy, aiogram base classes.
- **Stateless services, stateful infrastructure.** Business logic holds no mutable state. All persistent state lives in DB/Redis.
- **Small functions.** A function SHOULD be 30 lines or fewer. MUST NOT exceed 50 lines.

---

## 2. Naming Conventions

### Variables & Constants

```python
# Variables: snake_case
user_count = 10
self._repo = repo          # private: single underscore

# Constants: SCREAMING_SNAKE_CASE
_CACHE_TTL = 300
MAX_RETRIES = 3
```

### Functions & Methods

```python
# snake_case, action verbs
async def get_user(tg_id: int) -> User | None: ...
async def create_key(tg_id: int) -> Key: ...
def _sign(data: str) -> str: ...          # private helper

# FORBIDDEN
async def async_get_user(): ...            # async_ prefix
async def fetchUser(): ...                 # camelCase
```

### Classes

```python
# UpperCamelCase with purpose suffix
class UserService: ...                     # Service
class KeyRepository: ...                   # Repository
class AuthMiddleware: ...                  # Middleware
class InsufficientBalanceError: ...        # Error (exceptions)
class UserCreate(BaseModel): ...           # Pydantic model
```

### Files & Modules

- `lowercase_with_underscores.py`
- Named after entity or purpose: `users.py`, `broadcast_handler.py`, `auth.py`

---

## 3. Typing Rules

- ALL function signatures MUST have complete type annotations (parameters AND return type).
- Use `from __future__ import annotations` at the top of every module.
- Use PEP 604: `str | None`, NOT `Optional[str]`.
- Use built-in generics: `list[int]`, `dict[str, Any]`, NOT `List`, `Dict`.

```python
from __future__ import annotations

async def get_user(tg_id: int) -> User | None: ...
items: list[KeyResponse] = []
cache: dict[int, tuple[bool, float]] = {}
```

---

## 4. Function Design

### Arguments

- 4 or fewer positional/keyword arguments (excluding `self`).
- If more needed, group into a dataclass or Pydantic model.

### Return Patterns

- Service methods return domain entities or Pydantic models, never raw `dict`.
- Repository methods return ORM instances or `None`.
- Boolean returns for predicates: `is_banned()`, `can_notify()`.

### Pure vs Impure

- I/O functions (DB, HTTP, Redis) are `async def`.
- Pure transformations (format, validate, compute) are `def`.
- Pure helpers SHOULD be module-level private functions: `_normalize()`, `_sign()`.

---

## 5. Error Handling

### Hierarchy

```python
class AppError(Exception):
    """Base for all domain exceptions."""

class UserNotFoundError(AppError):
    def __init__(self, user_id: int) -> None:
        super().__init__(f"User {user_id} not found")
        self.user_id = user_id
```

- Exceptions MUST store context attributes for programmatic handling.
- Exception names MUST end with `Error`.

### Forbidden Patterns

```python
# FORBIDDEN — silent swallow
except Exception:
    pass

# REQUIRED — at minimum log
except Exception:
    logger.debug("Non-critical error", exc_info=True)

# FORBIDDEN — broad catch with return
except Exception:
    return None

# GOOD — narrow catch
except httpx.HTTPError:
    logger.exception("HTTP call failed")
    return None
```

### Null Checks

```python
# GOOD
if tariff_id is None:
    return False

# BAD — 0 is falsy but valid
if not tariff_id:
    return False
```

---

## 6. Async & Concurrency

### Mandatory Async

- ALL I/O: database, HTTP, Redis, Telegram API, file reads.
- ALL route/handler functions.
- ALL middleware `__call__` methods.

### Forbidden in Async Context

- `time.sleep()` — use `await asyncio.sleep()`.
- `requests` library — use `httpx.AsyncClient` or `aiohttp`.
- Synchronous DB drivers — use `asyncpg`.

### HTTP Client Lifecycle

```python
# GOOD — created once, reused
self._client = httpx.AsyncClient(base_url=url, timeout=15)

# BAD — created per request
async with httpx.AsyncClient() as client:
    resp = await client.post(...)
```

### Concurrency Safety

- Module-level mutable caches OK for single-instance only, document limitation.
- For multi-instance: use Redis with TTL.
- Use `asyncio.Lock` for overlapping coroutine protection.

---

## 7. Import Rules

1. Use absolute imports. Relative imports (`from ..`) are FORBIDDEN.
2. Import order (blank line between groups):
   - Standard library
   - Third-party packages
   - Local modules
3. Dynamic imports inside functions are FORBIDDEN. All imports at module top.

```python
from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import Router, Bot
from pydantic import BaseModel

from app.core.config import settings
from app.db.repositories.users import UserRepository
```

---

## 8. Logging

- Use `structlog.get_logger()` or `logging.getLogger(__name__)`.
- Log structured key-value pairs:

```python
# GOOD
logger.info("user_created", user_id=user.id, username=user.username)

# BAD
logger.info(f"Created user {user.id} with name {user.username}")
```

- NEVER log: passwords, API tokens, full webhook payloads, card numbers.
- Suppress noisy libraries to WARNING: `sqlalchemy.engine`, `aiogram`, `httpx`.

---

## 9. Pydantic & Configuration

- Settings via `pydantic-settings` with `BaseSettings`.
- Response models MUST set `model_config = ConfigDict(from_attributes=True)`.
- Use `env_nested_delimiter="__"` for nested env vars.

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    bot_token: str
    redis_url: str = "redis://localhost:6379/0"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }

settings = Settings()
```

---

## 10. Database Patterns

### Repository Pattern

```python
class UserRepository(BaseRepository):
    async def get_by_id(self, user_id: int) -> asyncpg.Record | None:
        return await self.fetchrow(
            "SELECT * FROM users WHERE id = $1", user_id
        )

    async def create(self, user_id: int, name: str) -> int:
        return await self.fetchval(
            "INSERT INTO users (id, name) VALUES ($1, $2) RETURNING id",
            user_id, name,
        )
```

- Repository methods return DB records or domain objects, never raw dicts.
- All DB access goes through repositories, never direct SQL in handlers.

---

## 11. Caching Pattern

```python
async def get_cached(key: str, ttl: int, fetch_fn) -> Any:
    redis = get_redis()
    cached = await redis.get(key)
    if cached is not None:
        return json.loads(cached)

    data = await fetch_fn()
    await redis.setex(key, ttl, json.dumps(data))
    return data
```

- Always define TTL. No indefinite caches.
- Invalidate on write.
- Cache keys as constants in dedicated module.

---

## 12. aiogram Bot Patterns

### Handler Design

- Handlers are thin wiring: parse input, call service/repo, send response.
- No business logic in handlers. Extract to services.

### Callback Data

- Use structured prefix: `action_param1_param2`
- For complex data, use aiogram `CallbackData` factories:

```python
class KeyAction(CallbackData, prefix="key"):
    action: str
    key_id: str
```

### FSM States

- Group related states in `StatesGroup`.
- Always `await state.clear()` on completion or cancel.

### Webhook Mode

- Bot MUST run in **webhook mode**, NOT polling.
- Use `aiohttp` web server built into aiogram for receiving updates.
- Webhook path: `/webhook/{bot_token}` (token in path prevents unauthorized requests).
- On startup: `set_webhook()`; on shutdown: `delete_webhook()`.

### Middleware Order

- Error handling (outermost) -> Throttling -> DB session -> Feature checks

---

## 13. Reverse Proxy & TLS (Centralized Caddy)

All projects share a **single centralized Caddy** instance on the server at `/opt/caddy/`.

### Architecture

```
/opt/caddy/
├── docker-compose.yml     # Caddy container only, owns webapps_network
├── Caddyfile              # imports from conf.d/<project>/*.conf
├── conf.d/
│   ├── project_a/
│   │   └── project_a.conf
│   ├── realty/
│   │   └── realty.conf
│   └── ...
├── data/
└── logs/
```

### Rules for every new project

1. **Do NOT add Caddy to the project's `docker-compose.yml`.**
2. **Connect services that Caddy needs to reach to `webapps_network` (external).**
3. **Give containers unique `container_name`** — these are used in Caddy conf as upstream targets.
4. **Do NOT expose ports** (`ports:`) — all traffic goes through Caddy.
5. **Create `caddy/<project>.conf`** in the project repo with site blocks for each domain.
6. **On deploy:** copy conf to `/opt/caddy/conf.d/<project>/`, add `import` line to main Caddyfile, run `docker exec caddy caddy reload`.

### docker-compose.yml template

```yaml
services:
  bot:
    build: .
    container_name: <project>_bot
    restart: unless-stopped
    env_file: .env
    networks:
      - default
      - webapps_network

  web:
    build: web/frontend
    container_name: <project>_web
    restart: unless-stopped
    networks:
      - default
      - webapps_network

  db:
    image: postgres:16-alpine
    container_name: <project>_db
    restart: unless-stopped
    # no webapps_network needed — only internal access

  redis:
    image: redis:7-alpine
    container_name: <project>_redis
    restart: unless-stopped

networks:
  webapps_network:
    external: true
```

### Caddy conf template (`caddy/<project>.conf`)

```caddyfile
bot.<domain> {
    log {
        output file /var/log/caddy/<project>-bot-access.log {
            roll_size 100mb
            roll_keep 5
        }
        format json
    }

    handle /webhook/* {
        reverse_proxy <project>_bot:8080 {
            header_up Host {host}
            header_up X-Real-IP {remote_host}
        }
    }

    handle /api/* {
        reverse_proxy <project>_bot:8081 {
            header_up Host {host}
            header_up X-Real-IP {remote_host}
        }
    }

    handle /health {
        respond "OK" 200
    }

    handle {
        respond "Not Found" 404
    }
}

web.<domain> {
    log {
        output file /var/log/caddy/<project>-web-access.log {
            roll_size 100mb
            roll_keep 5
        }
        format json
    }

    encode gzip

    header {
        X-Frame-Options "SAMEORIGIN"
        X-Content-Type-Options "nosniff"
        X-XSS-Protection "1; mode=block"
        Referrer-Policy "strict-origin-when-cross-origin"
    }

    handle /health {
        respond "OK" 200
    }

    handle /app/* {
        reverse_proxy <project>_web:3000 {
            header_up Host {host}
            header_up X-Real-IP {remote_host}
        }
    }

    handle {
        reverse_proxy <project>_web:3000 {
            header_up Host {host}
            header_up X-Real-IP {remote_host}
        }
    }
}
```

### Frontend container (Caddy inside)

Frontend containers use **Caddy** (not `serve` or `nginx`) to serve static files with SPA fallback:

```dockerfile
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
COPY . .
RUN npm run build

FROM caddy:2-alpine
COPY --from=build /app/dist /srv/app
COPY Caddyfile /etc/caddy/Caddyfile
EXPOSE 3000
```

Frontend Caddyfile (`web/frontend/Caddyfile`):

```caddyfile
:3000 {
    root * /srv
    file_server

    @spa path /app /app/*
    handle @spa {
        try_files {path} /app/index.html
    }
}
```

### Deploy checklist

```bash
# On server:
mkdir -p /opt/caddy/conf.d/<project>
cp /root/<project>/caddy/<project>.conf /opt/caddy/conf.d/<project>/
echo 'import /etc/caddy/conf.d/<project>/*.conf' >> /opt/caddy/Caddyfile
cd /root/<project> && docker compose up -d --build
docker exec caddy caddy reload --config /etc/caddy/Caddyfile
```

---

## 14. File Structure (updated)

```
project/
├── app/
│   ├── core/           # config, logging, lifespan
│   ├── handlers/       # route/message handlers (thin)
│   ├── services/       # business logic
│   ├── workers/        # background tasks
│   ├── middleware/      # request middleware
│   ├── cache/          # Redis wrappers, keys, TTLs
│   ├── db/
│   │   ├── base.py     # base repository
│   │   ├── pool.py     # connection pool
│   │   └── repositories/
│   └── utils/          # pure helpers
├── alembic/            # migrations
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env
```

---

## 15. AI Agent Rules

### NEVER

- NEVER use `Optional[T]`. Use `T | None`.
- NEVER block the event loop. No `requests`, `time.sleep()`, sync DB.
- NEVER put business logic in handlers. Handlers are wiring only.
- NEVER catch bare `Exception` and silently discard it.
- NEVER create HTTP clients per request. Reuse.
- NEVER use relative imports.
- NEVER add docstrings/comments/types to code you didn't change.
- NEVER use `if not x` when checking for `None` (0 and "" are falsy).

### ALWAYS

- ALWAYS add `from __future__ import annotations` as first import.
- ALWAYS annotate every function parameter and return type.
- ALWAYS use `is None` / `is not None` for None comparisons.
- ALWAYS use `async def` for any function that does I/O.
- ALWAYS close async resources on shutdown.
- ALWAYS commit and push after changes (project-specific).

### PREFER

- PREFER small functions (< 30 lines).
- PREFER `structlog` for structured logging.
- PREFER early returns over deep nesting.
- PREFER composition over inheritance.
- PREFER explicit over implicit.

---

## 16. Frontend / UI

- For any frontend, UI/UX, or design-related work — **ALWAYS** use the `/ui-ux-pro-max` skill.
- This includes: web interfaces, admin panels, dashboards, landing pages, Telegram WebApp (Mini App) frontends.

---

## 17. Priority

**Security > Correctness > Consistency > Brevity.**

When rules conflict, follow this order. If a style rule would introduce a vulnerability, break the style rule.
