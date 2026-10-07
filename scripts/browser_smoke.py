"""Headless browser acceptance against an isolated Mock application, no API keys."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import httpx
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]


async def check():
    target = ROOT / 'data/runtime/verification'
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cardio-browser-') as directory:
        env = {**os.environ, 'LLM_BACKEND':'mock', 'DEEPSEEK_API_KEY':'', 'CHROMA_MODE':'disabled', 'NETWORK_ENABLED':'false', 'AGENTIC_ENABLED':'false',
               'REDIS_URL':'', 'EMBEDDING_BACKEND':'deterministic', 'DATA_DIR':directory,
               'EVALUATION_DIR':str(Path(directory)/'evaluations')}
        process = subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8012'],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            async with httpx.AsyncClient(timeout=1) as client:
                for _ in range(60):
                    if process.poll() is not None:
                        raise RuntimeError('UI test server exited before readiness')
                    try:
                        if (await client.get('http://127.0.0.1:8012/api/v1/health')).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(.5)
                else:
                    raise RuntimeError('UI test server failed to start')
            async with async_playwright() as p:
                browser = await p.chromium.launch(channel='chrome', headless=True)
                page = await browser.new_page(viewport={'width':1440,'height':1000})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.goto('http://127.0.0.1:8012')
                await page.locator('#runtimeStatus').filter(has_text='mock').wait_for()
                await page.locator('[data-view="cases"]').click()
                await page.locator('#caseEditor').fill(json.dumps({'age':58,'chief_complaint':'研究病例','medications':[{'name':'阿司匹林'}]},ensure_ascii=False))
                await page.locator('#caseForm button[type=submit]').click()
                await page.locator('#caseStatus').filter(has_text='已保存').wait_for()
                await page.locator('#bindCase').click()
                assert await page.locator('#caseSelect').input_value()
                await page.locator('#messageInput').fill('胸痛，大汗')
                await page.locator('#sendChat').click()
                await page.locator('.message.emergency').filter(has_text='120').wait_for()
                await page.locator('#tracePanel summary').click()
                await page.locator('#traceState').filter(has_text='模型调用 0 次').wait_for()
                await page.screenshot(path=str(target/'console-desktop.png'),full_page=True)
                await page.reload()
                assert '120' in await page.locator('#messages').inner_text()
                await page.locator('[data-view="evaluations"]').click()
                await page.locator('#evalCount').fill('1')
                await page.locator('#runEvaluation').click()
                await page.locator('#evalProgress').filter(has_text='completed').wait_for(timeout=30000)
                assert 'recall_at_5' in await page.locator('#evalMetrics').inner_text()
                await page.screenshot(path=str(target/'console-evaluation.png'),full_page=True)
                await page.set_viewport_size({'width':390,'height':844})
                await page.locator('[data-view="chat"]').click()
                assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                await page.screenshot(path=str(target/'console-mobile.png'),full_page=True)
                assert not errors, errors
                await browser.close()
                report = {'status':'passed','checks':['status','case_create','case_binding','emergency','zero_model_calls','trace','session_restore','background_evaluation','mobile_width','no_js_errors']}
                (target/'browser.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps(report))
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    asyncio.run(check())
