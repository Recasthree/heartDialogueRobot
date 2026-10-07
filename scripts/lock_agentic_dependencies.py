"""Generate a dependency-closure lock, excluding unrelated shared packages."""
from importlib import metadata
from pathlib import Path
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def main():
    root=Path(__file__).resolve().parents[1]
    todo=[Requirement(line) for line in (root/'requirements-agentic.txt').read_text().splitlines() if line and not line.startswith('#')]
    resolved={}
    while todo:
        req=todo.pop()
        if req.marker and not req.marker.evaluate({'extra':''}):
            continue
        name=canonicalize_name(req.name)
        if name in resolved:
            continue
        dist=metadata.distribution(req.name)
        if not req.specifier.contains(dist.version):
            raise RuntimeError(f'{name}: installed {dist.version} does not satisfy {req.specifier}')
        resolved[name]=dist.version
        todo.extend(Requirement(r) for r in dist.requires or [])
    target=root/'requirements-agentic-lock.txt'
    target.write_text('# Validated Python 3.13 / Windows dependency closure.\n'+'\n'.join(f'{name}=={version}' for name,version in sorted(resolved.items()))+'\n',encoding='utf-8')
    print(f'Locked {len(resolved)} packages: {target.name}')


if __name__=='__main__': main()
