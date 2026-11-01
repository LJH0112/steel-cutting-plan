"""Angles use the existing TS import, engine, trace and reference report forms."""
import io
import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest
import xlrd
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from app import app
from steelplan import settings as settings_module
from steelplan.bom import inspect_bom, parse_bom
from steelplan.exports import export_pdf, export_xlsx
from steelplan.materials import DEFAULT_STOCK_LENGTHS, classify_material
from steelplan.models import OptimizeRequest, StockSettings
from steelplan.planning import plan_by_material, validate_plan_input
from steelplan.reports import MATERIAL_COLUMNS, COMBINATION_COLUMNS, REPORT_ROW_HEIGHT


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_module,'SETTINGS_PATH',tmp_path/'stock_settings.json')
    monkeypatch.delenv('STEELPLAN_ADMIN_PASSWORD',raising=False)


def angle_bom():
    wb=Workbook();ws=wb.active;ws.title='TS BOM LIST'
    for row in [['BOM LIST'],['Synthetic angle test'],[],[],
                ['Ass Mark','Part Mark','PROFILE','Length(mm)',"Q'ty",'Unit','Grade']]:
        ws.append(row)
    for row in [['A1','','L65*65*5',2000,2,'',''], # Assembly summary is not an extra two pieces.
                ['A1','L1','L65*65*5',2000,2,'','SS275'],
                ['A2','L2','L65*65*5',2000,3,'','SS275'],
                ['A3','L1','L65*65*5',2000,1,'','SS275'],
                ['A4','L3','L65*65*5',2000,1,'','SM355'],
                ['A5','L4','L75*75*6',2000,1,'','SS275'],
                ['P1','PL1','PL6*100',200,1,'','SS275']]:
        ws.append(row)
    wb.create_sheet('TS ASSEM LIST').append(['Other sheet is not a cutting source'])
    out=io.BytesIO();wb.save(out);return out.getvalue()


def parse(data):
    s=inspect_bom(data)['sheets'][0]
    return parse_bom(data,s['name'],s['header_row'],s['mapping'])


def config():return StockSettings(stock_lengths_by_type=DEFAULT_STOCK_LENGTHS)


@pytest.mark.parametrize('profile',['L65*65*5','L-75×75×6',' L 100 * 75 * 7 ','l50x50x4'])
def test_angle_profiles_are_included_by_ts_bom_import(profile):
    data=angle_bom()
    wb=load_workbook(io.BytesIO(data));ws=wb['TS BOM LIST'];ws['C7']=profile
    out=io.BytesIO();wb.save(out)
    parsed=parse(out.getvalue())
    assert parsed['errors']==[]
    member=next(i for i in parsed['items'] if i['source_row']==7)
    assert member['title']=='L1' and member['quantity']==2
    assert classify_material(member['spec'])=='앵글'


@pytest.mark.parametrize('lengths',[[6000],[8000],[12000],[10000,12000]])
def test_angle_order_length_is_fixed_at_10000_even_for_api_settings(lengths):
    value={**DEFAULT_STOCK_LENGTHS,'앵글':lengths}
    with pytest.raises(ValueError,match='앵글.*10000mm 고정'):
        StockSettings(stock_lengths_by_type=value)
    with TestClient(app,client=('127.0.0.1',1234)) as client:
        assert client.put('/api/settings',json={'stock_lengths_by_type':value}).status_code==422
        assert client.get('/api/settings').json()['stock_lengths_by_type']['앵글']==['10000']


