"""Integer cutting-stock model. One stock bar must leave strictly more than 30mm."""
import time
from math import gcd
from functools import reduce
from collections import Counter, defaultdict
from decimal import Decimal

from ortools.sat.python import cp_model

from .models import OptimizeRequest
from .materials import classify_material

SCALE = 1000  # 0.001 mm; a 30.000 mm remainder is forbidden.
MIN_REMAINDER = 30 * SCALE + 1


def units(value):
    return int(Decimal(str(value)) * SCALE)


def mm(value):
    return round(value / SCALE, 3)


def greedy(lengths, demands, stocks, kerf, minimum_remnant=MIN_REMAINDER, inventory=(), reservations=None, smallest=False):
    bars, remaining = [], list(demands)
    available = {r['id']: r['quantity'] for r in inventory}
    by_id = {r['id']: r for r in inventory}

    def insert(i, required_id=None):
        size = lengths[i] + kerf
        candidates = [(b['capacity'] - b['used'] - size, j) for j,b in enumerate(bars)
                      if b['capacity']-b['used'] >= size and (required_id is None or b.get('inventory_id') == required_id)]
        if candidates:
            bar = bars[min(candidates)[1]]
        else:
            inv = [r for r in inventory if available[r['id']] and r['length'] >= size+minimum_remnant
                   and (required_id is None or r['id'] == required_id)]
            if inv:
                row = min(inv, key=lambda r: (r['length'], r['id']))
                available[row['id']] -= 1
                bar = {'capacity': row['length']-minimum_remnant, 'used':0, 'counts':[0]*len(lengths),
                       'inventory_id': row['id'], 'stock': row['length']}
            else:
                if required_id:
                    raise ValueError('승인한 연결재의 확인 재고가 부족하거나 승인 후보가 서로 충돌합니다.')
                fits = [s for s in stocks if s >= size+minimum_remnant]
                if not fits:
                    raise ValueError('절단 부재가 허용 발주 길이에 들어가지 않습니다.')
                capacity = fits[0] if smallest else fits[-1]
                bar = {'capacity':capacity-minimum_remnant,'used':0,'counts':[0]*len(lengths)}
            bars.append(bar)
        bar['used'] += size
        bar['counts'][i] += 1

    for (inventory_id, i), qty in sorted((reservations or {}).items()):
        if inventory_id not in by_id:
            raise ValueError('확인되지 않은 잔재를 연결재에 사용할 수 없습니다.')
        for _ in range(qty):
            insert(i, inventory_id)
            remaining[i] -= 1
    for i in sorted(range(len(lengths)), key=lambda j: -lengths[j]):
        for _ in range(remaining[i]): insert(i)
    for bar in bars:
        if not bar.get('inventory_id'):
            bar['stock'] = next(s for s in stocks if s-bar['used'] >= minimum_remnant)
    return sorted(bars, key=lambda b: (-b['stock'], -b['used']))


