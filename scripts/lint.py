"""Dependency-free semantic lint: invalid control flow, tuple asserts, duplicate literal keys."""
import ast
from pathlib import Path


def main():
    errors = []
    files = [p for root in ('src','scripts','tests','migrations') for p in Path(root).rglob('*.py')]
    for path in files:
        tree = ast.parse(path.read_text(encoding='utf-8-sig'),filename=str(path))
        compile(tree,str(path),'exec')  # invalid return/break/continue/nonlocal, etc.
        for node in ast.walk(tree):
            if isinstance(node,ast.Assert) and isinstance(node.test,ast.Tuple):
                errors.append(f'{path}:{node.lineno}: assertion on a tuple is always true')
            if isinstance(node,ast.Dict):
                keys = [k.value for k in node.keys if isinstance(k,ast.Constant)]
                if len(keys)!=len(set(keys)):
                    errors.append(f'{path}:{node.lineno}: duplicate literal dictionary key')
    if errors: raise SystemExit('\n'.join(errors))
    print(f'Semantic lint passed: {len(files)} Python files')


if __name__=='__main__': main()
