"""Backend launcher — forces a SelectorEventLoop before uvicorn starts.

psycopg's async driver needs a SelectorEventLoop, but on Windows uvicorn's
built-in `asyncio` loop factory hardcodes ProactorEventLoop (it is only
SelectorEventLoop when reload/workers spawn a subprocess). Since uvicorn 0.36
the loop is created via `asyncio.run(coro, loop_factory=...)`, which ignores
`asyncio.set_event_loop_policy()` entirely — so passing our own factory is the
only way to get a SelectorEventLoop.

Run the backend with:
    .venv/Scripts/python.exe run.py
"""

from __future__ import annotations

import asyncio
import sys


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """Loop factory handed to uvicorn; SelectorEventLoop where psycopg needs it.

    uvicorn passes this straight to `asyncio.run(..., loop_factory=...)`, so it
    must return a loop instance, not a loop class.
    """
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


def configure_event_loop_policy() -> None:
    """Also set the policy for any asyncio.run() outside uvicorn (scripts, tests)."""
    if sys.platform == "win32":
        policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
        if policy is not None and not isinstance(
            asyncio.get_event_loop_policy(), policy
        ):
            asyncio.set_event_loop_policy(policy())


def main() -> None:
    configure_event_loop_policy()

    import uvicorn

    from config import get_settings

    settings = get_settings()
    uvicorn.run(
        "main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
        loop="run:selector_loop_factory",
    )


if __name__ == "__main__":
    main()
