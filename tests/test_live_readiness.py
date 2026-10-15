"""Regressions for PR #1 review; never access an installed game or history."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

def load():
    spec = importlib.util.spec_from_file_location('readiness_bridge', ROOT/'server/tf3_mcp_server.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def fixture(self, count=1, lines=1, target=0):
        line_rows = [{'id':i,'name':f'Line {i:03}', 'cargos':['PASSENGERS'],
                      'usagePct':100 if i==lines-1 else 30} for i in range(lines)]
        stations = [{'id':i,'name':f'Station {i}', 'town':'Town',
                     'lines':[{'id':l['id']} for l in line_rows],
                     'waiting':{'PASSENGERS':count+100-i}} for i in range(count)]
        samples = [{'gameTime':i,'lines':{str(l['id']):l for l in line_rows},
                    'stations':{str(target):{'id':target,'name':f'Station {target}',
                        'town':'Town','waiting':100,'lineIds':[str(l['id']) for l in line_rows]}}}
                   for i in range(3)]
        self.m.load_snapshot = lambda:({'lines':line_rows},None,0)
        self.m.load_kind = lambda kind:({'stations':stations},None,0)
        return samples, stations

    def test_station_101_classified_from_full_evidence(self):
        samples,_ = self.fixture(count=101,target=100)
        diag = self.m._v86_service_diagnosis('Town',{'save_key':'test'},samples=samples)
        self.assertEqual(diag['capacityReviewCount'],1)
        self.assertEqual(diag['stations'][0]['lineCount'],1)
        # Public presentation remains bounded.
        self.assertEqual(self.m.tool_passenger_line_diagnostic_v82({'limit':150})['returnedStations'],100)

    def test_13th_line_capacity_not_hidden_by_public_limit(self):
        samples,_ = self.fixture(lines=13)
        diag = self.m._v86_service_diagnosis('Town',{'save_key':'test'},samples=samples)
        self.assertEqual(diag['capacityReviewCount'],1)
        self.assertEqual(diag['stations'][0]['lineCount'],13)
        self.assertEqual(len(self.m.tool_passenger_line_diagnostic_v82({})['stations'][0]['lines']),12)

    def test_missing_current_station_is_insufficient_evidence(self):
        samples,_ = self.fixture()
        self.m.load_kind = lambda kind:({'stations':[]},None,0)
        diag = self.m._v86_service_diagnosis('Town',{'save_key':'test'},samples=samples)
        self.assertEqual(diag['insufficientCurrentEvidenceCount'],1)
        self.assertEqual(diag['servicePatternReviewCount'],0)
        self.assertEqual(diag['capacityReviewCount'],0)

    def test_stale_current_propagates_to_standalone_and_combined(self):
        samples,stations = self.fixture()
        self.m.load_kind = lambda kind:({'stations':stations},'Stale stations',999)
        self.m._v85_history = lambda args:(samples,'test',[])
        diag = self.m.tool_passenger_service_diagnosis_v86({'town':'Town','save_key':'test'})
        self.assertIn('Stale stations',diag['warnings'])
        self.assertEqual(diag['currentDataAgeSec'],999)
        self.assertEqual(diag['insufficientCurrentEvidenceCount'],1)
        self.m.tool_network_investment_advisor_v85 = lambda args:{'recommendations':[
            {'category':'passenger','town':'Town','historicalAssessment':'persistent_capacity_investigation_priority'}]}
        result = self.m.tool_network_investment_advisor_v861({'save_key':'test'})
        embedded = result['recommendations'][0]['servicePatternDiagnosis']
        self.assertIn('Stale stations',embedded['warnings'])
        self.assertIn('Stale stations',result['historyWarnings'])
        self.assertEqual(embedded['currentDataAgeSec'],999)
        self.assertEqual(embedded['capacityReviewCount'],0)

    def test_recording_interval_and_invalid_conversion(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'TF3_HISTORY_DIR':directory}):
            snap={'towns':[],'lines':[],'meta':{'gameTime':100,'yearTicks':365}}
            self.m.load_snapshot=lambda:(snap,None,0)
            self.m.load_kind=lambda kind:({'stations':[]},None,0)
            self.assertTrue(self.m.tool_passenger_history_record_v84({'save_key':'test'})['recorded'])
            snap['meta']['gameTime']=101
            args={'save_key':'test','min_game_days':2}
            self.assertEqual(self.m.tool_passenger_history_record_v84(args)['status'],'minimum_game_interval_not_reached')
            snap['meta']['gameTime']=102
            self.assertTrue(self.m.tool_passenger_history_record_v84(args)['recorded'])
            snap['meta']['gameTime']=103
            for value in (None,0,-1,float('nan'),float('inf')):
                snap['meta']['yearTicks']=value
                self.assertEqual(self.m.tool_passenger_history_record_v84(args)['status'],'game_time_conversion_unavailable')
            for value in (-1,float('nan'),float('inf')):
                with self.assertRaises(ValueError):
                    self.m.tool_passenger_history_record_v84({'save_key':'test','min_game_days':value})

    def test_both_simulators_ignore_inherited_live_paths(self):
        # A decoy with sentinel contents must remain byte-identical. Empty/non-Lua
        # files ensure an accidental selection fails before any real game access.
        with tempfile.TemporaryDirectory() as directory:
            decoy=Path(directory)
            (decoy/'keep.txt').write_text('untouched')
            before={p.name:p.read_bytes() for p in decoy.iterdir()}
            env=dict(os.environ,TF3_MCP_BRIDGE_DIR=directory,TF3_HISTORY_DIR=directory)
            for script in ('bridge_test.py','mod_server_integration_test.py'):
                run=subprocess.run([sys.executable,str(ROOT/'tests'/script)],env=env,
                                   capture_output=True,text=True,timeout=40)
                self.assertEqual(run.returncode,0,run.stdout+'\n'+run.stderr)
                self.assertEqual(before,{p.name:p.read_bytes() for p in decoy.iterdir()})

if __name__ == '__main__':
    unittest.main(verbosity=2)
