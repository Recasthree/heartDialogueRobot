"""Atomic lightweight index generations with per-request leases across MCP calls."""
import asyncio
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from pathlib import Path
from .index_manager import load_index

PINNED = ContextVar('retrieval_generation',default=None)


class LiveRetriever:
    def __init__(self,cfg,initial):
        self.cfg,self.current=cfg,initial
        self.lock=asyncio.Lock(); self.readers={}; self.retired={}
        self.path=Path(cfg.data_dir)/'indexes/active.json'
        self.stamp=self.path.stat().st_mtime_ns if self.path.exists() else None
        if getattr(initial,'index_error',None): self.stamp=None
        self.reload_error=None

    def capture(self):
        pinned=PINNED.get()
        return pinned[1] if pinned and pinned[0] is self else self.current

    def __getattr__(self,name): return getattr(self.capture(),name)

    @contextmanager
    def use(self,handle):
        token=PINNED.set((self,handle))
        try: yield
        finally: PINNED.reset(token)

    async def refresh(self):
        stamp=self.path.stat().st_mtime_ns if self.path.exists() else None
        if stamp==self.stamp: return
        async with self.lock:
            if stamp==self.stamp: return
            try: new=await asyncio.to_thread(load_index,self.cfg,self.current.reranker)
            except (ValueError,RuntimeError,OSError,sqlite3.Error) as exc:
                self.reload_error=type(exc).__name__; return
            old=self.current
            self.current,self.stamp,self.reload_error=new,stamp,None
            self.retired[id(old)]=old
            await self._collect()

    async def _collect(self):
        for key,handle in list(self.retired.items()):
            if not self.readers.get(key):
                if handle.vector_store: await handle.vector_store.close()
                self.retired.pop(key,None)

    @asynccontextmanager
    async def lease(self):
        pinned=PINNED.get()
        if pinned and pinned[0] is self:
            yield pinned[1]; return
        await self.refresh()
        handle=self.current; key=id(handle)
        self.readers[key]=self.readers.get(key,0)+1
        try:
            with self.use(handle): yield handle
        finally:
            self.readers[key]-=1
            await self._collect()

    async def close(self):
        if self.current.vector_store: await self.current.vector_store.close()
        for handle in self.retired.values():
            if handle.vector_store: await handle.vector_store.close()
