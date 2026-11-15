"""BK partitioning keeps quantities, approvals, stock and the four forms separate."""
import io
import json
import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from zipfile import ZipFile

import pytest
import xlrd
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from app import app
from steelplan import settings as settings_module
from steelplan.bom import inspect_bom, parse_bom
from steelplan.exports import export_xlsx, export_pdf_zip
from steelplan.materials import DEFAULT_STOCK_LENGTHS
from steelplan.models import BOMItem, OptimizeRequest, StockSettings
from steelplan.planning import plan_by_material, validate_plan_input

PRIMARY=['BK 조합','BK 원자재','거더 조합','거더 원자재']


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path,monkeypatch):
    monkeypatch.setattr(settings_module,'SETTINGS_PATH',tmp_path/'settings.json')
    monkeypatch.delenv('STEELPLAN_ADMIN_PASSWORD',raising=False)


def part(title,length=2000,qty=1,**more):
    return {'source_format':'ts_bom','source_sheet':'TS BOM LIST','material':'SS275',
        'spec':'C100*50*20*2.3','assembly_mark':'A1','title':title,'length':length,'quantity':qty,**more}


def config():return StockSettings(stock_lengths_by_type=DEFAULT_STOCK_LENGTHS)
def run(request):return plan_by_material(request,config())


@pytest.mark.parametrize(('mark','expected'),[('BK1','BK'),('WBK3','BK'),('FBK2','BK'),
    ('bk-01','BK'),(' WBK3  ','BK'),('B5_WBK3','BK'),('C1-FBK2','BK'),('BK','BK'),
    ('NORMAL','GENERAL'),('MB1','GENERAL'),('BOOK1','GENERAL'),('ABK1','GENERAL'),('WBKX','GENERAL')])
def test_bk_is_derived_from_the_confirmed_part_mark_rule(mark,expected):
    item=BOMItem(**part(mark,cutting_group='GENERAL' if expected=='BK' else 'BK'))
    assert item.cutting_group==expected  # Submitted classification cannot override the source mark.
    assert item.model_dump()['cutting_group']==expected
    assert BOMItem(**part('NORMAL',assembly_mark='BK1')).cutting_group=='GENERAL'


@pytest.mark.parametrize('mode',['manufacturing','legacy'])
def test_same_material_spec_and_length_never_mix_between_bk_and_girders(mode):
    request=OptimizeRequest(items=[part('NORMAL'),part('WBK3',qty=3)],planning_mode=mode,time_limit=2)
    separated=run(request)
    # Without the BK constraint all four parts fit in one 10m bar.
    unsplit=request.model_copy(update={'items':[i.model_copy(update={'source_format':'legacy'}) for i in request.items]})
    before=run(unsplit)
    assert before['summary']['bar_count']==1 and separated['summary']['bar_count']==2
    assert before['summary']['piece_count']==separated['summary']['piece_count']==4
    assert {o['cutting_group'] for o in separated['orders']}=={'GENERAL','BK'}
    by_group={r['cutting_group']:r for r in separated['report']['cutting_group_summaries']}
    assert (by_group['GENERAL']['confirmed_quantity'],by_group['BK']['confirmed_quantity'])==(1,3)
    assert (by_group['GENERAL']['bar_count'],by_group['BK']['bar_count'])==(1,1)
    assert by_group['GENERAL']['combined_length']==2000 and by_group['BK']['combined_length']==6000
    assert by_group['GENERAL']['remnant']==7997 and by_group['BK']['remnant']==3991
    assert by_group['GENERAL']['loss_rate']==80 and by_group['BK']['loss_rate']==40
    for group in separated['report']['combination_groups']:
        allocations=[a for p in group['patterns'] for cut in p['cuts'] for a in cut['allocations']]
        assert {a['cutting_group'] for a in allocations}=={group['cutting_group']}
        assert {a['title'] for a in allocations}==({'WBK3'} if group['cutting_group']=='BK' else {'NORMAL'})


@pytest.mark.parametrize('inventory_group',['GENERAL','BK'])
def test_finite_remnants_are_reserved_for_one_cutting_group(inventory_group):
    request=OptimizeRequest(items=[part('NORMAL'),part('WBK3')],time_limit=2,
        remnant_inventory=[dict(id='R1',material='SS275',spec='C100*50*20*2.3',length=5000,
            quantity=1,confirmed=True,cutting_group=inventory_group)])
    p=run(request)
    assert p['summary']['piece_count']==2 and p['summary']['bar_count']==1
    assert p['inventory_usage'][0]['used_quantity']==1
    assert p['inventory_usage'][0]['cutting_group']==inventory_group
    assert {pat['cutting_group'] for pat in p['patterns'] if pat.get('inventory_id')}=={inventory_group}
    assert all(len({a['cutting_group'] for cut in pat['cuts'] for a in cut['allocations']})==1
        for group in p['report']['combination_groups'] for pat in group['patterns'])


