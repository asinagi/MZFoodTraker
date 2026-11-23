"""Local frontend + read-only B8 bridge. No API credentials, DB writes or public binding."""
import argparse
import json
import re
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent / "public"
API_PATH = re.compile(r'^/api/v1/(health|foods(?:/[A-Za-z0-9_-]+(?:/(?:timeline|metrics))?)?|sources/status)$')

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def error_json(self, status, message):
        data = json.dumps({'error': {'message': message}}, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def allowed_host(self):
        return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}

    def do_GET(self):
        if not self.allowed_host():
            return self.error_json(403, 'Local host required')
        origin = self.headers.get('Origin')
        if origin and origin not in {f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'}:
            return self.error_json(403, 'Same-origin requests only')
        parsed = urlsplit(self.path)
        if parsed.path.startswith('/api/'):
            if not API_PATH.fullmatch(parsed.path) or len(self.path) > 8192:
                return self.error_json(404, 'Unsupported read endpoint')
            if self.server.api_port is None:
                return self.error_json(503, 'B8 연결이 설정되지 않았습니다. --api-port 8030 또는 8020으로 실행하세요.')
            target = f'http://127.0.0.1:{self.server.api_port}' + self.path
            opener = build_opener(ProxyHandler({}), NoRedirect())
            try:
                with opener.open(Request(target, headers={'Accept': 'application/json'}), timeout=8) as response:
                    data = response.read(4 * 1024 * 1024 + 1)
                    if len(data) > 4 * 1024 * 1024:
                        return self.error_json(502, 'Response exceeds 4 MB')
                    json.loads(data)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json; charset=utf-8')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
            except HTTPError as error:
                self.error_json(error.code if 400 <= error.code < 600 else 502, f'B8 응답 오류 {error.code}')
            except (URLError, TimeoutError, ValueError):
                self.error_json(502, 'B8 서버에 연결하지 못했습니다. 같은 PC의 API 실행 상태를 확인하세요.')
            return
        path = Path(self.translate_path(parsed.path)).resolve()
        if not path.is_relative_to(ROOT) or any(p.startswith('.') for p in path.relative_to(ROOT).parts):
            return self.error_json(404, 'Not found')
        if path.is_dir() and path != ROOT:
            return self.error_json(404, 'Directory listing disabled')
        if path.suffix not in {'', '.html', '.js', '.css', '.json', '.png', '.svg', '.woff2'}:
            return self.error_json(404, 'Not found')
        super().do_GET()

    def do_POST(self):
        self.error_json(405, 'Read-only server')

    do_HEAD = do_POST
    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8088)
    parser.add_argument('--api-port', type=int, choices=[8020, 8030])
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.api_port = args.api_port
    print(f'MZ web: http://127.0.0.1:{args.port} | B8 port: {args.api_port or "not connected"}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
