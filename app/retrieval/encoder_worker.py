"""Disposable CPU encoder process. Only public corpus text enters this protocol."""
import asyncio
from contextlib import redirect_stdout
import json
import sys
from ..providers import LocalSemanticEmbedding


async def main():
    config=json.loads(sys.argv[1])
    encoder=LocalSemanticEmbedding(**config)
    for line in sys.stdin:
        request=json.loads(line)
        try:
            with redirect_stdout(sys.stderr):
                values=await encoder.embed_documents(request['texts'])
            print(json.dumps({'vectors':values}),flush=True)
        except Exception as exc:
            print(json.dumps({'error':type(exc).__name__}),flush=True)


if __name__=='__main__': asyncio.run(main())
