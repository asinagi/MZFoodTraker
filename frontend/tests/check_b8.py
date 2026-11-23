"""Optional: python3 -B tests/check_b8.py /path/to/MZFoodTraker
Runs the official synthetic Mock API and a temporary loopback frontend bridge.
No production DB or external API is used.
"""
import importlib.util,json,subprocess,sys,threading,time
from pathlib import Path
from urllib.request import build_opener,ProxyHandler
from urllib.parse import urlencode
repo=Path(sys.argv[1]).resolve()
spec=importlib.util.spec_from_file_location('serve',Path(__file__).resolve().parents[1]/'serve.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
server=m.ThreadingHTTPServer(('127.0.0.1',0),m.Handler);server.api_port=8030
threading.Thread(target=server.serve_forever,daemon=True).start()
process=subprocess.Popen([sys.executable,'-B','-m','food_trends.app_mock'],cwd=repo,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
opener=build_opener(ProxyHandler({}))
def get(path):
 with opener.open(f'http://127.0.0.1:{server.server_port}'+path,timeout=10) as r:return json.load(r)
try:
 deadline=time.monotonic()+55
 while time.monotonic()<deadline:
  if process.poll() is not None:raise RuntimeError(process.stdout.read())
  try:
   health=get('/api/v1/health');break
  except Exception:time.sleep(.5)
 else:raise RuntimeError('Mock did not start in 55 seconds')
 assert health['meta']['data_mode']=='mock'
 foods=get('/api/v1/foods?limit=2');assert len(foods['data']['items'])==2
 cursor=foods['page']['next_cursor'];assert cursor
 second=get('/api/v1/foods?'+urlencode({'limit':2,'cursor':cursor}));assert len(second['data']['items'])==2
 assert set(f['food_id'] for f in foods['data']['items']).isdisjoint(f['food_id'] for f in second['data']['items'])
 match=get('/api/v1/foods?'+urlencode({'q':'두바이쫀득'}));assert match['data']['items']
 food=match['data']['items'][0]['food_id']
 timeline=get('/api/v1/foods/'+food+'/timeline?period=7d&source=instagram_graph&granularity=day');assert timeline['data']['series'][0]['points']
 missing=get('/api/v1/foods/FOOD_MOCK_HEUGIMJA/timeline?period=7d&source=instagram_graph');assert all(p['value'] is None for p in missing['data']['series'][0]['points'])
 print(json.dumps({'upstream':'official app_mock','health':'mock','pagination':'passed','alias_search':'passed','timeline':'passed','null_values':'preserved','production_api_tested':False},ensure_ascii=False))
finally:
 process.terminate()
 try:process.wait(timeout=5)
 except subprocess.TimeoutExpired:process.kill();process.wait()
 server.shutdown();server.server_close()
