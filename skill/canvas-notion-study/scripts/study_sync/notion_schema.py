"""Translate a plan's schemas to the connected Notion tool's DDL format."""
from __future__ import annotations

import json
from typing import Mapping

PERSONAL_FIELDS = {
    "tasks": {"Done":"CHECKBOX", "Planned":"DATE", "Priority":"SELECT('High':red, 'Normal':blue, 'Low':gray)", "Personal Notes":"RICH_TEXT"},
    "notes": {"Personal Notes":"RICH_TEXT"},
}


def _quote_identifier(value):
    return '"'+str(value).replace('"','""')+'"'


def _quote_text(value):
    return "'"+str(value).replace("'","''")+"'"


def database_definition(plan: Mapping, kind: str, bindings: Mapping | None = None, *, include_personal: bool = True) -> dict:
    schema=plan["databases"][kind]
    bindings=bindings or {}
    fields=[]
    values_by_name={}
    for record in plan.get("records",[]):
        if record["kind"]!=kind:
            continue
        for name,value in record.get("properties",{}).items():
            if value is None:
                continue
            values=value if isinstance(value,list) else [value]
            values_by_name.setdefault(name,set()).update(str(v) for v in values)
    for name,spec in schema.get("properties",{}).items():
        type_=spec.get("type","text")
        if type_=="relation":
            target=spec.get("target") or spec.get("target_kind") or ("courses" if name=="Course" else "resources" if name=="Resources" else None)
            if not target or target not in bindings:
                raise ValueError(f"Relation {kind}.{name} needs a bound target database.")
            ddl=f"RELATION({_quote_text(bindings[target]['data_source_id'])})"
        elif type_ in ("select","multi_select"):
            values=sorted(values_by_name.get(name,set()) | set(spec.get("options",[]))) or ["Other"]
            options=", ".join(_quote_text(v)+":default" for v in values)
            ddl=("MULTI_SELECT" if type_=="multi_select" else "SELECT")+"("+options+")"
        else:
            ddl={"title":"TITLE","text":"RICH_TEXT","rich_text":"RICH_TEXT","url":"URL","date":"DATE","checkbox":"CHECKBOX","number":"NUMBER","files":"FILES"}.get(type_)
            if not ddl:
                raise ValueError(f"Unsupported Notion schema type: {type_}")
        fields.append(_quote_identifier(name)+" "+ddl)
    for name,ddl in (PERSONAL_FIELDS.get(kind,{}) if include_personal else {}).items():
        if name not in schema.get("properties",{}):
            fields.append(_quote_identifier(name)+" "+ddl)
    return {"title":schema.get("name",kind.title()),
            "description":schema.get("description",""), "schema":"CREATE TABLE ("+", ".join(fields)+")"}


def schema_updates(plan: Mapping, kind: str, remote_state: Mapping, bindings: Mapping | None = None) -> dict:
    """Add missing properties and enum choices without replacing existing columns."""
    remote=remote_state.get('schema',remote_state)
    statements=[]
    for name,spec in plan['databases'][kind]['properties'].items():
        type_=spec.get('type')
        actual=remote.get(name)
        if actual is None:
            partial={'databases':{kind:{'properties':{name:spec}}},'records':plan.get('records',[])}
            ddl=database_definition(partial,kind,bindings,include_personal=False)['schema'][len('CREATE TABLE ('):-1]
            statements.append('ADD COLUMN '+ddl)
            continue
        if type_ not in ('select','multi_select'):
            canonical=lambda t: 'text' if t=='rich_text' else t
            if canonical(actual.get('type')) != canonical(type_):
                raise ValueError(f'Fetch and reconcile schema type for {kind}.{name}.')
            continue
        if not actual or actual.get('type')!=type_:
            raise ValueError(f'Fetch and reconcile the schema for {kind}.{name}; automatic type conversion is disabled.')
        options={o['name']:o.get('color','default') for o in actual.get('options',[])}
        values=set(spec.get('options',[]))
        for record in plan.get('records',[]):
            if record['kind']!=kind:
                continue
            value=record.get('properties',{}).get(name)
            values.update(str(v) for v in (value if isinstance(value,list) else [value]) if v is not None and v!='')
        missing=values-set(options)
        if not missing:
            continue
        options.update({value:'default' for value in sorted(missing)})
        type_name='MULTI_SELECT' if type_=='multi_select' else 'SELECT'
        definition=type_name+'('+', '.join(_quote_text(value)+':'+color for value,color in options.items())+')'
        statements.append('ALTER COLUMN '+_quote_identifier(name)+' SET '+definition)
    return {'statements':'; '.join(statements),'changes':len(statements)}
