"""Length-first purchasing with finite verified inventory and explicit approvals.

Proposals are server-side snapshots. An approval applies to exactly the displayed
pattern, source demand and inventory state, never to a newly recomputed pattern.
"""
import copy
import hashlib
import json
import secrets
import threading
import time
from collections import Counter, OrderedDict, defaultdict
from decimal import Decimal

from .materials import classify_material, TS_BOM_MATERIAL_TYPES
from .models import BOMItem
from .optimizer import optimize, units, mm, MIN_REMAINDER

_CACHE = OrderedDict()
_LOCK = threading.Lock()
_TTL = 3600


def _digest(request, settings):
    data = request.model_dump(mode='json', exclude={'proposal_token','approved_candidate_ids','time_limit',
        'exclude_invalid','exclusion_token','project_name','project_section','report_date','source_filename','weight_basis'})
    return hashlib.sha256(json.dumps([data,settings.model_dump(mode='json')],sort_keys=True).encode()).hexdigest()


def _lengths(request, settings):
    limits = [v for v in (request.transport_max_length,request.processing_max_length) if v is not None]
    ceiling = min(limits) if limits else None
    return {kind:[s for s in values if ceiling is None or s<=ceiling]
            for kind,values in settings.stock_lengths_by_type.items()}


def _inventory(request):
    return [r for r in request.remnant_inventory if r.confirmed and r.length>=request.reusable_remnant_min
            and (request.processing_max_length is None or r.length<=request.processing_max_length)]


def _scope(item):
    kind=classify_material(item.spec)
    return kind is not None and (item.source_format!='ts_bom' or kind in TS_BOM_MATERIAL_TYPES)


def _solve(request, items, lengths, *, inventory=(), relaxed=False, objective='total_length', seconds=None):
    return optimize(request.model_copy(update={'items':items,'objective':objective,
        'time_limit':seconds if seconds is not None else max(1,request.time_limit/2)}),lengths,inventory,
        minimum_remnant=0 if relaxed else MIN_REMAINDER)


def _row(item,index):
    return {'input_index':index,'row':item.source_row or index+1,'title':item.title,
            'assembly_mark':item.assembly_mark,'material':item.material,'spec':item.spec,
            'length':float(item.length),'quantity':item.quantity,'material_type':classify_material(item.spec)}


def _purchase_score(plan):
    s=plan['summary']
    return s.get('total_new_stock_length',s['total_stock_length']),s['bar_count']


def _allocations(pattern, remaining, items):
    assigned=[]
    for cut in pattern['cuts']:
        needed=cut['quantity']*pattern['bar_count']
        for i,item in enumerate(items):
            if (item.material,item.spec,units(item.length))!=(pattern['material'],pattern['spec'],units(cut['length'])):
                continue
            take=min(needed,remaining[i])
            if take:
                assigned.append({'input_index':item.input_index,'quantity':take})
                remaining[i]-=take;needed-=take
        if needed:raise RuntimeError('승인 후보의 원본 수량 배정 실패')
    return assigned


