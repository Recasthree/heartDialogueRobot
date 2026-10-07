"""Optional local KG gateway: POST {query} -> {evidence: [EvidenceItem]}."""
import httpx
from ..models import EvidenceItem
from ..privacy import redact_text


class CPubMedKGTool:
    def __init__(self, url=""):
        self.url = url
        self.client = httpx.AsyncClient(timeout=6)

    async def search(self, query):
        if not self.url:
            return []
        response = await self.client.post(self.url, json={"query": redact_text(query)[0]})
        response.raise_for_status()
        return [EvidenceItem.model_validate(row) for row in response.json().get("evidence", [])][:5]

    async def query_entity(self, entity):
        return {"configured": bool(self.url), "evidence": await self.search(entity)}

    async def close(self):
        await self.client.aclose()

