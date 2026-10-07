import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.config import Settings


@pytest.fixture
async def app(tmp_path):
    instance = create_app(Settings(llm_backend='mock', chroma_mode='disabled', redis_url='', agentic_enabled=False, network_enabled=False,
        embedding_backend='deterministic', data_dir=str(tmp_path), evaluation_dir=str(tmp_path / 'evaluations')))
    async with instance.router.lifespan_context(instance):
        yield instance


@pytest.mark.asyncio
async def test_health_and_emergency_response(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        health = await client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json()['dependency_probe'] == 'cached'
        response = await client.post("/api/v1/chat", json={"message": "突然胸痛并大汗"})
        assert response.status_code == 200
        body = response.json()
        assert body["is_emergency"] is True
        assert "120" in body["answer"]
        assert "300mg" not in body["answer"]


@pytest.mark.asyncio
async def test_case_lifecycle_and_mock_chat(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/api/v1/cases", json={"age": 58, "chief_complaint": "演示数据"})
        assert created.status_code == 200
        case_id = created.json()["case_id"]
        patched = await client.patch("/api/v1/cases/%s" % case_id, json={"sex": "未知"})
        assert patched.status_code == 200
        deleted = await client.delete("/api/v1/cases/%s" % case_id)
        assert deleted.status_code == 200
        chat = await client.post("/api/v1/chat", json={"message": "普通演示问题"})
        assert chat.status_code == 200
        assert chat.json()["evidence_status"] in ["demo", "none", "unavailable"]

