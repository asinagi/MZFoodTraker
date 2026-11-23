import importlib.util
import json
from pathlib import Path
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError

spec = importlib.util.spec_from_file_location('serve', Path(__file__).resolve().parents[1] / 'serve.py')
serve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(serve)

class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = serve.ThreadingHTTPServer(('127.0.0.1', 0), serve.Handler)
        cls.server.api_port = None
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
    def get(self, path, headers=None, method='GET'):
        request=Request(self.url+path,headers=headers or {},method=method)
        try:
            with urlopen(request,timeout=2) as r:return r.status,r.read()
        except HTTPError as e:return e.code,e.read()
    def test_index(self):
        status,body=self.get('/')
        self.assertEqual(status,200)
        self.assertIn(b'integration.js',body)
    def test_brand_svg(self):
        with urlopen(self.url+'/assets/mz-mark.svg', timeout=2) as response:
            self.assertEqual(response.status,200)
            self.assertEqual(response.headers.get_content_type(),'image/svg+xml')
            self.assertIn(b'<svg',response.read())
    def test_js(self):self.assertEqual(self.get('/integration.js')[0],200)
    def test_disabled_api(self):self.assertEqual(self.get('/api/v1/health')[0],503)
    def test_admin_blocked(self):self.assertEqual(self.get('/api/collect')[0],404)
    def test_write_blocked(self):self.assertEqual(self.get('/api/v1/foods',method='POST')[0],405)
    def test_foreign_origin(self):self.assertEqual(self.get('/api/v1/health',{'Origin':'https://evil.test'})[0],403)
    def test_foreign_host(self):self.assertEqual(self.get('/',{'Host':'evil.test'})[0],403)
    def test_no_listing(self):self.assertEqual(self.get('/fixtures/')[0],404)
    def test_hidden_file(self):self.assertEqual(self.get('/.env')[0],404)
    def test_no_source(self):self.assertEqual(self.get('/../serve.py')[0],404)

if __name__=='__main__':unittest.main()
