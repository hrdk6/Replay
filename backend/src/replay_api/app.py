"""ASGI entry point: ``uvicorn replay_api.app:app``."""

from replay_api.main import create_app

app = create_app()
