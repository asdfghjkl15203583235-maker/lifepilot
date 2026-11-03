# Chief of Staff — Cursor Agent Rules

# These rules apply to every file in this project. Follow them always.

---

## Project Context

This is the Personal Chief of Staff — an autonomous AI agent that connects to Gmail, Google Calendar, and Notion via MCP, retrieves context via RAG, and generates outputs using a fine-tuned LLM. It asks for user approval before taking any real-world action.

Stack: FastAPI (Python 3.11), Next.js 14, LangGraph, MCP, Pinecone/ChromaDB, QLoRA fine-tuning, Ollama, Docker, AWS, Terraform.

---

## General Rules

- Write code that a tired version of yourself at 2am can read without asking anyone
- Optimise for readability first, cleverness never
- When in doubt, do the simpler thing
- Every file you touch should be left cleaner than you found it (Boy Scout Rule)

---

## Naming

- Use descriptive, intention-revealing names — `get_user_email_thread()` not `get_data()`
- No abbreviations unless universally understood (`id`, `url`, `api` are fine — `usr`, `msg`, `cfg` are not)
- Boolean variables and functions start with `is_`, `has_`, `can_`, `should_`
- Constants are UPPER_SNAKE_CASE
- Classes are PascalCase
- Functions and variables are snake_case (Python) or camelCase (TypeScript/JS)
- File names are snake_case (Python) or kebab-case (TypeScript/JS)

---

## Functions

- One function does one thing. If you use "and" to describe what it does, split it
- Keep functions under 20 lines where possible
- Functions that return early are cleaner than deeply nested if-else — prefer guard clauses
- Pure functions over functions with side effects wherever possible
- Name the function after what it returns, not what it does internally

```python
# bad
def process(data):
    ...

# good
def extract_email_thread(gmail_message: dict) -> EmailThread:
    ...
```

---

## No Magic Numbers or Strings

- Never hardcode raw values inline — name every constant

```python
# bad
if len(chunks) > 5:
    ...

# good
MAX_RETRIEVAL_CHUNKS = 5
if len(chunks) > MAX_RETRIEVAL_CHUNKS:
    ...
```

---

## Comments

- Comments explain WHY, not WHAT
- If code needs a comment to explain what it does, rewrite the code
- Delete commented-out code — that's what git is for
- TODO comments must include a reason: `# TODO: replace with streaming once LangGraph supports it`

```python
# bad
# loop through emails
for email in emails:
    ...

# good
# Gmail API returns newest-first; we reverse so RAG context is chronological
for email in reversed(emails):
    ...
```

---

## Error Handling

- Fail fast — validate inputs at the top of every function
- Never swallow exceptions silently
- Raise specific exceptions, not generic `Exception`
- Always log the error with context before re-raising

```python
# bad
try:
    result = call_gmail_api()
except:
    pass

# good
try:
    result = call_gmail_api()
except GoogleAuthError as e:
    logger.error(f"Gmail auth failed for user {user_id}: {e}")
    raise
```

---

## DRY — Don't Repeat Yourself

- If you write the same logic twice, extract it into a function
- If you write the same logic three times, it needs its own module
- Shared constants live in `backend/config.py` — never duplicate them

---

## Separation of Concerns

- Agent logic lives in `agents/` — never import FastAPI or frontend concerns here
- RAG logic lives in `rag/` — agents call retriever functions, they don't embed directly
- MCP tool wrappers live in `agents/tools/` — raw API calls never happen inside nodes
- Database/vector store access only happens through service functions, never inline in routers
- Business logic never goes inside FastAPI route handlers — handlers are thin, services are thick

---

## Python-Specific Rules

- Type hints on every function signature — inputs and return type
- Use Pydantic models for all data that crosses a boundary (API in/out, agent state)
- Prefer `dataclass` or Pydantic over raw dicts for structured internal data
- Use `async/await` consistently — never mix sync and async in the same call chain
- Never use `import *`
- Keep imports grouped: stdlib → third-party → local, each group alphabetically sorted

```python
# good
import os
from typing import Optional

import httpx
from pydantic import BaseModel

from agents.graph import AgentState
from rag.retriever import retrieve_context
```

---

## TypeScript/Next.js-Specific Rules

- Use TypeScript strictly — no `any` types
- Define interfaces for all props and API response shapes
- Use `const` by default, `let` only when reassignment is needed, never `var`
- Keep components small — if a component file exceeds 150 lines, split it
- Fetch logic never lives inside components — use custom hooks in `hooks/`
- No inline styles — use Tailwind utility classes only

---

## LangGraph / Agent Rules

- Every node function must have a clear single responsibility
- Agent state (`AgentState`) is the only way nodes communicate — no direct function calls between nodes
- Nodes must never call external APIs directly — always go through a tool wrapper
- Every tool call must have a timeout and a fallback
- The approval node must always be present in any graph that performs write actions
- Log the intent classification result at every run — it's the most important debug signal

```python
# every node follows this signature
def draft_node(state: AgentState) -> AgentState:
    ...
    return state
```

---

## RAG Rules

- Chunk size: 512 tokens, overlap: 64 tokens — do not change without testing retrieval quality
- Always include source metadata when upserting to the vector store
- Retrieval always returns top-5 chunks by default — make this configurable via env var
- Never embed directly in an agent node — always call `retrieve_context()` from `rag/retriever.py`

---

## Environment and Config

- All secrets live in `.env` — never hardcode API keys, even in tests
- All config values are read once in `backend/config.py` using Pydantic Settings
- Use `.env.example` to document every required variable — keep it up to date

---

## Testing Rules

- Every agent node must have a unit test
- Every FastAPI route must have an integration test
- Mock all external API calls in tests — no real Gmail/Calendar/Notion calls in the test suite
- Test file names mirror source file names: `agents/draft_node.py` → `tests/test_draft_node.py`
- A test that doesn't assert anything is worse than no test

---

## Git and File Hygiene

- One concern per commit — don't mix feature work with formatting fixes
- Commit messages are imperative: `Add approval node to LangGraph graph` not `Added` or `Adding`
- Never commit `.env`, model weights, or `__pycache__`
- Delete dead code — do not comment it out and leave it

---

## The Non-Negotiables

1. No magic numbers or strings inline — ever
2. No silent exception swallowing — ever
3. No business logic inside route handlers — ever
4. No real-world action (send email, create event) without passing through the approval node — ever
5. No API keys in source code — ever
