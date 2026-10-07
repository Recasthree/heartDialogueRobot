"""Read-only medical evidence with pinned public DNS, bounded bodies and provenance."""
import asyncio
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import socket
import time
from urllib.parse import urlsplit, urljoin, urlencode
from xml.etree import ElementTree as ET

import httpx
from httpcore._backends.auto import AutoBackend
from ..models import EvidenceItem
from ..privacy import redact_text
from ..retrieval.hybrid import stable_evidence_id


class BlockedURL(ValueError):
    pass


def valid_url(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            raise BlockedURL("url_not_allowed")
        host = parsed.hostname.lower().rstrip(".")
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
            raise BlockedURL("private_host")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise BlockedURL("private_ip")
        return parsed
    except ValueError as exc:
        raise BlockedURL("url_not_allowed") from exc


def on_domain(url, domains):
    host = valid_url(url).hostname.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


class PublicNetworkBackend:
    """Connect to the IP we validated; TLS SNI remains the original hostname."""
    def __init__(self, dns_mode="system"):
        self.backend = AutoBackend()
        if dns_mode not in {"system", "public_doh"}:
            raise ValueError("invalid_network_dns_mode")
        self.dns_mode = dns_mode
        self.cache = {}

    async def resolve(self, host, port, timeout):
        try:
            return [str(ipaddress.ip_address(host))]
        except ValueError:
            pass
        if self.dns_mode == "system":
            addresses = await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM), timeout or 10)
            return list(dict.fromkeys(row[4][0] for row in addresses))
        cached = self.cache.get(host)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        # Numeric bootstrap avoids this machine's fake-IP DNS. HTTPS certificate
        # validation stays enabled; redirects/proxies are disabled. Only hostnames leave.
        async with httpx.AsyncClient(timeout=min(timeout or 10, 10), trust_env=False, follow_redirects=False) as dns:
            async with dns.stream("GET", "https://1.1.1.1/dns-query", params={"name":host,"type":"A"}, headers={"Accept":"application/dns-json"}) as response:
                response.raise_for_status()
                parts, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 65536:
                        raise BlockedURL("dns_response_too_large")
                    parts.append(chunk)
        value = json.loads(b"".join(parts))
        records = [r for r in value.get("Answer", []) if r.get("type") == 1]
        ips = list(dict.fromkeys(r["data"] for r in records))
        if value.get("Status") != 0 or not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise BlockedURL("non_public_dns_answer")
        ttl = min(300, min(max(0, r.get("TTL", 0)) for r in records))
        self.cache[host] = (time.monotonic() + ttl, ips)
        return ips

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        ips = await self.resolve(host, port, timeout)
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise BlockedURL("private_dns_address")
        return await self.backend.connect_tcp(ips[0], port, timeout=timeout,
                                             local_address=local_address, socket_options=socket_options)

    async def connect_unix_socket(self, *args, **kwargs):
        raise BlockedURL("unix_socket_forbidden")

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


def public_client(dns_mode="system"):
    transport = httpx.AsyncHTTPTransport(retries=0)
    transport._pool._network_backend = PublicNetworkBackend(dns_mode)
    return httpx.AsyncClient(transport=transport, timeout=15, trust_env=False, follow_redirects=False,
                             headers={"User-Agent": "HeartDialogueResearch/0.4 (local research)", "Accept-Encoding": "identity"})


def network_query(text):
    # Do not send paragraphs, clinical records, identifiers or patient measurements.
    text = redact_text(text)[0]
    text = re.sub(r"https?://\S+|\[[^\]]+\]|\b\d+(?:\.\d+)?(?:\s*/\s*\d+)?\b", " ", text)
    text = re.sub(r"姓名|患者|住院号|身份证|电话|地址|病历号", " ", text)
    text = re.sub(r"[^\w\u4e00-\u9fff\s-]", " ", text)
    return " ".join(text.split())[:200]