def test_short_remnant_approval_is_also_separate_for_bk():
    request=OptimizeRequest(items=[part('NORMAL',4982,2),part('WBK3',4982,2)],time_limit=2)
    validation=validate_plan_input(request,config())
    candidates=[c for c in validation['candidate_reviews'] if c['kind']=='SHORT_REMNANT_PATTERN']
    assert {c['cutting_group'] for c in candidates}=={'GENERAL','BK'}
    bk=next(c for c in candidates if c['cutting_group']=='BK')
    assert {r['title'] for r in bk['members']}=={'WBK3'}
    p=run(request.model_copy(update={'proposal_token':validation['proposal_token'],'approved_candidate_ids':[bk['id']]}))
    stats={r['cutting_group']:r for r in p['report']['cutting_group_summaries']}
    assert stats['GENERAL']['bar_count']==2 and stats['BK']['bar_count']==1
    assert p['summary']['piece_count']==4
    assert all(pat['cutting_group']=='BK' for pat in p['patterns'] if pat.get('approval_id'))


def test_splice_candidates_only_use_inventory_in_the_same_partition():
    request=OptimizeRequest(items=[part('NORMAL',10050),part('WBK3',10050)],time_limit=2,
        remnant_inventory=[dict(id=key,material='SS275',spec='C100*50*20*2.3',length=500,
            quantity=1,confirmed=True,cutting_group=group)for key,group in [('RG','GENERAL'),('RB','BK')]])
    v=validate_plan_input(request,config())
    candidates=[c for c in v['candidate_reviews'] if c['kind']=='SMALL_SPLICE']
    assert {(c['cutting_group'],c['inventory_id'])for c in candidates}=={('GENERAL','RG'),('BK','RB')}
    c=next(c for c in candidates if c['cutting_group']=='BK')
    p=run(request.model_copy(update={'proposal_token':v['proposal_token'],'approved_candidate_ids':[c['id']]}))
    assert p['summary']['piece_count']==1 and p['summary']['pending_piece_count']==1
    assert p['pending'][0]['title']=='NORMAL'
    assert {pat['cutting_group'] for pat in p['patterns']}=={'BK'}


def book_bytes(rows,header_row=1,reverse=False):
    wb=Workbook();ws=wb.active;ws.title='TS BOM LIST'
    headers=['Ass Mark','Part Mark','PROFILE','Length(mm)',"Q'ty",'Grade']
    for _ in range(header_row-1):ws.append(['Project heading'])
    ws.append(list(reversed(headers)) if reverse else headers)
    for row in rows:ws.append(list(reversed(row)) if reverse else row)
    wb.create_sheet('TS ASSEM LIST').append(['Do not use this sheet'])
    output=io.BytesIO();wb.save(output);return output.getvalue()


def preview(client,data,name):
    response=client.post('/api/inspect',files={'file':(name,data)});assert response.status_code==200
    s=response.json()['sheets'][0]
    response=client.post('/api/preview',files={'file':(name,data)},data={
        'sheet':s['name'],'header_row':s['header_row'],'mapping':json.dumps(s['mapping'])})
    assert response.status_code==200
    return response.json()


def counts_in_sheet(sheet):
    counts=Counter()
    for row in sheet.iter_rows(min_row=5,values_only=True):
        if isinstance(row[8],int) and row[0]!='합 계':counts[row[10]]+=row[8]
    return counts


