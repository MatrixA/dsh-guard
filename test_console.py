import http.client
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('guard_console', Path(__file__).with_name('console.py'))
console = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console)


class ConsoleTests(unittest.TestCase):
    def test_native_application_redirect_requires_same_origin_and_hides_token_from_status(self):
        class Guard:
            ready = True

            def __init__(self, _path):
                pass

            def status(self):
                return {'health': 'ready'}

            def application_url(self):
                return 'http://127.0.0.1:3080/?token=fixture_private' if self.ready else None

        with tempfile.TemporaryDirectory() as directory:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            server = console.start(directory, port, Guard, lambda *_: None)
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
            try:
                conn.request('GET', '/open-dsh')
                response = conn.getresponse()
                self.assertEqual(response.status, 303)
                self.assertEqual(response.getheader('Location'), Guard(None).application_url())
                self.assertEqual(response.getheader('Cache-Control'), 'no-store')
                self.assertEqual(response.getheader('Referrer-Policy'), 'no-referrer')
                self.assertNotIn(b'fixture_private', response.read())
                conn.request('GET', '/api/status')
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertNotIn(b'fixture_private', response.read())
                for headers in [{'Sec-Fetch-Site': 'cross-site'}, {'Sec-Fetch-Site': 'same-site'}, {'Host': 'evil.example'}]:
                    conn.request('GET', '/open-dsh', headers=headers)
                    response = conn.getresponse()
                    self.assertEqual(response.status, 403)
                    self.assertIsNone(response.getheader('Location'))
                    response.read()
                Guard.ready = False
                conn.request('GET', '/open-dsh')
                response = conn.getresponse()
                self.assertEqual(response.status, 503)
                self.assertIsNone(response.getheader('Location'))
                self.assertIn('返回控制台', response.read().decode())
            finally:
                conn.close()
                server.shutdown()
                server.server_close()

    def test_same_origin_token_queue_and_no_duplicate_action(self):
        class Guard:
            def __init__(self, _path):
                pass

            def status(self):
                return {'health': 'ready'}

        def atomic(path, value):
            path.write_text(json.dumps(value))

        with tempfile.TemporaryDirectory() as directory:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            server = console.start(directory, port, Guard, atomic)
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
            try:
                conn.request('GET', '/api/status')
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                token = json.loads(response.read())['control_token']
                navigation = {'Sec-Fetch-Site': 'same-site', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}
                conn.request('GET', '/', headers=navigation)
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                conn.request('GET', '/api/status', headers=navigation)
                response = conn.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
                for host, origin, supplied, expected in [
                    ('evil.example', f'http://127.0.0.1:{port}', token, 403),
                    (f'127.0.0.1:{port}', 'https://evil.example', token, 403),
                    (f'127.0.0.1:{port}', f'http://127.0.0.1:{port}', 'bad', 403),
                    (f'127.0.0.1:{port}', f'http://127.0.0.1:{port}', token, 202),
                    (f'127.0.0.1:{port}', f'http://127.0.0.1:{port}', token, 409),
                ]:
                    conn.request('POST', '/api/control', json.dumps({'action': 'checkpoint'}), headers={
                        'Host': host, 'Origin': origin, 'X-Guard-Token': supplied,
                        'Content-Type': 'application/json'})
                    response = conn.getresponse()
                    self.assertEqual(response.status, expected)
                    response.read()
                conn.request('POST', '/api/control', json.dumps({'action': 'stop'}), headers={
                    'Origin': f'http://127.0.0.1:{port}', 'X-Guard-Token': token, 'Content-Type': 'application/json'})
                response = conn.getresponse()
                self.assertEqual(response.status, 202)
                stopped = json.loads(response.read())
                marker = json.loads((Path(directory) / 'manual-stop').read_text())
                self.assertEqual(marker['request_id'], stopped['request_id'])
                queued = json.loads((Path(directory) / 'control.json').read_text())
                self.assertEqual(queued['action'], 'checkpoint')
                self.assertEqual(len(queued['request_id']), 24)
                self.assertEqual(list(Path(directory).glob('control-*.json')), [])
            finally:
                conn.close()
                server.shutdown()
                server.server_close()
