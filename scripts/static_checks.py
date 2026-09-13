"""Small mandatory release checks without changing legacy formatting rules."""
import ast
import json
from pathlib import Path


def main():
    for root in ('src','scripts','migrations','tests'):
        for path in Path(root).rglob('*.py'):
            ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
    from credit_harness.api.ui import create_production_ui_app, create_ui_app
    for name, factory in [('openapi.json',create_production_ui_app),('openapi-legacy.json',create_ui_app)]:
        actual = json.loads((Path('frontend')/name).read_text(encoding='utf-8'))
        assert actual == factory({}).openapi(), f'{name} drift; regenerate contracts'
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from credit_harness.production.schema import HEAD
    assert ScriptDirectory.from_config(Config('alembic.ini')).get_heads() == [HEAD]
    assert 'create_all' not in Path('src/credit_harness/production/app.py').read_text()
    print('Python AST, production/legacy OpenAPI, migration head and startup checks passed')


if __name__ == '__main__': main()
