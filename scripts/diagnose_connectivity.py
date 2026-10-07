"""Read-only connectivity checks in the same Python environment as the API.

No model generation is performed. Only status, timings and error types are
reported; credentials and response bodies are never logged.
"""
import asyncio
import json
import socket
import time
from urllib.parse import urlsplit

import httpx

from app.config import Settings
from app.tools.network import public_client


def error_summary(exc):
    chain = []
    while exc is not None and len(chain) < 5:
        chain.append({"type": type(exc).__name__, "errno": getattr(exc, "errno", None),
                      "winerror": getattr(exc, "winerror", None)})
        exc = exc.__cause__ or exc.__context__
    return chain


async def probe(name, client, url, headers=None):
    started = time.perf_counter()
    try:
        response = await client.get(url, headers=headers)
        return {"name": name, "http_status": response.status_code,
                "reachable": True, "seconds": round(time.perf_counter() - started, 2)}
    except Exception as exc:
        return {"name": name, "reachable": False, "errors": error_summary(exc),
                "seconds": round(time.perf_counter() - started, 2)}


async def main():
    cfg = Settings.from_env()
    domain = urlsplit(cfg.aihubmix_base_url).hostname
    try:
        addresses = sorted({row[4][0] for row in socket.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)})
    except OSError as exc:
        addresses = [type(exc).__name__]
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as model, public_client(cfg.network_dns_mode) as medical:
        report = {"model_host": domain, "model_dns_addresses": addresses,
                  "network_dns_mode": cfg.network_dns_mode, "paid_model_requests": 0,
                  "checks": await asyncio.gather(
                      probe("model_api", model, cfg.aihubmix_base_url.rstrip('/') + '/models',
                            {"Authorization": "Bearer " + cfg.deepseek_api_key} if cfg.deepseek_api_key else None),
                      probe("pubmed_with_application_transport", medical,
                            'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=arrhythmia%20symptoms&retmode=json&retmax=1'))}
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return all(check.get('http_status') == 200 for check in report['checks'])


if __name__ == '__main__':
    raise SystemExit(0 if asyncio.run(main()) else 1)
