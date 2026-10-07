"""Real Chromium UI smoke on an isolated Mock app. No paid model or public network."""
import asyncio
import json
from pathlib import Path
import socket
import sys
import time

import uvicorn

from app.config import Settings, ROOT
from app.main import create_app
from app.models import EvidenceItem


async def main():
    output = ROOT/'data/runtime/verification'/('chat-ui-'+time.strftime('%Y%m%d-%H%M%S'))
    output.mkdir(parents=True)
    sock = socket.socket(); sock.bind(('127.0.0.1',0)); port = sock.getsockname()[1]
    app = create_app(Settings(llm_backend='mock', embedding_backend='deterministic', redis_url='',
        chroma_mode='disabled', agentic_enabled=False, network_enabled=False,
        data_dir=str(output/'store'), knowledge_root=str(output/'empty'), evaluation_dir=str(output/'evaluations')))
    server = uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',access_log=False))
    running = asyncio.create_task(server.serve(sockets=[sock]))
    errors = []; checks = []; report = {'paid_model_requests':0, 'backend':'isolated Mock'}
    try:
        for _ in range(600):
            if server.started: break
            if running.done(): await running
            await asyncio.sleep(.1)
        assert server.started
        await app.state.services.retriever.add([EvidenceItem(
            evidence_id='ui-evidence', source_id='ui-evidence', source_type='clinical_guideline',
            title='房颤检查示例资料', content='示例资料：心电图和动态心电图可用于记录心律。',
            is_demo=False, medical_truth_verified=False, metadata={'partition':'evidence'}), EvidenceItem(
            evidence_id='ui-second', source_id='ui-second', source_type='clinical_guideline',
            title='房颤检查补充示例', content='房颤检查示例：动态心电图记录心律变化。',
            is_demo=False, medical_truth_verified=False, metadata={'partition':'evidence'})])
        original_generate = app.state.services.llm.generate
        async def formatted_generation(query, evidence):
            if '房颤' in query and len(evidence) >= 2:
                return json.dumps({'answer':'**常见检查**：** 心电图 **和动态心电图可用于记录心律[1][2]。',
                    'citation_ids':[e.evidence_id for e in evidence[:2]], 'support_mode':'cited'}, ensure_ascii=False)
            return await original_generate(query, evidence)
        app.state.services.llm.generate = formatted_generation
        sys.path.append(str(ROOT/'.runtime-deps'))
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(channel='chrome',headless=True)
            context = await browser.new_context(viewport={'width':1440,'height':1000})
            page = await context.new_page()
            page.on('pageerror',lambda error:errors.append(str(error)))
            origin = f'http://127.0.0.1:{port}'
            # Keep this verification strictly local even if future UI code adds requests.
            await context.route('**/*',lambda route: route.continue_() if route.request.url.startswith(origin+'/') else route.abort())
            await page.goto(origin,wait_until='networkidle')
            await page.wait_for_function("document.getElementById('visitorLabel').textContent.startsWith('访客 ')")
            assert not await page.locator('#researchDesk').is_visible()
            assert not await page.locator('#caseSelect').is_visible()
            assert await page.locator('#historySidebar').is_visible()
            assert not await page.locator('#timelineSidebar').is_visible()
            assert await page.locator('#toggleTimeline').get_attribute('aria-expanded') == 'false'
            user_cookie = next(c for c in await context.cookies() if c['name']=='hd_user')
            assert user_cookie['httpOnly'] and user_cookie['sameSite']=='Strict'
            assert 'hd_user=' not in await page.evaluate('document.cookie')
            await page.screenshot(path=str(output/'default-desktop.png'),full_page=True)
            checks.append('default chat, HttpOnly identity, research hidden')

            await page.locator('#brandLogo').click(click_count=4,delay=80)
            assert not await page.locator('#researchDesk').is_visible()
            await page.wait_for_timeout(3100)
            await page.locator('#brandLogo').click()
            assert not await page.locator('#researchDesk').is_visible()
            await page.locator('#viewTitle').click()  # break the consecutive gesture
            await page.locator('#brandLogo').click(click_count=5,delay=80)
            assert await page.locator('#researchDesk').is_visible()
            await page.screenshot(path=str(output/'research-desktop.png'),full_page=True)
            research_before = next(c['value'] for c in await context.cookies() if c['name']=='hd_research')
            await page.reload(wait_until='networkidle')
            assert not await page.locator('#researchDesk').is_visible()
            assert next(c['value'] for c in await context.cookies() if c['name']=='hd_research') == research_before
            checks.append('five clicks within 3 seconds; timeout and reload stay hidden')

            async def send(question):
                await page.locator('#networkMode').uncheck()
                await page.locator('#messageInput').fill(question)
                await page.locator('#sendChat').click()
                await page.wait_for_function("document.getElementById('requestState').textContent==='本轮完成'",timeout=30000)

            await send('房颤的常见检查有哪些？')
            assert await page.locator('.inline-citations').count()==1
            assert not await page.locator('.inline-citations').first.locator('.inline-citation').first.is_visible()
            assert await page.locator('.citation-marker').count() >= 1
            assert await page.locator('.message.assistant > p strong').all_text_contents() == ['常见检查','心电图']
            assert await page.locator('.message.assistant > p sup').first.inner_text() == '1、2'
            formatting = await page.evaluate('''() => {
                const el=document.createElement('p');
                appendAnswerText(el,'** 粗体 ** [1] [2] [12] `<b>代码</b>` <img src=x onerror=alert(1)>');
                return {bold:el.querySelector('strong').textContent,refs:el.querySelector('sup').textContent,
                    html:el.querySelectorAll('img,b,script').length,code:el.querySelector('code').textContent};
            }''')
            assert formatting == {'bold':'粗体','refs':'1、2、12','html':0,'code':'<b>代码</b>'}
            user = await page.locator('.message.user').first.bounding_box()
            messages = await page.locator('#messages').bounding_box()
            assert abs(user['x']+user['width']-messages['x']-messages['width']) <= 2
            assert user['width'] < messages['width'] * .88
            input_box = await page.locator('#messageInput').bounding_box()
            composer = await page.locator('#chatForm').bounding_box()
            assert input_box['height'] <= 44 and composer['height'] < 165
            await page.locator('#messageInput').fill('\n'.join(['多行输入测试']*12))
            expanded_input = await page.locator('#messageInput').bounding_box()
            assert input_box['height'] < expanded_input['height'] <= 133
            await page.locator('#messageInput').fill('')
            assert (await page.locator('#messageInput').bounding_box())['height'] <= 44
            before = await page.locator('.chat-panel').bounding_box()
            await page.locator('#toggleTimeline').click()
            assert await page.locator('#timelineSidebar').is_visible()
            assert (await page.locator('.chat-panel').bounding_box())['width'] < before['width']
            assert await page.locator('#toggleTimeline').get_attribute('aria-expanded') == 'true'
            await page.screenshot(path=str(output/'timeline-desktop.png'),full_page=True)
            await page.locator('#hideTimeline').click()
            assert not await page.locator('#timelineSidebar').is_visible()
            assert (await page.locator('.chat-panel').bounding_box())['width'] == before['width']
            checks.append('safe bold including spaced markers, separated superscripts, flush-right user bubbles')
            checks.append('entire timeline column toggles, compact composer grows then shrinks')
            await page.locator('.inline-citations summary').first.click()
            assert await page.locator('.inline-citation').first.is_visible()
            assert '房颤检查示例资料' in await page.locator('.inline-citation-list').first.inner_text()
            assert await page.locator('.history-item').count()==1
            assert await page.locator('#conversationEvents li').count()==2
            first_session=await page.evaluate('sessionId')
            await page.locator('#newSession').click()
            await send('运动的一般注意事项有哪些？')
            assert await page.locator('.history-item').count()==2
            await page.locator('.history-item').filter(has_text='房颤').click()
            await page.wait_for_function('expected => sessionId===expected',arg=first_session)
            assert '房颤' in await page.locator('#messages').inner_text()
            await page.reload(wait_until='networkidle')
            await page.wait_for_function('conversation.length===2')
            assert await page.locator('.history-item').count()==2
            await page.locator('.history-item').filter(has_text='房颤').click()
            await page.wait_for_function('expected => sessionId===expected',arg=first_session)
            await page.screenshot(path=str(output/'conversation-desktop.png'),full_page=True)
            checks.append('two persistent conversations, switch, reload, timestamps')

            await page.locator('#toggleHistory').click()
            assert not await page.locator('#historySidebar').is_visible()
            await page.locator('#toggleHistory').click()
            assert await page.locator('#historySidebar').is_visible()
            await page.set_viewport_size({'width':390,'height':844})
            assert not await page.locator('#historySidebar').is_visible()
            assert not await page.evaluate('document.documentElement.scrollWidth > innerWidth')
            user = await page.locator('.message.user').first.bounding_box()
            messages = await page.locator('#messages').bounding_box()
            assert abs(user['x']+user['width']-messages['x']-messages['width']) <= 2
            await page.screenshot(path=str(output/'conversation-mobile.png'),full_page=True)
            await page.locator('#toggleTimeline').click()
            assert await page.locator('#timelineSidebar').is_visible()
            assert not await page.evaluate('document.documentElement.scrollWidth > innerWidth')
            await page.locator('#hideTimeline').click()
            await page.set_viewport_size({'width':320,'height':740})
            assert not await page.evaluate('document.documentElement.scrollWidth > innerWidth')
            await page.screenshot(path=str(output/'conversation-narrow.png'),full_page=True)
            await page.set_viewport_size({'width':390,'height':844})
            await page.locator('#toggleHistory').click()
            assert await page.locator('#historySidebar').is_visible()
            await page.screenshot(path=str(output/'history-mobile.png'),full_page=True)
            await page.keyboard.press('Escape')
            assert not await page.locator('#historySidebar').is_visible()
            checks.append('collapsible desktop sidebar, mobile drawer/Escape, no horizontal overflow')

            await page.set_viewport_size({'width':1440,'height':1000})
            await page.locator('#brandLogo').click(click_count=5,delay=80)
            await page.locator('#clearResearchCookies').click()
            assert all(c['name']!='hd_research' for c in await context.cookies())
            assert next(c['value'] for c in await context.cookies() if c['name']=='hd_user')==user_cookie['value']
            assert await page.locator('.history-item').count()==2
            await page.locator('#brandLogo').click(click_count=5,delay=80)
            await page.locator('[data-view="cases"]').click()
            research_before=next(c['value'] for c in await context.cookies() if c['name']=='hd_research')
            sibling=await context.new_page();await sibling.goto(origin,wait_until='networkidle')
            await sibling.wait_for_function('conversation.length===2')
            assert await sibling.locator('.history-item').count()==2
            page.once('dialog',lambda dialog:dialog.accept())
            await page.locator('#clearUserCookies').click()
            await page.wait_for_function("document.getElementById('pageNotice').textContent.includes('已建立新的匿名身份')")
            assert await page.locator('.history-item').count()==0
            cookies={c['name']:c['value'] for c in await context.cookies()}
            assert cookies['hd_user']!=user_cookie['value'] and cookies['hd_research']==research_before
            await sibling.wait_for_function("document.getElementById('pageNotice').textContent.includes('其他页面清除')")
            assert await sibling.locator('#messages .message').count()==0
            await sibling.close()
            checks.append('clearing either cookie group leaves the other group intact')
            checks.append('other open tab clears its stale UI after identity reset')

            other=await browser.new_context()
            other_page=await other.new_page();await other_page.goto(origin,wait_until='networkidle')
            await other_page.wait_for_function("document.getElementById('visitorLabel').textContent.startsWith('访客 ')")
            assert await other_page.locator('.history-item').count()==0
            assert next(c['value'] for c in await other.cookies() if c['name']=='hd_user')!=cookies['hd_user']
            checks.append('separate browser context gets separate identity and history')
            await other.close();await browser.close()
        assert not errors,errors
        report.update(checks=checks,browser_errors=errors,status='passed')
    except BaseException as exc:
        report.update(checks=checks,browser_errors=errors,status='failed',error=repr(exc))
        raise
    finally:
        server.should_exit=True
        await asyncio.wait_for(running,15)
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(str(output/'report.json'))


if __name__=='__main__':asyncio.run(main())
