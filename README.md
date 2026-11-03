# LifePilot — Personal AI Multi-Agent Life Automation Manager

LifePilot is a personal AI agent that plans and automates life administration: meeting extraction, scheduling, proactive reminders, long-term memory, task planning, weather/map lookups, and optional Tencent Meeting control. Real-world actions stay human-in-the-loop; the agent drafts, proposes, and waits for approval before sending or modifying anything.

---

## What it does

You type a request like:

> "Plan my next three priorities using my saved preferences, long-term memory, and connected tools."

LifePilot decomposes it with a planner, runs ready steps in parallel with domain agents, supervises results, replans blocked steps, and persists planned tasks back into the task board.

Email login uses a **163 邮箱授权码** from `backend/.env`; the backend reads the mailbox over IMAP and sends mail over SMTP, so no Google OAuth login screen is shown.

Key capabilities:

- **163 邮箱登录与收发** — `.env` 中的 `MAIL163_EMAIL` + `MAIL163_EMAIL_AUTH_CODE` 登录应用并驱动 IMAP 收信、SMTP 发信
- **Planner + Supervisor orchestration** — dependency-aware task decomposition, parallel fan-out, dynamic replanning, persisted `plan_id` and planner task metadata
- **Meeting extraction** — parse meeting time, meeting number, join URL, and source text from email-like invites; auto-create pre-meeting reminders
- **Tencent Meeting API** — optional query/create/update meeting support with idempotent creates
- **Proactive reminders** — deadline, meeting, scheduled-run, and manual reminders; a one-minute APScheduler tick delivers due reminders as in-app notifications
- **Long-term memory** — PostgreSQL + pgvector + VoyageAI embeddings, Redis-cached retrieval, user bio summarization, preference/habit store
- **Life services** — weather and map lookups via Open-Meteo/Nominatim by default, with optional OpenWeatherMap/Google Maps keys
- **Background jobs** — cron-style recurring agent runs with run history, outcome mapping, and execution traces
- **Reliability** — Redis idempotency keys, LangGraph in-process checkpointing, retry/replan supervision, task progress tracking
- **LifePilot dashboard** — dedicated frontend surface for invitations, reminders, memory, preferences, and life-service calls

---

## Tech stack

| Layer | Technology |
|---|---|
| Frontend | Next.js (App Router), Tailwind CSS, React Query |
| Backend | FastAPI, Python 3.11+, SQLAlchemy (async) |
| Agent orchestration | LangGraph planner/supervisor state graph |
| Tool integration | MCP (Model Context Protocol) |
| LLM | Claude / OpenRouter (configurable) |
| Embeddings | VoyageAI |
| Database | PostgreSQL 15+ with `pgvector` |
| Cache | Redis |
| Email | 163 邮箱授权码登录 + IMAP 收信 / SMTP 发信 |
| Voice | SiliconFlow SenseVoiceSmall STT + Edge TTS |
| Infrastructure | AWS ECS Fargate, RDS, ElastiCache, CloudFront, Terraform |

---

## Project structure

```
chief-of-staff/
├── frontend/               # Next.js app
│   ├── app/                # /, /tasks, /notifications, /background-jobs, /calendar, /life-pilot
│   ├── components/         # workspaces, dashboards, chat UI
│   ├── hooks/              # React Query hooks for every API resource
│   └── lib/                # auth, authenticated fetch, storage utilities
│
├── backend/                # FastAPI server
│   ├── routers/            # HTTP endpoints (chat, tasks, reminders, life-services, memory, …)
│   ├── agents/             # LangGraph planner/supervisor orchestration
│   ├── models/             # SQLAlchemy ORM models
│   ├── schemas/            # Pydantic request/response schemas
│   ├── services/           # Business logic
│   ├── jobs/               # APScheduler background jobs
│   ├── db/                 # session, migrations, constants
│   └── tests/              # unittest suite, including LifePilot full-link tests
│
├── infra/                  # Terraform — AWS ECS Fargate + CloudFront deployment
│
└── scripts/                # Helper scripts
    └── deploy-ecs-images.sh
```

---

## API surface

The FastAPI app registers 17 routers. LifePilot-specific endpoints include:

| Router | Prefix | Purpose |
|---|---|---|
| Life services | `/life-services` | weather/map lookups with persisted call history |
| Life preferences | `/life-preferences` | habits, time preferences, work hours, priority rules |
| Memory | `/memory` | vector memory CRUD, query, deduplicate, bio |
| Meeting invites | `/meeting-invites` | extract, upsert, scan inbox → tasks, list upcoming, delete |
| Tencent Meeting | `/tencent-meeting` | optional meeting query/create/update |
| Reminders | `/reminders` | create/list, acknowledge |
| Tasks | `/tasks` | planner-backed task board with project/dependency fields |

Shared routers remain: chat, auth, voice, Gmail, Calendar, Notion, notifications, background jobs, health, and entity creation.

---

## Local development

### Prerequisites

- Node.js 20+
- Python 3.11+
- PostgreSQL 15+ with `pgvector`
- Redis

### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e .

cp .env.example .env
# Fill in DATABASE_URL, JWT_SECRET, OPENROUTER_API_KEY, REDIS_URL, etc.

