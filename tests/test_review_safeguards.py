import importlib.util, pathlib, tempfile, os, json, subprocess, sys, unittest
ROOT=pathlib.Path(__file__).resolve().parent.parent
def load(path):
 spec=importlib.util.spec_from_file_location('bridge',path); m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
m=load(ROOT/'server/tf3_mcp_server.py'); old=json.loads((ROOT/'tests/fixtures/upstream_tool_schemas.json').read_text())
class ReviewTests(unittest.TestCase):
 def test_original_tools_preserved(self):
  for name, schema in old.items():
   current=next(t for t in m.TOOLS if t['name']==name)
   self.assertEqual(schema,current['inputSchema'])
  self.assertEqual(len(m.TOOLS),len(m.HANDLERS))
 def test_history_isolation(self):
  snap={'towns':[{'id':1,'name':'Town'}]}
  with self.assertRaises(ValueError):m._v84_identity(snap,{})
  self.assertNotEqual(m._v84_identity(snap,{'save_key':'A'})[0],m._v84_identity(snap,{'save_key':'B'})[0])
  with self.assertRaises(ValueError):m._v84_identity(snap,{'save_key':'../../escape'})
 def test_history_recording(self):
  with tempfile.TemporaryDirectory() as directory:
   os.environ['TF3_HISTORY_DIR']=directory
   snap={'towns':[{'id':1,'name':'Town'}],'meta':{'gameTime':1},'lines':[]}
   m.load_snapshot=lambda:(snap,None,0)
   m.load_kind=lambda kind:({'stations':[]},None,0)
   self.assertTrue(m.tool_passenger_history_record_v84({'save_key':'A'})['recorded'])
   self.assertEqual(m.tool_passenger_history_record_v84({'save_key':'A'})['status'],'duplicate_game_time_skipped')
   snap['meta']['gameTime']=0
   self.assertEqual(m.tool_passenger_history_record_v84({'save_key':'A'})['status'],'game_time_rollback_not_recorded')
   self.assertTrue(m.tool_passenger_history_record_v84({'save_key':'B'})['recorded'])
   snap['meta']['gameTime']=2
   m.load_snapshot=lambda:(snap,'stale',2)
   self.assertEqual(m.tool_passenger_history_record_v84({'save_key':'A'})['status'],'stale_export_not_recorded')
   del os.environ['TF3_HISTORY_DIR']
 def test_protocol_survives_bad_messages(self):
  messages=[[],None,{'jsonrpc':'2.0','id':1,'method':'ping','params':[]},{'jsonrpc':'2.0','id':2,'method':'ping'},{'jsonrpc':'2.0','id':3,'method':'tools/list'}]
  p=subprocess.run([sys.executable,'-I',str(ROOT/'server/tf3_mcp_server.py')],input='\n'.join(map(json.dumps,messages))+'\n',text=True,capture_output=True,timeout=5)
  self.assertEqual(p.returncode,0,p.stderr)
  rows=list(map(json.loads,p.stdout.splitlines()))
  self.assertEqual([r['error']['code'] for r in rows[:3]],[-32600,-32600,-32602]);self.assertEqual(rows[3]['result'],{})
  self.assertEqual(len(rows[4]['result']['tools']),len(m.TOOLS))
 def test_old_mod_guard(self):
  m=load(ROOT/'server/tf3_mcp_server.py')
  m.mod_version=lambda:5
  for kind in ('catchment','cargo_access','town_delivery'):
   with self.assertRaises(RuntimeError):m.load_kind(kind)
if __name__=='__main__':
 print('Upstream tools:',len(old),'Proposed tools:',len(m.TOOLS),flush=True)
 unittest.main(verbosity=2)
