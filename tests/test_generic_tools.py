"""Canonical discovery and backwards-compatible dispatch, without live data."""
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
class GenericToolTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('generic_bridge',ROOT/'server/tf3_mcp_server.py')
        self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m)
        self.legacy=json.loads((ROOT/'tests/fixtures/pre_generic_tool_registry.json').read_text())

    def test_discovery_has_only_unique_generic_names(self):
        tools=self.m.handle({'method':'tools/list'})['tools']
        names=[t['name'] for t in tools]
        self.assertEqual(len(names),31)
        self.assertEqual(len(names),len(set(names)))
        self.assertFalse(any(re.search(r'_v\d+$',n) for n in names))
        self.assertEqual(len(self.m.HANDLERS),44)
        self.assertEqual(len(self.m.COMPATIBILITY_ALIASES),13)

    def test_canonical_implementations_and_schemas(self):
        indexed={t['name']:t for t in self.m.TOOLS}
        for canonical,source in self.m.CANONICAL_IMPLEMENTATIONS.items():
            self.assertIs(self.m.HANDLERS[canonical],getattr(self.m,self.legacy[source]['handler']))
            self.assertEqual(indexed[canonical]['inputSchema'],self.legacy[source]['inputSchema'])
        self.assertIn('save_key',indexed['tf3_network_investment_advisor']['inputSchema']['required'])

    def test_every_legacy_versioned_name_keeps_behavior_and_schema(self):
        indexed={t['name']:t for t in self.m.COMPATIBILITY_TOOLS}
        for name,canonical in self.m.COMPATIBILITY_ALIASES.items():
            self.assertIn(canonical,self.m.HANDLERS)
            self.assertIs(self.m.HANDLERS[name],getattr(self.m,self.legacy[name]['handler']))
            self.assertEqual(indexed[name]['inputSchema'],self.legacy[name]['inputSchema'])
        # Old advisors remain compatible with callers that never supplied history keys.
        self.assertNotIn('save_key',indexed['tf3_network_investment_advisor_v81']['inputSchema'].get('required',[]))

    def test_all_preexisting_names_remain_callable(self):
        self.assertTrue(set(self.legacy).issubset(self.m.HANDLERS))
        # Only the two already generic capabilities deliberately promote newer implementations.
        for name,old in self.legacy.items():
            if name not in ('tf3_network_investment_advisor','tf3_cargo_reachability'):
                self.assertEqual(self.m.HANDLERS[name].__name__,old['handler'])

    def test_alias_dispatch_and_matching_passenger_results(self):
        self.m.load_snapshot=lambda:({'lines':[]},None,0)
        self.m.load_kind=lambda kind:({'stations':[]},None,0)
        for canonical in ('tf3_passenger_line_diagnostic','tf3_passenger_capacity_advisor'):
            alias=next(a for a,c in self.m.COMPATIBILITY_ALIASES.items() if c==canonical)
            def call(name):
                response=self.m.handle({'method':'tools/call','params':{'name':name,'arguments':{}}})
                self.assertFalse(response.get('isError'),response)
                return json.loads(response['content'][0]['text'])
            self.assertEqual(call(canonical),call(alias))
        for name in self.m.COMPATIBILITY_ALIASES:
            self.m.HANDLERS[name]=lambda args:{'echo':args}
            response=self.m.handle({'method':'tools/call','params':{'name':name,'arguments':{'sentinel':1}}})
            self.assertEqual(json.loads(response['content'][0]['text']),{'echo':{'sentinel':1}})

    def test_stdio_discovery_and_unknown_tool(self):
        messages=[{'jsonrpc':'2.0','id':1,'method':'tools/list'},
                  {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'tf3_missing_tool'}}]
        p=subprocess.run([sys.executable,'-I',str(ROOT/'server/tf3_mcp_server.py')],
                         input='\n'.join(map(json.dumps,messages))+'\n',text=True,capture_output=True,timeout=5)
        self.assertEqual(p.returncode,0,p.stderr)
        rows=[json.loads(line) for line in p.stdout.splitlines()]
        self.assertEqual(len(rows[0]['result']['tools']),31)
        self.assertEqual(rows[1]['error']['code'],-32601)

if __name__=='__main__':unittest.main(verbosity=2)