def test_other_files_are_reclassified_and_always_produce_the_four_outputs(tmp_path):
    client=TestClient(app)
    files=[('first_project.xlsx',book_bytes([['G1','M1','C100*50*20*2.3',2000,1,'SS275'],
                                           ['B5','WBK3','C100*50*20*2.3',2000,3,'SS275']],header_row=3)),
           ('another_project.xlsx',book_bytes([['G9','N9','L65*65*5',2000,2,'SS275']],header_row=8,reverse=True))]
    for name,data in files:
        parsed=preview(client,data,name)
        result=client.post('/api/optimize',json={'items':parsed['items'],'import_audit':parsed['import_audit'],'source_filename':name,'time_limit':2})
        assert result.status_code==200
        result=result.json();job=result['job_id']
        response=client.get(f'/api/results/{job}.xlsx');assert response.status_code==200
        workbook=load_workbook(io.BytesIO(response.content))
        assert workbook.sheetnames[:4]==PRIMARY
        bk=counts_in_sheet(workbook['BK 조합']);girder=counts_in_sheet(workbook['거더 조합'])
        assert not (set(bk)&set(girder))
        if name.startswith('first'):
            assert bk=={'B5_WBK3':3} and girder=={'G1_M1':1}
        else:
            assert not bk and girder=={'G9_N9':2}
        assert sum(bk.values())+sum(girder.values())==sum(i['quantity']for i in parsed['items'])
        response=client.get(f'/api/results/{job}.pdfzip');assert response.status_code==200
        archive=ZipFile(io.BytesIO(response.content))
        assert archive.namelist()==[n+'.pdf' for n in PRIMARY]
        for document in PRIMARY:
            output=archive.read(document+'.pdf');assert output.startswith(b'%PDF-')
            if shutil.which('pdftotext'):
                path=tmp_path/(name+'_'+document+'.pdf');path.write_bytes(output)
                text=subprocess.check_output(['pdftotext','-raw',str(path),'-'],text=True)
                if name.startswith('first'):
                    assert ('B5_WBK3' in text)==(document=='BK 조합')
                    assert ('G1_M1' in text)==(document=='거더 조합')


def test_only_pending_bk_still_has_all_four_forms(tmp_path):
    p=run(OptimizeRequest(items=[part('BK1',10895,2)],time_limit=1))
    assert p['summary']['piece_count']==0 and p['summary']['pending_piece_count']==2
    assert p['report']['partition_reports']['BK']['combination_groups'][0]['pending_quantity']==2
    wb=load_workbook(io.BytesIO(export_xlsx(p,layout='split')))
    assert wb.sheetnames[:4]==PRIMARY
    archive=ZipFile(io.BytesIO(export_pdf_zip(p)))
    assert len(archive.namelist())==4
    if shutil.which('pdftotext'):
        for name in archive.namelist():
            path=tmp_path/name;path.write_bytes(archive.read(name))
            text=subprocess.check_output(['pdftotext','-raw',str(path),'-'],text=True)
            assert ('BK1' in text)==name.startswith('BK ')


@pytest.mark.skipif(not os.environ.get('STEELPLAN_REAL_BOM'),reason='Local customer BOM is not committed.')
def test_actual_bom_partition_and_source_row_quantities_are_conserved():
    data=Path(os.environ['STEELPLAN_REAL_BOM']).read_bytes()
    book=xlrd.open_workbook(file_contents=data,on_demand=True);ws=book.sheet_by_name('TS BOM LIST')
    original={i+1:int(ws.cell_value(i,4)) for i in range(5,ws.nrows)
        if str(ws.cell_value(i,1)).strip().upper().startswith(('BK','WBK','FBK')) and str(ws.cell_value(i,6)).strip()}
    book.release_resources()
    assert len(original)==50 and sum(original.values())==73
    client=TestClient(app);parsed=preview(client,data,'different_file_name.xls')
    assert {i['source_row']:i['quantity']for i in parsed['items'] if i['cutting_group']=='BK'}==original
    request=OptimizeRequest(items=parsed['items'],import_audit=parsed['import_audit'],time_limit=2)
    p=run(request)
    stats={r['cutting_group']:r for r in p['report']['cutting_group_summaries']}
    assert stats['BK']['required_quantity']==stats['BK']['confirmed_quantity']==73
    assert stats['GENERAL']['required_quantity']==219
    assert stats['GENERAL']['confirmed_quantity']==204 and stats['GENERAL']['pending_quantity']==15
    assert sum(r['required_quantity']for r in stats.values())==292
    assert len(p['report']['reconciliation'])==141
    assert all(r['required_quantity']==r['planned_quantity']+r['pending_quantity'] for r in p['report']['reconciliation'])
    wb=load_workbook(io.BytesIO(export_xlsx(p,layout='split')))
    assert sum(counts_in_sheet(wb['BK 조합']).values())==73
    assert sum(counts_in_sheet(wb['거더 조합']).values())==204
    assert p['summary']['piece_count']==277 and p['summary']['pending_piece_count']==15
