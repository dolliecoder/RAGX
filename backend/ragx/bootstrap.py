"""Import every module that registers job handlers, and initialise storage."""

from __future__ import annotations

import logging


def register_handlers() -> None:
    from . import evals, tasks  # noqa: F401
    from .reflex import deep  # noqa: F401
    from .repair import service  # noqa: F401


def init_app() -> None:
    from .config import get_settings
    from .control import ensure_initial_config
    from .db import init_db, session_scope

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    get_settings().data_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    with session_scope() as s:
        ensure_initial_config(s)
    register_handlers()