class NetworkEvidence:
    def __init__(self, settings, attachments=None, client=None):
        self.cfg, self.attachments = settings, attachments
        self.client = client or public_client(settings.network_dns_mode)
        self.domains = tuple(d.strip().lower() for d in settings.web_allowed_domains.split(",") if d.strip())
        self.ncbi_lock = asyncio.Lock()
        self.cache = Path(settings.data_dir) / "network-cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.drug_names = {}
        aliases = Path(__file__).resolve().parents[2] / "config/drug_aliases.json"
        if aliases.exists():
            for name, values in json.loads(aliases.read_text(encoding="utf-8")).items():
                self.drug_names[name.lower()] = set(values)
        path = Path(settings.knowledge_root) / "knowledge/drug_concepts.jsonl"
        if path.exists():
            with path.open(encoding="utf-8") as file:
                for line in file:
                    try:
                        row = json.loads(line)
                        english = row.get("name_en") or row.get("generic_name_en") or row.get("drug_name_en")
                        if english:
                            for key in ("name", "name_zh", "generic_name_zh", "drug_name_zh"):
                                if row.get(key):
                                    self.drug_names.setdefault(str(row[key]).lower(), set()).add(str(english))
                    except (ValueError, TypeError):
                        continue

    async def request(self, url, *, method="GET", payload=None, headers=None, limit=3_000_000, redirect_domains=None):
        for attempt in range(4):
            valid_url(url)
            async with self.client.stream(method, url, json=payload, headers=headers) as response:
                if response.is_redirect:
                    target = urljoin(url, response.headers.get("location", ""))
                    valid_url(target)
                    if redirect_domains and not on_domain(target, redirect_domains):
                        raise BlockedURL("redirect_outside_allowed_sources")
                    if urlsplit(target).netloc != urlsplit(url).netloc:
                        headers = None  # Never forward API credentials across origins.
                    url = target
                    continue
                response.raise_for_status()
                if int(response.headers.get("content-length", "0")) > limit:
                    raise ValueError("response_size_limit")
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("response_size_limit")
                    chunks.append(chunk)
                return b"".join(chunks), response.headers.get("content-type", ""), str(response.url)
        raise BlockedURL("redirect_limit")

    def evidence(self, text, title, url, kind, source_id="", page=None, **metadata):
        # Bounded chunks keep all supplied text addressable; callers select relevant sections.
        out = []
        digest = hashlib.sha256(text.encode()).hexdigest()
        for start in range(0, min(len(text), 24000), 1800):
            content = text[start:start + 2000]
            if not content.strip():
                continue
            out.append(EvidenceItem(evidence_id=stable_evidence_id(url, source_id, str(start), content),
                source_id=source_id or url, title=title[:300], content=content, source_type=kind,
                url=url, page=page, is_demo=False, source_sha256=digest,
                metadata={"external": True, "partition": "session_evidence", "retrieval_allowed": True,
                          "retrieved_at": datetime.now(timezone.utc).isoformat(),
                          "untrusted_input": True, "document_truncated": len(text) > 24000, **metadata}))
        return out

    async def pubmed(self, query):
        query = network_query(query)
        async with self.ncbi_lock:
            base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
            data, _, _ = await self.request(base + "esearch.fcgi?" + urlencode({"db": "pubmed", "term": query, "retmode": "json", "retmax": 3, "sort": "relevance"}))
            ids = json.loads(data)["esearchresult"]["idlist"]
            await asyncio.sleep(.36)
            if not ids:
                return {"status": "no_record", "evidence": []}
            data, _, _ = await self.request(base + "efetch.fcgi?" + urlencode({"db": "pubmed", "id": ",".join(ids), "retmode": "xml"}))
            await asyncio.sleep(.36)
        if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
            # NCBI supplies a harmless public DTD: strip its declaration, never resolve it.
            data = re.sub(rb"<!DOCTYPE[^>]*>", b"", data)
            if b"<!ENTITY" in data:
                raise ValueError("xml_entity_forbidden")
        rows = []
        for article in ET.fromstring(data).findall(".//PubmedArticle"):
            pmid = article.findtext(".//PMID")
            title_node = article.find(".//ArticleTitle")
            title = "".join(title_node.itertext()) if title_node is not None else "PubMed"
            text = "\n".join("".join(n.itertext()) for n in article.findall(".//AbstractText"))
            if pmid and text:
                rows.extend(self.evidence(text, title, f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", "pubmed_abstract", pmid,
                    publication_types=[n.text for n in article.findall(".//PublicationType")],
                    document_date=article.findtext(".//PubDate/Year"), evidence_extent="abstract"))
        return {"status": "available" if rows else "no_record", "evidence": [r.model_dump() for r in rows]}

    async def labels(self, query):
        name = query.strip().lower()
        if re.search(r"[\u4e00-\u9fff]", name):
            names = self.drug_names.get(name, set())
            if len(names) != 1:
                return {"status": "needs_confirmation", "error": "drug_ingredient_ambiguous", "evidence": []}
            name = next(iter(names))
        if not re.fullmatch(r"[A-Za-z][A-Za-z -]{1,99}", name):
            return {"status": "needs_confirmation", "error": "explicit_generic_drug_name_required", "evidence": []}
        query_url = "https://api.fda.gov/drug/label.json?" + urlencode({"search": f'openfda.generic_name:"{name}"', "limit": 2})
        try:
            data, _, _ = await self.request(query_url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return {"status": "no_record", "evidence": []}
            raise
        rows = []
        for label in json.loads(data).get("results", []):
            for section in ["indications_and_usage", "warnings", "contraindications", "drug_interactions", "adverse_reactions"]:
                text = "\n".join(label.get(section, []))
                if text:
                    rows.extend(self.evidence(text, name + " / " + section, query_url, "drug_label_openfda", label.get("id", ""),
                        jurisdiction="US", document_date=label.get("effective_time"), section=section))
        return {"status": "available" if rows else "no_record", "evidence": [r.model_dump() for r in rows[:20]]}

    async def search_web(self, query):
        if not self.cfg.tavily_api_key:
            return {"status": "not_configured", "error": "tavily_key_missing", "leads": []}
        data, _, _ = await self.request("https://api.tavily.com/search", method="POST",
            payload={"query": network_query(query), "search_depth": "basic", "max_results": 3,
                     "include_domains": list(self.domains), "include_answer": False, "include_raw_content": False},
            headers={"Authorization": "Bearer " + self.cfg.tavily_api_key})
        leads = [{"url": row["url"], "title": row.get("title", "")[:300], "snippet": row.get("content", "")[:500]}
                 for row in json.loads(data).get("results", []) if on_domain(row["url"], self.domains)]
        return {"status": "available" if leads else "no_record", "leads": leads, "evidence": []}

    async def fetch(self, url, scope, user_provided=False):
        if not user_provided and not on_domain(url, self.domains):
            raise BlockedURL("source_domain_not_allowed")
        data, mime, final_url = await self.request(url, limit=20 * 1024 * 1024,
            redirect_domains=None if user_provided else self.domains)
        rows = []
        if data.startswith(b"%PDF-"):
            if not self.attachments:
                raise RuntimeError("pdf_parser_unavailable")
            item = self.attachments.create(data, "network-reference.pdf", scope, "reference")
            await self.attachments.parse(item["id"], scope)
            parsed = self.attachments.get(item["id"], scope)
            if parsed["status"] != "ready":
                return {"status": "unavailable", "error": parsed["error"], "evidence": []}
            for page in parsed["pages"]:
                if page["method"] == "ocr" and (page["low_confidence"] or page["critical_lines"]):
                    continue  # Never silently trust numeric/low-confidence OCR in a fetched document.
                rows.extend(self.evidence(page["text"], "PDF reference", final_url, "web_pdf", page=page["page"], user_provided=user_provided))
        elif "html" in mime or "text/plain" in mime:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(data, "html.parser")
            title = soup.title.get_text(" ", strip=True) if soup.title else final_url
            from .patient_sources import education_metadata
            provenance = education_metadata(final_url)
            date_tag = soup.find('meta', attrs={'property': 'article:modified_time'}) or soup.find('time')
            document_date = (date_tag.get('content') or date_tag.get('datetime') or date_tag.get_text(' ', strip=True)) if date_tag else None
            for tag in soup(["script", "style", "nav", "header", "footer", "form", "iframe"]):
                tag.decompose()
            body = soup.find("article") or soup.find("main") or soup.body or soup
            text = body.get_text(" ", strip=True)
            if len(text) >= 80:
                rows = self.evidence(text, title, final_url, "web_reference", user_provided=user_provided,
                                     document_date=document_date, **provenance)
        else:
            raise ValueError("unsupported_web_content")
        # Cache only public source evidence, never the patient query or uploaded attachments.
        if rows:
            key = hashlib.sha256(final_url.encode()).hexdigest()
            (self.cache / (key + ".json")).write_text(json.dumps({"expires_at": time.time() + 86400,
                "evidence": [r.model_dump() for r in rows]}, ensure_ascii=False), encoding="utf-8")
        return {"status": "available" if rows else "no_record", "evidence": [r.model_dump() for r in rows]}

    def cleanup(self):
        for path in self.cache.glob("*.json"):
            if re.fullmatch(r"[a-f0-9]{64}\.json", path.name) and not path.is_symlink():
                try:
                    if json.loads(path.read_text(encoding="utf-8")).get("expires_at", 0) <= time.time():
                        path.unlink()
                except (ValueError, OSError):
                    continue

    async def close(self):
        await self.client.aclose()
