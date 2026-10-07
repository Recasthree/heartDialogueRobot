"""One-shot local completion step for a long-running CPU index build.

No model API requests. Publishes honest pending/failed/completed status, evaluates
the activated index, and optionally starts the local UI. Does not stop processes.
"""
import argparse
import asyncio
import ctypes
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

from app.config import ROOT, Settings
from app.main import create_app
from app.retrieval.index_manager import read_active


def is_running(pid):
    if os.name != 'nt':
        try:
            os.kill(pid,0)
            return True
        except ProcessLookupError:
            return False
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong]
    kernel.OpenProcess.restype=ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_ulong]
    kernel.CloseHandle.argtypes=[ctypes.c_void_p]
    handle=kernel.OpenProcess(0x100000,False,pid)
    if not handle:
        if ctypes.get_last_error()==5:
            raise PermissionError('cannot inspect build process')
        return False
    try:
        return kernel.WaitForSingleObject(handle,0)==258
    finally:
        kernel.CloseHandle(handle)


def build_counts(database):
    if not database.exists():
        return {}
    db=sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
    try:
        # Count indexed keys only. Scanning every JSON/vector page can block the
        # writer in SQLite's rollback-journal mode during a large CPU build.
        count=db.execute('SELECT count(*) FROM documents').fetchone()[0]
        encoded=db.execute('SELECT count(*) FROM vector_cache').fetchone()[0]
        return {'persisted_records':count,'encoded_unique_chunks':encoded}
    finally:
        db.close()


async def run(args):
    if not re.fullmatch(r'index-[\w-]+\.sqlite3',args.database):
        raise ValueError('expected a build database filename')
    cfg=Settings.from_env()
    folder=Path(cfg.data_dir)/'verification'; folder.mkdir(parents=True,exist_ok=True)
    path=folder/'delivery-status.json'
    database=Path(cfg.data_dir)/'indexes'/args.database
    def save(status,**values):
        payload={'status':status,'build_pid':args.build_pid,'database':args.database,
                 'updated_at':time.strftime('%Y-%m-%d %H:%M:%S'),'model_api_calls':0,**values}
        temp=path.with_suffix('.tmp')
        temp.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
        temp.replace(path)
    try:
        while True:
            active=read_active(cfg)
            if active and active['database']==args.database:
                break
            if not is_running(args.build_pid):
                raise RuntimeError('build_stopped_without_activating_expected_index')
            try:
                counts=build_counts(database)
            except sqlite3.OperationalError:
                counts={'progress':'database_temporarily_busy'}
            save('building',total_target_records=args.total,**counts)
            await asyncio.sleep(30)
        save('verifying',index_version=active['index_version'])
        # Explicit Mock: final evaluation can never consume API credit.
        app=create_app(replace(cfg,llm_backend='mock',deepseek_api_key=''))
        started=time.perf_counter()
        async with app.router.lifespan_context(app):
            services=app.state.services
            load_seconds=time.perf_counter()-started
            if services.warm_task:
                await services.warm_task
            report=await services.runner.run('ragcare','retrieval_only',32,[],False)
            if report['model_calls']!=0:
                raise AssertionError('retrieval evaluation must not call models')
            metrics={k:v for k,v in report['suites']['ragcare'].items() if k!='results'}
        server=None
        if args.serve:
            log=(folder/'post-build-start.log').open('a',encoding='utf-8')
            try:
                result=subprocess.run(['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',
                                       str(ROOT/'scripts/start_app.ps1'),'-Python',sys.executable],
                                      cwd=ROOT,stdout=log,stderr=log,check=True,
                                      creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                server='http://127.0.0.1:8000'
            finally:
                log.close()
        save('completed',index_version=active['index_version'],index_build=active['build'],
             documents=active['documents'],load_seconds=round(load_seconds,3),
             evaluation_run_id=report['run_id'],retrieval_metrics=metrics,
             server=server,server_state='start_requested' if server else 'not_started')
    except BaseException as exc:
        save('failed',error=type(exc).__name__,reason=str(exc) if isinstance(exc,(RuntimeError,AssertionError)) else 'see local log')
        raise


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--build-pid',type=int,required=True)
    parser.add_argument('--database',required=True)
    parser.add_argument('--total',type=int,required=True)
    parser.add_argument('--serve',action='store_true')
    asyncio.run(run(parser.parse_args()))
