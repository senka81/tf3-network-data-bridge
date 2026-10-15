import importlib.util, pathlib
p=pathlib.Path(__file__).resolve().parents[1]/'server'/'tf3_mcp_server.py'
spec=importlib.util.spec_from_file_location('bridge861',p)
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
assert 'tf3_network_investment_advisor_v861' in m.HANDLERS
assert 'tf3_passenger_service_diagnosis_v86' in m.HANDLERS

# Test the full integrated handoff by replacing underlying read-only export access
# with synthetic game/history rows. Verify that no empty line lists reach the diagnosis.
samples=[{'gameTime':i,'stations': [],'lines':[]} for i in range(5)]
def mkstation(sid,name,q,lines):
    return {'stationGroupId':sid,'station':name,'waitingPassengersSnapshot':q,'lines':lines}
def mkline(lid,name,usage):
    return {'lineId':lid,'line':name,'vehicles':1,'intervalSec':180,'usagePct':usage}
source={
 'TestFullTown':[mkstation(10,'TestFullTown Centre',38,[mkline(1,'Test Full Line 1',100),mkline(2,'Test Full Line 2',100)])],
 'TestMixedTown':[mkstation(20,'TestMixedTown Grand Central',180,[mkline(3,'Test Mixed Line 1',76),mkline(4,'Test Mixed Line 2',79)])]
}
orig={k:getattr(m,k) for k in ('tool_network_investment_advisor_v85','_v85_history','_v85_station_rows','_v85_line_history_index','tool_passenger_line_diagnostic_v82')}
try:
    m.tool_network_investment_advisor_v85=lambda args:{'recommendations':[
     {'category':'passenger','town':'TestFullTown','historicalAssessment':'persistent_capacity_investigation_priority','busyStations':[{'stationGroupId':10,'servingLines':source['TestFullTown'][0]['lines']}]},
     {'category':'passenger','town':'TestMixedTown','historicalAssessment':'persistent_station_queues_without_confirmed_line_capacity_shortage','busyStations':[{'stationGroupId':20,'servingLines':source['TestMixedTown'][0]['lines']}]},
     {'category':'cargo','town':'TestMixedTown','cargo':'PLANKS'}], 'knownLimitations':[]}
    m._v85_history=lambda args:(samples,'synthetic-save',[])
    def stationrows(history,town,threshold,limit):
        st=source[town][0]
        return ([{'stationGroupId':st['stationGroupId'],'station':st['station'],'meanWaiting':st['waitingPassengersSnapshot'], 'busyObservations':5,'samplesObserved':5,'busyFraction':1.0,'trendStatus':'persistent_busy_station'}],[])
    m._v85_station_rows=stationrows
    m._v85_line_history_index=lambda history,town,threshold: {str(line['lineId']):{'historyGrade':('persistent_full_and_busy_shared_stations' if town=='TestFullTown' else 'mixed_or_unconfirmed'),'meanUtilizationPct':line['usagePct']} for line in source[town][0]['lines']}
    m.tool_passenger_line_diagnostic_v82=lambda args, _complete=False:{'stations':source[args['town']]}
    result=m.HANDLERS['tf3_network_investment_advisor']({'save_key':'synthetic-save'})
    cl,ma,ca=result['recommendations']
    assert cl['servicePatternDiagnosis']['capacityReviewCount']==1,cl
    assert cl['servicePatternDiagnosis']['servicePatternReviewCount']==0,cl
    assert len(cl['servicePatternDiagnosis']['priorityStations'][0]['servingLines'])==2
    assert ma['servicePatternDiagnosis']['capacityReviewCount']==0,ma
    assert ma['servicePatternDiagnosis']['servicePatternReviewCount']==1,ma
    assert len(ma['servicePatternDiagnosis']['priorityStations'][0]['servingLines'])==2
    assert 'servicePatternDiagnosis' not in ca
    print('PASS: TestFullTown capacity, TestMixedTown service-pattern, cargo untouched, lines populated')
    # Negative test: Contradiction produces explicit error, not silent bad advice.
    m._v85_line_history_index=lambda history,town,threshold:{}
    try:m.HANDLERS['tf3_network_investment_advisor']({'save_key':'synthetic-save'})
    except RuntimeError as e:assert 'TestFullTown' in str(e);print('PASS: classification conflict detection')
    else:raise AssertionError('Mismatch must fail loudly')
finally:
    for k,v in orig.items():setattr(m,k,v)
