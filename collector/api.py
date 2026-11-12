import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import resolution
from .storage import Store


def serve(database, port):
    Path(database).parent.mkdir(parents=True, exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format_string, *arguments):
            return None

        def reply(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            segments = [unquote(part) for part in urlparse(self.path).path.split('/') if part]
            store = Store(database)
            try:
                if segments == ['records']:
                    self.reply(200, store.records())
                elif segments == ['entities']:
                    self.reply(200, resolution.list_entities(store))
                elif len(segments) == 2 and segments[0] == 'entities':
                    entity = store.entity(segments[1])
                    if entity is None:
                        self.reply(404, {'error': '实体不存在'})
                    else:
                        self.reply(200, resolution.entity_view(store, entity))
                elif len(segments) == 3 and segments[0] == 'entities' and segments[2] == 'events':
                    self.reply(200, store.entity_events(segments[1]))
                elif segments == ['candidates']:
                    self.reply(200, resolution.explain_candidates(store))
                elif len(segments) == 3 and segments[0] == 'records' and segments[2] == 'history':
                    self.reply(200, resolution.record_history(store, segments[1]))
                elif segments == ['integrity']:
                    self.reply(200, resolution.integrity_check(store))
                else:
                    self.reply(404, {'error': '资源不存在'})
            finally:
                store.close()

        def do_POST(self):
            segments = [unquote(part) for part in urlparse(self.path).path.split('/') if part]
            length = int(self.headers.get('Content-Length', '0'))
            try:
                decision = json.loads(self.rfile.read(length) or b'{}')
            except json.JSONDecodeError:
                self.reply(400, {'error': '请求体不是有效 JSON'})
                return
            if segments != ['decisions']:
                self.reply(404, {'error': '资源不存在'})
                return
            store = Store(database)
            try:
                self.reply(200, resolution.submit_decision(store, decision))
            except resolution.VersionConflict as conflict:
                self.reply(409, {'error': '版本冲突：实体已被自动更新改变，请重新获取后再决定',
                                 'current': conflict.current})
            except (ValueError, KeyError) as error:
                self.reply(400, {'error': str(error)})
            finally:
                store.close()

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'采集记录服务启动于 127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
