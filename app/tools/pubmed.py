"""Optional NCBI E-utilities adapter, disabled by default."""
import asyncio
from xml.etree import ElementTree
import httpx
from ..models import EvidenceItem
from ..privacy import redact_text


class PubMedTool:
    def __init__(self, enabled=False):
        self.enabled = enabled
        self.client = httpx.AsyncClient(base_url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/", timeout=6)
        self.lock = asyncio.Lock()

    async def search(self, query):
        if not self.enabled:
            return []
        async with self.lock:
            response = await self.client.get("esearch.fcgi", params={"db": "pubmed", "term": redact_text(query)[0], "retmode": "json", "retmax": 5})
            response.raise_for_status()
            ids = response.json()["esearchresult"]["idlist"]
            if not ids:
                return []
            await asyncio.sleep(.35)
            response = await self.client.get("efetch.fcgi", params={"db": "pubmed", "id": ",".join(ids), "retmode": "xml"})
            response.raise_for_status()
            rows = []
            for article in ElementTree.fromstring(response.content).findall(".//PubmedArticle"):
                pmid = article.findtext(".//PMID")
                title = "".join(article.find(".//ArticleTitle").itertext())
                abstract = "\n".join("".join(a.itertext()) for a in article.findall(".//AbstractText"))
                if abstract and pmid:
                    rows.append(EvidenceItem(evidence_id="pubmed:" + pmid, source_id=pmid, source_type="pubmed",
                        title=title, content=abstract, url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", is_demo=False,
                        metadata={"partition": "evidence", "retrieval_allowed": True, "external": True}))
            return rows

    async def search_pubmed(self, query, max_results=5):
        return (await self.search(query))[:max_results]

    async def close(self):
        await self.client.aclose()

