"""Loopback-only Guardian console. Controls are queued for the supervisor lock."""
import hmac
import http.server
import json
import os
import time
import secrets
import threading
from pathlib import Path


def start(directory, port, guard_type, atomic):
    token = secrets.token_urlsafe(32)
    origin = f'http://127.0.0.1:{port}'
    root = Path(__file__).resolve().parent

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def reply(self, code, value, html=False, headers=()):
            body = value if html else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'text/html; charset=utf-8' if html else 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def same_site(self):
            return (self.headers.get('Host') == f'127.0.0.1:{port}' and
                    self.headers.get('Sec-Fetch-Site', 'none') in ('none', 'same-origin') and
                    self.headers.get('Origin', origin) == origin)

        def do_GET(self):
            navigation = (self.path == '/' and self.headers.get('Host') == f'127.0.0.1:{port}' and
                          self.headers.get('Sec-Fetch-Mode') == 'navigate' and self.headers.get('Sec-Fetch-Dest') == 'document')
            if not self.same_site() and not navigation:
                return self.reply(403, {'error': '仅允许从本机控制台访问。'})
            if self.path == '/':
                return self.reply(200, (root / 'ui/index.html').read_bytes(), html=True)
            if self.path == '/open-dsh':
                try:
                    url = guard_type(directory).application_url()
                except (ValueError, OSError, RuntimeError):
                    url = None
                if url:
                    return self.reply(303, {}, headers=(('Location', url),))
                return self.reply(503, ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1">'
                    '<title>DSH 暂不可用</title><style>body{font:16px/1.8 system-ui;max-width:36rem;'
                    'margin:12vh auto;padding:24px;color-scheme:light dark}a{color:inherit}</style>'
                    '<h1>DSH 暂不可用</h1><p>尚未取得受管服务的认证入口。'
                    '请返回控制台检查启动状态，或恢复管理后重试。</p><a href="/">返回控制台</a></html>').encode(), html=True)
            if self.path == '/api/status':
                try:
                    value = guard_type(directory).status()
                    value['control_token'] = token
                    return self.reply(200, value)
                except (ValueError, OSError, RuntimeError):
                    return self.reply(503, {'error': '状态暂不可读，正在重试。'})
            self.reply(404, {'error': '页面不存在。'})

        def do_POST(self):
            if (not self.same_site() or self.headers.get('Origin') != origin or
                    not hmac.compare_digest(self.headers.get('X-Guard-Token', ''), token)):
                return self.reply(403, {'error': '操作校验失败，请刷新页面。'})
            if self.path != '/api/control':
                return self.reply(404, {'error': '操作不存在。'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 512 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError()
                value = json.loads(self.rfile.read(length))
                allowed = ('stop', 'resume', 'restart', 'checkpoint', 'return-development')
                if not isinstance(value, dict) or set(value) != {'action'} or value['action'] not in allowed:
                    raise ValueError()
            except (ValueError, OSError):
                return self.reply(400, {'error': '操作参数无效。'})
            command = Path(directory) / 'control.json'
            request_id = secrets.token_hex(12)
            if value['action'] == 'stop':
                # Stop must interrupt a long validation/model operation even when its queue is occupied.
                atomic(Path(directory) / 'manual-stop', {'requested_by': 'console', 'requested': time.time(), 'request_id': request_id})
                return self.reply(202, {'accepted': True, 'request_id': request_id, 'message': '停止请求已提交，正在结束受管进程。'})
            stage = Path(directory) / ('control-' + request_id + '.json')
            try:
                atomic(stage, {**value, 'request_id': request_id, 'queued_at': time.time()})
                # Atomic exclusive publication: the main loop can never read a partial command.
                os.link(stage, command)
            except FileExistsError:
                return self.reply(409, {'error': '已有操作正在等待执行，请稍候。'})
            finally:
                stage.unlink(missing_ok=True)
            self.reply(202, {'accepted': True, 'request_id': request_id, 'message': '操作已提交，状态会自动更新。'})

    server = http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name='guard-console', daemon=True)
    thread.start()
    return server
