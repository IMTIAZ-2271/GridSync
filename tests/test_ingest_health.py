"""Ingest's liveness endpoint, as an uptime monitor probes it."""
from __future__ import annotations

import httpx
import pytest

from services.ingest.main import app

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_health_answers_get_and_head(method):
    """UptimeRobot's free tier probes with HEAD; a 405 there reads as down."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ingest.test"
    ) as client:
        response = await client.request(method, "/health")

    assert response.status_code == 200
