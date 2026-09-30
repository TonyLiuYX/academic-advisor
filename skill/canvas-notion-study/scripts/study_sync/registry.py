"""Non-secret instance discovery for future skill invocations."""
from __future__ import annotations
import os
from pathlib import Path
from .state import read_json,write_json


def default_registry() -> Path:
    codex_dir=Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')
    return codex_dir/'canvas-notion-study'/'instances.json'


def register_instance(config_path: str | Path, config: dict, registry_path: str | Path | None = None) -> dict:
    if not config.get('owner_id'):
        raise ValueError('Run probe first to record the verified Canvas identity.')
    path=Path(registry_path) if registry_path else default_registry()
    data=read_json(path,{'schema_version':1,'instances':{}})
    key=f"{config['base_url']}|{config['owner_id']}|{config['term']['key']}"
    data['instances'][key]={'config':str(Path(config_path).resolve()),'term':config['term']['label'],'canvas_origin':config['base_url'],'owner_id':config['owner_id'],'notion_root_page_id':config.get('notion',{}).get('root_page_id')}
    write_json(path,data)
    return {'registry':str(path),'instance_key':key}
