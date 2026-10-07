"""Run measured suites through the same application lifecycle as HTTP requests."""
import argparse
import asyncio
import json
from app.config import Settings
from app.main import create_app


async def run(args):
    app = create_app(Settings.from_env())
    async with app.router.lifespan_context(app):
        report = await app.state.services.runner.run(args.suite, args.mode, args.max_cases, [], args.judge)
        print(json.dumps({k:v for k,v in report.items() if k not in {'suites','results'}},ensure_ascii=False,indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', choices=['ragcare','safety','judge','all'],default='ragcare')
    parser.add_argument('--mode', choices=['retrieval_only','full'],default='retrieval_only')
    parser.add_argument('--max-cases',type=int,default=32)
    parser.add_argument('--judge',action='store_true')
    asyncio.run(run(parser.parse_args()))
