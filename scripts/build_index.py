"""python -m scripts.build_index --download-model (explicit network download)."""
import argparse
import asyncio
import json
import os
from pathlib import Path
from app.config import Settings
from app.retrieval.index_manager import build_index


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--download-model', action='store_true')
    parser.add_argument('--limit', type=int, help='Smoke build only; does not activate or replace the full index')
    parser.add_argument('--stage', choices=['lexical','vectors','all'], default='all')
    parser.add_argument('--resume', help='Continue an existing immutable-source build job')
    parser.add_argument('--recover-from', help='Read-only legacy SQLite cache to verify and import')
    parser.add_argument('--activate', action='store_true', help='Publish each fully verified stage')
    parser.add_argument('--incremental', action='store_true', help='Seed a new version from the compatible active index; only rechunk changed sources')
    args = parser.parse_args()
    cfg = Settings.from_env()
    scratch = Path(cfg.data_dir)/'build-tmp'
    scratch.mkdir(parents=True,exist_ok=True)
    os.environ['TEMP'] = os.environ['TMP'] = str(scratch.resolve())
    result = asyncio.run(build_index(cfg, args.download_model, args.limit,
        stage=args.stage,resume=args.resume,recover_from=args.recover_from,activate=args.activate,incremental=args.incremental))
    print(json.dumps({k: v for k, v in result.items() if k != 'sources'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
