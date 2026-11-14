"""FastAPI dependencies shared by the routers."""
from __future__ import annotations

from fastapi import Request

from api.services import Services


def get_services(request: Request) -> Services:
    return request.app.state.services
