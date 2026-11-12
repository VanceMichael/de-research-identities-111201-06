import argparse
import json
from pathlib import Path
import tempfile

from . import resolution
from .domain import collect, collect_resource_feed
from .storage import Store
from .transport import Client, replay_server


def main():
    parser = argparse.ArgumentParser(description='本地采集与凭据查看')
    parser.add_argument('command', choices=['demo', 'import', 'show', 'serve', 'resolve', 'entities', 'decide'])
    parser.add_argument('--input', default='fixtures/sample.json')
    parser.add_argument('--db', default='var/records.sqlite3')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--decision', default=None, help='单条人工决定 JSON，如 {"type":"merge","sources":["a:7","b:2"],"version":1}')
    arguments = parser.parse_args()
    if arguments.command == 'serve':
        from .api import serve
        serve(arguments.db, arguments.port)
        return
    if arguments.command == 'demo':
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(arguments.input).read_bytes()
            with replay_server({'/source': [dict(body=payload.decode('utf-8'))]}) as (base, calls):
                status, headers, raw = Client().request(base + '/source')
                if status != 200:
                    raise RuntimeError('本地来源未返回成功响应')
                store = Store(Path(directory) / 'demo.sqlite3')
                rows = collect(json.loads(raw))
                digest = store.ingest('本地演示来源', raw, rows)
                print(json.dumps(dict(digest=digest, records=store.records(), requests=len(calls)), ensure_ascii=False, indent=2))
                store.close()
        return
    Path(arguments.db).parent.mkdir(parents=True, exist_ok=True)
    store = Store(arguments.db)
    try:
        if arguments.command == 'import':
            raw = Path(arguments.input).read_bytes()
            store.ingest(str(Path(arguments.input)), raw, collect(json.loads(raw)))
            print(json.dumps(store.records(), ensure_ascii=False, indent=2))
        elif arguments.command == 'show':
            print(json.dumps(store.records(), ensure_ascii=False, indent=2))
        elif arguments.command == 'resolve':
            raw = Path(arguments.input).read_bytes()
            records, aliases, decisions = collect_resource_feed(json.loads(raw))
            outcome = resolution.ingest_feed(store, str(Path(arguments.input)), raw, records, aliases)
            steps = []
            for decision in decisions:
                try:
                    result = resolution.submit_decision(store, decision)
                    steps.append(dict(decision=decision, applied=result['decision'], changes=result['changes']))
                except resolution.VersionConflict as conflict:
                    steps.append(dict(decision=decision, conflict=conflict.current))
            print(json.dumps(dict(
                ingest=dict(digest=outcome['digest'], skipped=outcome['skipped']),
                steps=steps,
                entities=resolution.list_entities(store),
                candidates=resolution.explain_candidates(store),
                events=store.entity_events(),
                integrity=resolution.integrity_check(store)), ensure_ascii=False, indent=2))
        elif arguments.command == 'entities':
            print(json.dumps(resolution.list_entities(store), ensure_ascii=False, indent=2))
        elif arguments.command == 'decide':
            if not arguments.decision:
                raise SystemExit('decide 命令需要 --decision 参数')
            result = resolution.submit_decision(store, json.loads(arguments.decision))
            print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == '__main__':
    main()