def proposals(request, settings):
    if request.stock_lengths is not None:
        raise ValueError('전역 발주 길이는 사용할 수 없습니다. 자재 종류별 설정을 사용해 주세요.')
    if sum(i.quantity for i in request.items)>5000:
        raise ValueError('총 부재 수량은 5,000개 이하여야 합니다.')
    if request.import_audit and any(i.source_format!='ts_bom' or i.source_sheet!='TS BOM LIST' for i in request.items):
        raise ValueError('TS BOM LIST의 대상 부재만 사용할 수 있습니다.')
    if any(i.required_inventory_id for i in request.items):
        raise ValueError('연결재 재고 배정은 승인 후보를 통해서만 지정할 수 있습니다.')
    digest=_digest(request,settings)
    with _LOCK:
        now=time.monotonic()
        for key in list(_CACHE):
            if now-_CACHE[key]['created']>_TTL:del _CACHE[key]
        if request.approved_candidate_ids:
            saved=_CACHE.get(request.proposal_token)
            if saved is None or saved['digest']!=digest:
                raise ValueError('승인 후보가 만료되었거나 BOM·재고·조건이 변경되었습니다. 다시 검토하고 승인해 주세요.')
            return saved
        for saved in reversed(_CACHE.values()):
            if saved['digest']==digest:return saved
    lengths=_lengths(request,settings)
    items=[i.model_copy(update={'input_index':n}) for n,i in enumerate(request.items)]
    normal=[];relaxed_items=[];reviews=[]
    kerf=units(request.kerf)
    for n,item in enumerate(items):
        allowed=lengths.get(classify_material(item.spec),[])
        code=reason=None
        if not _scope(item):code,reason='OUTSIDE_SCOPE','최적화 대상 외 규격'
        elif not settings.stock_lengths_by_type.get(classify_material(item.spec)):
            code,reason='LENGTH_UNCONFIGURED','자재 발주 길이 미설정'
        elif not allowed:code,reason='LENGTH_LIMIT','운송·가공 상한에 들어오는 발주 길이 없음'
        elif units(item.length)+kerf+MIN_REMAINDER<=units(max(allowed)):
            normal.append(item);relaxed_items.append(item)
        elif units(item.length)+kerf<=units(max(allowed)):
            relaxed_items.append(item)
            code,reason='SHORT_REMNANT_REVIEW','절단 가능 · 잔재 30mm 이하 승인 필요'
        elif item.length>max(allowed):
            deficit=item.length-max(allowed)
            code='SMALL_SPLICE_REVIEW' if deficit<=100 else 'LARGE_SPLICE_REVIEW'
            reason=f'부족 {deficit:g}mm · '+('보유 잔재 이음 승인 필요' if deficit<=100 else '100mm 초과 · 별도 이음 검토 필요')
        else:code,reason='CUTTING_ALLOWANCE','날 손실을 확보할 수 없음 · 별도 제작 검토'
        if code:reviews.append({**_row(item,n),'code':code,'reason':reason})
    seconds=max(.1,min(3,request.time_limit/4))
    baseline=_solve(request,normal,lengths,seconds=seconds)
    count_first=_solve(request,normal,lengths,objective='bar_count',seconds=seconds)
    # A time-limited count-first run can discover a better feasible incumbent.
    # Keep it when its actual purchase length wins, without borrowing its proof
    # of a different objective or presenting a worse length-first result.
    if _purchase_score(count_first)<_purchase_score(baseline):
        baseline=copy.deepcopy(count_first)
        baseline['summary']['optimal']=False
        for status in baseline['statuses']:
            status.update(status='FEASIBLE',detail='비교안의 더 짧은 실행 가능 조합 활용 · 총길이 최적해 미확정')
    relaxed=_solve(request,relaxed_items,lengths,relaxed=True,seconds=seconds)
    remaining=[i.quantity for i in relaxed_items]
    candidates=[]
    for pattern in relaxed['patterns']:
        allocations=_allocations(pattern,remaining,relaxed_items)
        if pattern['remnant']>30:continue
        refs=[_row(items[a['input_index']],a['input_index'])|{'quantity':a['quantity']} for a in allocations]
        candidates.append({'kind':'SHORT_REMNANT_PATTERN','pattern':pattern,'allocations':allocations,
            'cut_pattern':pattern['cuts'],'bar_count':pattern['bar_count'],
            'members':refs,'quantity':sum(a['quantity'] for a in allocations),'eligible_for_approval':True,
            'reason':'절단날 손실 반영 후 잔재 0~30mm · 해당 절단 패턴 승인 필요',
            'stock_length':pattern['stock_length'],'remnant':pattern['remnant'],
            'material':pattern['material'],'spec':pattern['spec']})
    for n,item in enumerate(items):
        if not _scope(item):continue
        shorter=[s for s in lengths.get(classify_material(item.spec),[]) if 0<item.length-s<=100]
        if not shorter:continue
        base=max(shorter);connector=item.length-base+request.splice_allowance
        matches=[r for r in request.remnant_inventory if (r.material,r.spec)==(item.material,item.spec)
                 and r.length>=request.reusable_remnant_min and units(r.length)>=units(connector)+kerf+MIN_REMAINDER
                 and (request.processing_max_length is None or r.length<=request.processing_max_length)]
        # Offer one verified and one unverified alternative; never invent stock.
        options=[]
        for confirmed in (True,False):
            group=[r for r in matches if r.confirmed==confirmed]
            if group:options.append(min(group,key=lambda r:(r.length,r.id)))
        if not options:options=[None]
        for stock in options:
            capacity=0 if stock is None else ((units(stock.length)-MIN_REMAINDER)//(units(connector)+kerf))*stock.quantity
            qty=min(item.quantity,capacity) if stock else item.quantity
            candidates.append({**_row(item,n),'kind':'SMALL_SPLICE','allocations':[{'input_index':n,'quantity':qty}],
                'quantity':qty,'base_length':float(base),'connector_length':float(connector),
                'shortage':float(item.length-base),'allowance':float(request.splice_allowance),
                'inventory_id':stock.id if stock else None,'inventory_length':float(stock.length) if stock else None,
                'inventory_confirmed':stock.confirmed if stock else False,
                'eligible_for_approval':bool(stock and stock.confirmed and qty),
                'reason':'이음 위치·연결 상세 승인 필요' if stock and stock.confirmed else '사용 가능한 실재고 미확인 · 확정 발주 반영 불가'})
    token=secrets.token_hex(32)
    for c in candidates:
        c['id']=hashlib.sha256((token+json.dumps(c,sort_keys=True)).encode()).hexdigest()
    saved={'created':time.monotonic(),'digest':digest,'token':token,'candidates':candidates,
           'reviews':reviews,'lengths':lengths,'baseline':baseline,'count_first':count_first,'normal_indices':{i.input_index for i in normal}}
    with _LOCK:
        _CACHE[token]=saved
        while len(_CACHE)>20:_CACHE.popitem(last=False)
    return saved


def validate_manufacturing(request, settings):
    saved=proposals(request,settings)
    public=[{k:v for k,v in c.items() if k not in ('pattern','allocations')} for c in saved['candidates']]
    # Nothing is omitted: unresolved rows do not require an "exclude BOM" action.
    return {'issues':[],'eligible_rows':len(request.items),'eligible_pieces':sum(i.quantity for i in request.items),
            'excluded_pieces':0,'splice_candidates':[],'proposal_token':saved['token'],
            'candidate_reviews':public,'pending_reviews':saved['reviews'],
            'pending_pieces':sum(r['quantity'] for r in saved['reviews'])}


def _summarize(plan, request, confirmed, joint_allowance=0):
    orders={};totals=Counter();used_inventory=Counter();inventory_totals=defaultdict(Counter)
    for n,p in enumerate(plan['patterns'],1):
        p['id']=f'CP-{n:03d}'
        q=p['bar_count'];stock=units(p['stock_length']);parts=sum(units(c['length'])*c['quantity'] for c in p['cuts'])
        loss=0 if p.get('uncut') else units(request.kerf)*sum(c['quantity'] for c in p['cuts'])
        remnant=stock-parts-loss
        if remnant<0 or (remnant<MIN_REMAINDER and not p.get('approval_id')):
            raise RuntimeError('미승인 잔재 예외가 확정 계획에 포함되었습니다.')
        p.update(parts_length=mm(parts),kerf_loss=mm(loss),remnant=mm(remnant),
                 loss_rate=round(100*(stock-parts)/stock,2))
        for field,value in [('stock',stock),('parts',parts),('kerf',loss),('remnant',remnant)]:totals[field]+=value*q
        if p.get('inventory_id'):
            used_inventory[p['inventory_id']]+=q;totals['inventory']+=stock*q
            totals['inventory_used']+=(parts+loss)*q
            for field,value in [('input_length',stock),('used_length',parts+loss),('remnant',remnant)]:
                inventory_totals[p['inventory_id']][field]+=value*q
            continue
        key=p['material'],p['spec'],p['stock_length']
        order=orders.setdefault(key,{'material':key[0],'spec':key[1],'stock_length':key[2],
            'material_type':classify_material(key[1]),'quantity':0,'total_length':0,'parts_length':0,'kerf_loss':0,'remnant':0})
        order['quantity']+=q
        for field,value in [('total_length',stock),('parts_length',parts),('kerf_loss',loss),('remnant',remnant)]:order[field]+=value*q
    for o in orders.values():
        o['loss_rate']=round(100*(o['kerf_loss']+o['remnant'])/o['total_length'],2)
        for field in ('total_length','parts_length','kerf_loss','remnant'):o[field]=mm(o[field])
    stock_by_id={r.id:r for r in _inventory(request)}
    if any(k not in stock_by_id or q>stock_by_id[k].quantity for k,q in used_inventory.items()):
        raise RuntimeError('확인 재고 초과 또는 미확인 재고 사용')
    finished=sum(units(i.length)*confirmed[n] for n,i in enumerate(request.items))
    if totals['parts']-joint_allowance!=finished:raise RuntimeError('완성 길이·이음 여유 검증 실패')
    s=plan['summary'];s.update(bar_count=sum(o['quantity'] for o in orders.values()),piece_count=sum(confirmed.values()),
        group_count=len({(p['material'],p['spec']) for p in plan['patterns']}),
        input_piece_count=sum(i.quantity for i in request.items),pending_piece_count=sum(i.quantity for i in request.items)-sum(confirmed.values()),
        excluded_piece_count=0,excluded_rows=0,total_stock_length=mm(totals['stock']),
        total_new_stock_length=mm(totals['stock']-totals['inventory']),inventory_input_length=mm(totals['inventory']),
        total_parts_length=mm(totals['parts']),total_kerf_loss=mm(totals['kerf']),total_remnant=mm(totals['remnant']),
        finished_parts_length=mm(finished),total_splice_allowance=mm(joint_allowance),
        loss_rate=round(100*(totals['stock']-finished)/totals['stock'],2) if totals['stock'] else 0,
        net_loss_rate=round(100*(totals['stock']-finished)/totals['stock'],2) if totals['stock'] else 0,
        inventory_bar_count=sum(used_inventory.values()),cut_piece_count=sum(c['quantity'] for c in plan['cut_bom']))
    s['inventory_used_length']=mm(totals['inventory_used'])
    plan['orders']=list(orders.values())
    plan['inventory_usage']=[{'id':key,'material':stock_by_id[key].material,'spec':stock_by_id[key].spec,
        'length':float(stock_by_id[key].length),'used_quantity':q,'remaining_quantity':stock_by_id[key].quantity-q,
        **{field:mm(value) for field,value in inventory_totals[key].items()},
        'confirmed':True} for key,q in used_inventory.items()]


def _comparison(label,plan,quantity,total):
    s=plan['summary']
    return {'label':label,'bar_count':s['bar_count'],'total_new_stock_length':s.get('total_new_stock_length',s['total_stock_length']),
        'inventory_input_length':s.get('inventory_input_length',0),'loss_rate':s['loss_rate'],
        'inventory_used_length':s.get('inventory_used_length',0),
        'confirmed_quantity':quantity,'pending_quantity':total-quantity,'optimal':s['optimal'],
        'max_stock_length':max((p['stock_length'] for p in plan['patterns']),default=0)}


def plan_manufacturing(request, settings):
    started=time.monotonic();saved=proposals(request,settings)
    choices={c['id']:c for c in saved['candidates']}
    if any(key not in choices or not choices[key]['eligible_for_approval'] for key in request.approved_candidate_ids):
        raise ValueError('미확인 재고 또는 유효하지 않은 후보는 승인·확정할 수 없습니다.')
    items=[i.model_copy(update={'input_index':n}) for n,i in enumerate(request.items)]
    remaining=Counter({n:i.quantity for n,i in enumerate(items)})
    confirmed=Counter();components=Counter();component_lengths=Counter();cut_bom=[];locked=[];joined=[]
    for key in request.approved_candidate_ids:
        c=choices[key]
        for a in c['allocations']:
            i,q=a['input_index'],a['quantity']
            if remaining[i]<q:raise ValueError('승인 후보가 같은 BOM 수량을 중복 사용합니다. 겹치는 승인을 해제해 주세요.')
            remaining[i]-=q;confirmed[i]+=q
        if c['kind']=='SHORT_REMNANT_PATTERN':
            p=copy.deepcopy(c['pattern']);p['approval_id']=key;locked.append(p)
            for a in c['allocations']:
                i,q=a['input_index'],a['quantity'];item=items[i]
                cut_bom.append({**item.model_dump(mode='json'),'quantity':q})
                components[i]+=q;component_lengths[i]+=units(item.length)*q
        else:
            i=c['input_index'];q=c['quantity'];item=items[i]
            connector=item.model_copy(update={'length':Decimal(str(c['connector_length'])),
                'quantity':q,'weight':'','required_inventory_id':c['inventory_id']})
            cut_bom.append({**connector.model_dump(mode='json'),'role':'연결재','assembly_length':float(item.length)})
            cut_bom.append({**item.model_dump(mode='json'),'length':str(c['base_length']),'quantity':q,'weight':'',
                            'role':'본재','assembly_length':float(item.length)})
            locked.append({'material':item.material,'spec':item.spec,'material_type':classify_material(item.spec),
                'stock_length':c['base_length'],'bar_count':q,'cuts':[{'length':c['base_length'],'quantity':1}],
                'uncut':True,'stock_source':'new','approval_id':key})
            components[i]+=2*q
            component_lengths[i]+=(units(c['base_length'])+units(c['connector_length']))*q
            joined.append({**c,'status':'이음 위치·연결 상세 사용자 승인','review_required':False})
    solve_items=[]
    for c in cut_bom:
        if c.get('role')=='연결재':solve_items.append(BOMItem(**c))
    for n,item in enumerate(items):
        if remaining[n] and n in saved['normal_indices']:
            q=remaining[n];confirmed[n]+=q
            model=item.model_copy(update={'quantity':q});solve_items.append(model)
            cut_bom.append(model.model_dump(mode='json'));components[n]+=q;component_lengths[n]+=units(item.length)*q
    inventory=_inventory(request)
    if not request.approved_candidate_ids and not inventory:
        plan=copy.deepcopy(saved['baseline'])
    else:
        plan=_solve(request,solve_items,saved['lengths'],inventory=inventory)
        if not request.approved_candidate_ids:
            if _purchase_score(saved['baseline'])<_purchase_score(plan):
                plan=copy.deepcopy(saved['baseline'])
                plan['summary']['optimal']=False
    for row in cut_bom:row['weight_basis_quantity']=items[row['input_index']].quantity
    plan['patterns']=locked+plan['patterns']
    # Restricted connector sources are allocated first in the shared report.
    plan['patterns'].sort(key=lambda p:not bool(p.get('inventory_id')))
    plan['cut_bom']=cut_bom;plan['excluded']=[]
    plan['expected_components']={i:{'quantity':components[i],'length':component_lengths[i]} for i in range(len(items))}
    allowance=sum(units(c['allowance'])*c['quantity'] for c in joined)
    _summarize(plan,request,confirmed,allowance)
    plan['bom']=[{**item.model_dump(mode='json'),'included':bool(confirmed[n]),'confirmed_quantity':confirmed[n],
        'pending_quantity':item.quantity-confirmed[n],'material_type':classify_material(item.spec) or '미지원'} for n,item in enumerate(items)]
    reasons={r['input_index']:r for r in saved['reviews']}
    plan['pending']=[{**_row(item,n),'quantity':item.quantity-confirmed[n],
        'code':reasons.get(n,{}).get('code','REVIEW_REQUIRED'),
        'reason':reasons.get(n,{}).get('reason','미승인 제작 조건')} for n,item in enumerate(items) if item.quantity>confirmed[n]]
    plan['manufacturing']={'proposal_token':saved['token'],'approved_candidate_ids':request.approved_candidate_ids,
        'candidates':[{k:v for k,v in c.items() if k not in ('pattern','allocations')} for c in saved['candidates']],
        'approved_splices':joined,'firm_orders_only':True,'inventory_entered':bool(request.remnant_inventory),
        'inventory':[r.model_dump(mode='json') for r in request.remnant_inventory],
        'transport_max_length':float(request.transport_max_length) if request.transport_max_length else None,
        'processing_max_length':float(request.processing_max_length) if request.processing_max_length else None}
    total=sum(i.quantity for i in items);normal_qty=sum(i.quantity for n,i in enumerate(items) if n in saved['normal_indices'])
    count_first=saved['count_first']
    plan['comparison']=[_comparison('기본 · 신규 총길이 우선',saved['baseline'],normal_qty,total),
        _comparison('비교 · 발주 본수 우선',count_first,normal_qty,total),
        _comparison('현장 · 확인 재고/승인 조건 적용',plan,sum(confirmed.values()),total)]
    plan['summary']['elapsed_seconds']=round(time.monotonic()-started,2)
    plan['settings'].update(weight_basis=request.weight_basis,splice_allowance=float(request.splice_allowance),
        reusable_remnant_min=float(request.reusable_remnant_min),objective='신규 발주 총길이 최소 → 신규 발주 본수 최소')
    plan['report_metadata']={k:getattr(request,k) for k in ('project_name','project_section','source_filename')}
    plan['report_metadata']['report_date']=request.report_date.isoformat()
    if request.import_audit:plan['import_audit']=request.import_audit.model_dump(mode='json')
    from .reports import build_report
    plan['report']=build_report(plan)
    return plan