uvicorn main:app --reload --port 8081
```

Set `INIT_DB_SCHEMA_ON_STARTUP=true` if you want the app to create tables and apply LifePilot migration columns on startup.

### Frontend

```bash
cd frontend
npm install

cp .env.example .env
# Set NEXT_PUBLIC_API_BASE_URL=http://localhost:8081

npm run dev
```

The app runs at `http://localhost:3000`. The LifePilot dashboard is at `/life-pilot`.

---

## Environment variables

### Backend (`backend/.env`)

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | Yes | PostgreSQL async URL (`postgresql+asyncpg://...`) |
| `JWT_SECRET` | Yes | Random 32+ char string for session signing |
| `OPENROUTER_API_KEY` | Yes | LLM access via OpenRouter |
| `REDIS_URL` | Yes | Redis URL (`redis://localhost:6379/0`) |
| `MAIL163_EMAIL` | For login/email | 163 邮箱地址，例如 `user@163.com` |
| `MAIL163_EMAIL_AUTH_CODE` | For login/email | 163 邮箱授权码（需在 163 邮箱设置中开启 IMAP/SMTP 后生成） |
| `MAIL163_SMTP_HOST` | Optional | SMTP 服务器，默认 `smtp.163.com` |
| `MAIL163_SMTP_PORT` | Optional | SMTP 端口，默认 `465` |
| `MAIL163_IMAP_HOST` | Optional | IMAP 服务器，默认 `imap.163.com` |
| `MAIL163_IMAP_PORT` | Optional | IMAP 端口，默认 `993` |
| `GOOGLE_CLIENT_ID` | Legacy Gmail/Calendar | Google OAuth client ID（前端已改用 163 邮箱登录） |
| `GOOGLE_CLIENT_SECRET` | Legacy Gmail/Calendar | Google OAuth client secret |
| `NOTION_CLIENT_ID` | For Notion | Notion OAuth client ID |
| `NOTION_CLIENT_SECRET` | For Notion | Notion OAuth client secret |
| `VOYAGEAI_API_KEY` | For memory/RAG | VoyageAI embeddings |
| `SILICONFLOW_API_KEY` | For voice | SiliconFlow SenseVoiceSmall STT |
| `TENCENT_MEETING_APP_ID` | Optional | Tencent Meeting REST API app ID |
| `TENCENT_MEETING_SECRET_ID` | Optional | Tencent Meeting secret ID |
| `TENCENT_MEETING_SECRET_KEY` | Optional | Tencent Meeting secret key |
| `WEATHER_PROVIDER` | Optional | `open_meteo` (default) or `openweathermap` |
| `MAP_PROVIDER` | Optional | `nominatim` (default) or `google_maps` |
| `OPENWEATHER_API_KEY` | For OpenWeatherMap | Weather provider API key |
| `GOOGLE_MAPS_API_KEY` | For Google Maps | Map provider API key |
| `INIT_DB_SCHEMA_ON_STARTUP` | Optional | Set `true` to auto-create tables on startup |
| `CORS_ALLOWED_ORIGINS` | Optional | Comma-separated allowed origins |

### Frontend (`frontend/.env`)

| Variable | Required | Description |
|---|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | Yes | Backend URL (`http://localhost:8081`) |

---

## Running tests

The backend uses Python's built-in `unittest` runner:

```bash
cd backend
.venv/Scripts/python.exe -m unittest discover -s tests -p "test_*.py"
```

The suite includes LifePilot full-link tests that cover email meeting extraction, automatic meeting reminders, planner task persistence, deadline reminders, reminder tick delivery, and idempotency round-trips.

---

## Deploy to AWS

Infrastructure is managed with Terraform in `infra/`. It provisions:

- **VPC** — public + private subnets across 2 AZs, NAT gateway
- **ECS Fargate** — backend and frontend containerised services behind ALBs
- **CloudFront** — HTTPS edge distribution in front of each ALB
- **ECR** — container registry for both images
- **RDS PostgreSQL 16** — private subnet, encrypted, 7-day backups
- **ElastiCache Redis 7** — private subnet

### First-time setup

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars
# Fill in all secrets in terraform.tfvars

terraform init

# Create ECR repos first so you can push images before the full apply
terraform apply -target=aws_ecr_repository.backend -target=aws_ecr_repository.frontend
```

### Build, push, and deploy — one command

The `scripts/deploy-ecs-images.sh` script handles the full deploy cycle: ECR login, Docker builds (injecting the correct `NEXT_PUBLIC_API_BASE_URL` at build time from Terraform state), image push, and ECS rolling deployment.

```bash
# Full deploy — build both images, push, and roll out new ECS tasks
./scripts/deploy-ecs-images.sh

# Build + push only, skip the ECS rollout
./scripts/deploy-ecs-images.sh --skip-ecs

# Push a versioned tag instead of latest
./scripts/deploy-ecs-images.sh --tag v1.2.3
```

**Requirements:** `docker`, `aws` CLI (authenticated), and `terraform` initialised with applied state in `infra/`.

After the first `terraform apply`, Terraform prints the live URLs:

```
frontend_url = "https://<cloudfront-domain>"   ← your app URL
backend_url  = "https://<cloudfront-domain>"   ← API URL
```

---

## License

MIT