def solve_group(lengths, demands, stocks, kerf, seconds, objective='total_length', inventory=(), reservations=None, minimum_remnant=MIN_REMAINDER):
    def score(bars):
        fresh = [b for b in bars if not b.get('inventory_id')]
        pair = sum(b['stock'] for b in fresh), len(fresh)
        return pair if objective == 'total_length' else pair[::-1]
    alternatives = [greedy(lengths, demands, stocks, kerf, minimum_remnant, inventory, reservations, small) for small in (False,True)]
    if inventory and stocks and not reservations:
        alternatives += [greedy(lengths, demands, stocks, kerf, minimum_remnant, smallest=small) for small in (False,True)]
    initial = min(alternatives, key=score)
    n = len(lengths)
    # A length-optimal solution may use MORE bars than any greedy incumbent.
    # One part per bar is the safe upper bound; the model-size fallback below
    # must report FEASIBLE rather than falsely prove a truncated optimum.
    count = sum(demands) if objective=='total_length' else min(sum(demands),len(initial)+sum(r['quantity'] for r in inventory))
    stock_choices = [(s,None) for s in stocks]+[(r['length'],r['id']) for r in inventory]
    # Bound model memory for large BOMs; the valid initial plan remains available.
    if count * (n + len(stock_choices)) > 30000 or seconds <= 0:
        return initial, 'FEASIBLE', '모델 크기 또는 전체 시간 제한: 검증된 휴리스틱 조합', None

    model = cp_model.CpModel()
    choices, cuts, loads, active = [], [], [], []
    for b in range(count):
        y = [model.new_bool_var(f's_{b}_{s}') for s in range(len(stock_choices))]
        on = model.new_bool_var(f'on_{b}')
        model.add(sum(y) == on)
        largest = max(s for s,_ in stock_choices)
        x = [model.new_int_var(0, min(demands[i], max(0,(largest-minimum_remnant)//(lengths[i]+kerf))),
                               f'x_{b}_{i}') for i in range(n)]
        load = sum((lengths[i] + kerf) * x[i] for i in range(n))
        capacity = sum((s-minimum_remnant)*y[j] for j,(s,_) in enumerate(stock_choices))
        model.add(load <= capacity)
        model.add(sum(x) >= on)
        choices.append(y)
        cuts.append(x)
        loads.append(load)
        active.append(on)
        hint = initial[b] if b < len(initial) else None
        model.add_hint(on, int(hint is not None))
        for j,(s,inventory_id) in enumerate(stock_choices):
            model.add_hint(y[j], int(hint is not None and (s,inventory_id)==(hint['stock'],hint.get('inventory_id'))))
        for i in range(n):
            model.add_hint(x[i], hint['counts'][i] if hint else 0)
        if b:
            model.add(sum(s*choices[b-1][j] for j,(s,_) in enumerate(stock_choices)) >=
                      sum(s*y[j] for j,(s,_) in enumerate(stock_choices)))
    for i in range(n):
        model.add(sum(cuts[b][i] for b in range(count)) == demands[i])
    for j,row in enumerate(inventory,len(stocks)):
        model.add(sum(choices[b][j] for b in range(count)) <= row['quantity'])
        for (inventory_id,i),qty in (reservations or {}).items():
            if inventory_id == row['id']:
                reserved = []
                for b in range(count):
                    value = model.new_int_var(0,demands[i],f'reserved_{j}_{i}_{b}')
                    model.add(value==cuts[b][i]).only_enforce_if(choices[b][j])
                    model.add(value==0).only_enforce_if(choices[b][j].Not())
                    reserved.append(value)
                model.add(sum(reserved)>=qty)
    # Exact lexicographic priorities, bounded in integer units to avoid floats.
    divisor = reduce(gcd,stocks,0) or 1
    total_length = sum((s//divisor)*choices[b][j] for b in range(count) for j,s in enumerate(stocks))
    fresh_count = sum(choices[b][j] for b in range(count) for j in range(len(stocks)))
    model.minimize(total_length*(count+1)+fresh_count if objective=='total_length'
                   else fresh_count*(count*max(stocks,default=0)//divisor+1)+total_length)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.05, seconds)
    solver.parameters.num_search_workers = 2
    solver.parameters.random_seed = 42
    status = solver.solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        if status in (cp_model.INFEASIBLE, cp_model.MODEL_INVALID):
            raise RuntimeError('절단 모델 검증에 실패했습니다. 입력값과 계산 로그를 확인해 주세요.')
        return initial, 'FEASIBLE', '시간 제한: 검증된 초기 조합을 반환했습니다.', None
    result = []
    for b in range(count):
        if solver.value(active[b]):
            stock,inventory_id = next(choice for j,choice in enumerate(stock_choices) if solver.value(choices[b][j]))
            result.append({'stock':stock,'counts':[solver.value(cuts[b][i]) for i in range(n)],
                           **({'inventory_id':inventory_id} if inventory_id else {})})
    proven = status == cp_model.OPTIMAL
    return result, 'OPTIMAL' if proven else 'FEASIBLE', '최적해 증명 완료' if proven else '시간 내 최선의 조합 (최적해 미확정)', round(solver.best_objective_bound, 3)


def optimize(request: OptimizeRequest, stock_lengths_by_type=None, inventory=(), minimum_remnant=MIN_REMAINDER):
    started = time.monotonic()
    if sum(item.quantity for item in request.items) > 5000:
        raise ValueError('총 부재 수량은 5,000개 이하여야 합니다.')
    grouped = defaultdict(Counter)
    for item in request.items:
        grouped[(item.material, item.spec)][units(item.length)] += item.quantity
    if len(grouped) > 100:
        raise ValueError('재질·규격 그룹은 최대 100개까지 지원합니다.')
    if stock_lengths_by_type is None and request.stock_lengths is None:
        raise ValueError('발주 길이 설정이 필요합니다.')
    kerf = units(request.kerf)

    def group_stocks(spec):
        values = stock_lengths_by_type[classify_material(spec)] if stock_lengths_by_type is not None else request.stock_lengths
        return [units(s) for s in values]

    for (material, spec), demand in grouped.items():
        stocks = group_stocks(spec)
        possible = stocks+[units(r.length) for r in inventory if r.confirmed and (r.material,r.spec)==(material,spec)]
        for length in demand:
            if not possible or length+kerf+minimum_remnant > max(possible):
                raise ValueError(f'{material} / {spec}: {mm(length):g}mm 부재는 발주 길이에 들어가지 않습니다. '
                                 '절단 손실과 30mm 초과 잔재를 고려해 더 긴 발주 길이를 지정해 주세요.')
    orders, patterns, statuses = [], [], []
    total_stock = total_parts = total_kerf = total_remnant = inventory_total = inventory_count = 0
    for index, ((material, spec), demand) in enumerate(sorted(grouped.items())):
        stocks = group_stocks(spec)
        kind = classify_material(spec) or '미지원 자재'
        lengths = sorted(demand, reverse=True)
        demands = [demand[v] for v in lengths]
        group_inventory = [{'id':r.id,'length':units(r.length),'quantity':r.quantity} for r in inventory
                           if r.confirmed and (r.material,r.spec)==(material,spec)]
        reservations = Counter()
        for item in request.items:
            if (item.material,item.spec)==(material,spec) and item.required_inventory_id:
                reservations[item.required_inventory_id,lengths.index(units(item.length))] += item.quantity
        remaining = max(0, request.time_limit - (time.monotonic() - started))
        allowance = remaining / (len(grouped) - index)
        bars, status, detail, _ = solve_group(lengths, demands, stocks, kerf, allowance, request.objective,
                                              group_inventory, reservations, minimum_remnant)
        # Independent invariant checks protect every output including fallback results.
        actual = [sum(bar['counts'][i] for bar in bars) for i in range(len(lengths))]
        if actual != demands:
            raise RuntimeError('부재 수량 검증 실패')
        statuses.append({'material': material, 'spec': spec, 'material_type': kind,
                         'allowed_lengths': [mm(s) for s in stocks], 'status': status, 'detail': detail})
        for (inventory_id,i),qty in reservations.items():
            if sum(b['counts'][i] for b in bars if b.get('inventory_id')==inventory_id)<qty:
                raise RuntimeError('연결재 재고 배정 검증 실패')
        used_ids = Counter(b.get('inventory_id') for b in bars if b.get('inventory_id'))
        if any(used_ids[r['id']] > r['quantity'] for r in group_inventory):
            raise RuntimeError('재고 수량 초과')
        identical = Counter((b['stock'], b.get('inventory_id',''), tuple(b['counts'])) for b in bars)
        by_stock = defaultdict(lambda: {'quantity': 0, 'parts_length': 0, 'kerf_loss': 0, 'remnant': 0})
        for (stock, inventory_id, counts), repeats in sorted(identical.items(), reverse=True):
            piece_count = sum(counts)
            parts = sum(l * q for l, q in zip(lengths, counts))
            loss = kerf * piece_count
            remnant = stock - parts - loss
            allowed_inventory = {r['id']:r['length'] for r in group_inventory}
            if remnant < minimum_remnant or (allowed_inventory.get(inventory_id)!=stock if inventory_id else stock not in stocks):
                raise RuntimeError('잔재 또는 발주 길이 검증 실패')
            patterns.append({'id': f'CP-{len(patterns) + 1:03d}', 'material': material, 'spec': spec, 'material_type': kind,
                             'stock_length': mm(stock), 'bar_count': repeats,
                             'stock_source':'inventory' if inventory_id else 'new', 'inventory_id':inventory_id or None,
                             'cuts': [{'length': mm(l), 'quantity': q} for l, q in zip(lengths, counts) if q],
                             'piece_count': piece_count, 'parts_length': mm(parts), 'kerf_loss': mm(loss),
                             'remnant': mm(remnant), 'loss_rate': round(100 * (stock - parts) / stock, 2)})
            if inventory_id:
                inventory_total += stock*repeats
                inventory_count += repeats
            else:
                row = by_stock[stock]
                for key, value in [('quantity', 1), ('parts_length', parts), ('kerf_loss', loss), ('remnant', remnant)]:
                    row[key] += value * repeats
            total_stock += stock * repeats
            total_parts += parts * repeats
            total_kerf += loss * repeats
            total_remnant += remnant * repeats
        for stock, row in sorted(by_stock.items()):
            orders.append({'material': material, 'spec': spec, 'material_type': kind, 'stock_length': mm(stock),
                           'quantity': row['quantity'], 'total_length': mm(stock * row['quantity']),
                           'parts_length': mm(row['parts_length']), 'kerf_loss': mm(row['kerf_loss']),
                           'remnant': mm(row['remnant']),
                           'loss_rate': round(100 * (row['kerf_loss'] + row['remnant']) / (stock * row['quantity']), 2)})
    return {'summary': {'bar_count': sum(o['quantity'] for o in orders),
                        'piece_count': sum(i.quantity for i in request.items), 'group_count': len(grouped),
                        'total_stock_length': mm(total_stock), 'total_parts_length': mm(total_parts),
                        'total_kerf_loss': mm(total_kerf), 'total_remnant': mm(total_remnant),
                        'loss_rate': round(100*(total_stock-total_parts)/total_stock,2) if total_stock else 0,
                        'total_new_stock_length':mm(total_stock-inventory_total),
                        'inventory_input_length':mm(inventory_total), 'inventory_bar_count':inventory_count,
                        'optimal': all(s['status'] == 'OPTIMAL' for s in statuses),
                        'elapsed_seconds': round(time.monotonic() - started, 2)},
            'settings': {'stock_lengths': sorted({float(s) for s in request.stock_lengths}) if stock_lengths_by_type is None
                         else sorted({float(s) for values in stock_lengths_by_type.values() for s in values}),
                         'stock_lengths_by_type': {k: [float(s) for s in v] for k, v in stock_lengths_by_type.items()}
                         if stock_lengths_by_type is not None else {}, 'kerf': float(request.kerf),
                         'minimum_remnant_exclusive': 30, 'time_limit': request.time_limit,
                         'objective': '신규 발주 총길이 최소 → 신규 발주 본수 최소' if request.objective=='total_length' else '발주 본수 최소 → 총 발주 길이 최소'},
            'orders': orders, 'patterns': patterns, 'statuses': statuses,
            'bom': [i.model_dump(mode='json') for i in request.items]}