def test_angle_quantities_grade_spec_isolation_and_combined_marks(tmp_path):
    parsed=parse(angle_bom())
    assert sum(i['quantity'] for i in parsed['items'])==8
    assert all(classify_material(i['spec'])=='앵글' for i in parsed['items'])
    request=OptimizeRequest(items=parsed['items'],import_audit=parsed['import_audit'],time_limit=2)
    p=plan_by_material(request,config())
    assert p['summary']['piece_count']==8 and p['summary']['pending_piece_count']==0
    assert p['summary']['bar_count']==4 and p['summary']['total_new_stock_length']==40000
    assert p['summary']['group_count']==3
    assert all(o['material_type']=='앵글' and o['stock_length']==10000 for o in p['orders'])
    expected=Counter()
    for i in parsed['items']:
        expected[i['material'],i['spec'],i['assembly_mark']+'_'+i['title']]+=i['quantity']
    actual=Counter()
    for g in p['report']['combination_groups']:
        for pattern in g['patterns']:
            assert pattern['stock_length']==10000 and pattern['remnant']>30
            for row in pattern['rows']:
                actual[g['material'],g['spec'],row['remarks']]+=row['total_quantity']
    assert actual==expected
    for row in p['report']['part_mark_reconciliation']:
        assert row['required_quantity']==row['planned_quantity'] and row['pending_quantity']==0
    wb=load_workbook(io.BytesIO(export_xlsx(p)))
    assert [c.value for c in wb['원자재 리스트'][4]]==[label for _,label in MATERIAL_COLUMNS]
    assert [c.value for c in wb['조합 리스트'][4]]==[label for _,label in COMBINATION_COLUMNS]
    exported=Counter()
    for row in wb['조합 리스트'].iter_rows(min_row=5):
        if isinstance(row[8].value,int) and row[0].value!='합 계':
            exported[row[10].value]+=row[8].value
            assert wb['조합 리스트'].row_dimensions[row[0].row].height==REPORT_ROW_HEIGHT
    assert exported==Counter({key[2]:q for key,q in expected.items()})
    assert wb['원자재 리스트']['A4'].fill.fgColor.rgb.endswith('FFFF00')
    path=tmp_path/'angles.pdf';path.write_bytes(export_pdf(p))
    if shutil.which('pdftotext'):
        text=subprocess.check_output(['pdftotext','-raw',str(path),'-'],text=True)
        assert 'L65*65*5' in text and all(mark in text for mark in exported)


def test_angle_long_parts_and_short_remnants_keep_existing_review_rules():
    request=OptimizeRequest(items=[dict(source_format='ts_bom',source_sheet='TS BOM LIST',
        material='SS275',spec='L65*65*5',assembly_mark='A1',title='L1',length=9990,quantity=2)],time_limit=1)
    v=validate_plan_input(request,config())
    p=plan_by_material(request,config())
    assert p['summary']['pending_piece_count']==2 and p['orders']==[]
    candidate=next(c for c in v['candidate_reviews'] if c['kind']=='SHORT_REMNANT_PATTERN')
    assert candidate['stock_length']==10000 and candidate['remnant']==7
    p=plan_by_material(request.model_copy(update={'proposal_token':v['proposal_token'],'approved_candidate_ids':[candidate['id']]}),config())
    assert p['summary']['piece_count']==2 and p['summary']['bar_count']==2
    assert all(o['stock_length']==10000 for o in p['orders'])


def test_angle_api_workflow_includes_both_reference_forms():
    client=TestClient(app);data=angle_bom();s=inspect_bom(data)['sheets'][0]
    import json
    response=client.post('/api/preview',files={'file':('angles.xlsx',data)},data={
        'sheet':s['name'],'header_row':s['header_row'],'mapping':json.dumps(s['mapping'])})
    assert response.status_code==200
    parsed=response.json();assert sum(i['quantity'] for i in parsed['items'])==8
    response=client.post('/api/optimize',json={'items':parsed['items'],'import_audit':parsed['import_audit'],'time_limit':2})
    assert response.status_code==200
    result=response.json();assert result['summary']['piece_count']==8
    for fmt in ('xlsx','pdf'):
        assert client.get(f"/api/results/{result['job_id']}.{fmt}").status_code==200


@pytest.mark.skipif(not os.environ.get('STEELPLAN_REAL_BOM'),reason='Local customer BOM is not committed.')
def test_actual_bom_angle_counts_match_original_ts_rows():
    data=Path(os.environ['STEELPLAN_REAL_BOM']).read_bytes()
    book=xlrd.open_workbook(file_contents=data,on_demand=True);ws=book.sheet_by_name('TS BOM LIST')
    original={}
    for row in range(5,ws.nrows):
        if str(ws.cell_value(row,2)).strip().upper().startswith('L') and str(ws.cell_value(row,1)).strip():
            original[row+1]=int(ws.cell_value(row,4))
    book.release_resources()
    parsed=parse(data)
    angles={i['source_row']:i['quantity'] for i in parsed['items'] if classify_material(i['spec'])=='앵글'}
    assert angles==original
    p=plan_by_material(OptimizeRequest(items=parsed['items'],import_audit=parsed['import_audit'],time_limit=2),config())
    audit=[r for r in p['report']['reconciliation'] if r['row'] in original]
    assert sum(r['required_quantity'] for r in audit)==sum(original.values())
    assert all(r['required_quantity']==r['planned_quantity']+r['pending_quantity'] for r in audit)
    assert sum(r['planned_quantity'] for r in audit)==sum(c['total_quantity'] for g in p['report']['combination_groups']
        if classify_material(g['spec'])=='앵글' for pat in g['patterns'] for c in pat['cuts'])
