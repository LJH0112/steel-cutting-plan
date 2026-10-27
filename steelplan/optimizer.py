"""Integer cutting-stock model. One stock bar must leave strictly more than 30mm."""
import time
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


def greedy(lengths, demands, stocks, kerf):
    bars = []
    for i in sorted(range(len(lengths)), key=lambda j: -lengths[j]):
        size = lengths[i] + kerf
        for _ in range(demands[i]):
            candidates = [(b['capacity'] - b['used'] - size, j) for j, b in enumerate(bars)
                          if b['capacity'] - b['used'] >= size]
            if candidates:
                _, j = min(candidates)
                bar = bars[j]
            else:
                bar = {'capacity': stocks[-1] - MIN_REMAINDER, 'used': 0, 'counts': [0] * len(lengths)}
                bars.append(bar)
            bar['used'] += size
            bar['counts'][i] += 1
    for bar in bars:
        bar['stock'] = next(s for s in stocks if s - bar['used'] >= MIN_REMAINDER)
    return sorted(bars, key=lambda b: (-b['stock'], -b['used']))


def solve_group(lengths, demands, stocks, kerf, seconds):
    initial = greedy(lengths, demands, stocks, kerf)
    n, count = len(lengths), len(initial)
    # Bound model memory for large BOMs; the valid initial plan remains available.
    if count * (n + len(stocks)) > 30000 or seconds <= 0:
        return initial, 'FEASIBLE', '모델 크기 또는 전체 시간 제한: 검증된 휴리스틱 조합', None

    model = cp_model.CpModel()
    choices, cuts, loads, active = [], [], [], []
    for b in range(count):
        y = [model.new_bool_var(f's_{b}_{s}') for s in range(len(stocks))]
        on = model.new_bool_var(f'on_{b}')
        model.add(sum(y) == on)
        x = [model.new_int_var(0, min(demands[i], (stocks[-1] - MIN_REMAINDER) // (lengths[i] + kerf)),
                               f'x_{b}_{i}') for i in range(n)]
        load = sum((lengths[i] + kerf) * x[i] for i in range(n))
        capacity = sum((s - MIN_REMAINDER) * y[j] for j, s in enumerate(stocks))
        model.add(load <= capacity)
        model.add(sum(x) >= on)
        choices.append(y)
        cuts.append(x)
        loads.append(load)
        active.append(on)
        model.add_hint(on, 1)
        for j, s in enumerate(stocks):
            model.add_hint(y[j], int(s == initial[b]['stock']))
        for i in range(n):
            model.add_hint(x[i], initial[b]['counts'][i])
        if b:
            model.add(sum(s * choices[b - 1][j] for j, s in enumerate(stocks)) >=
                      sum(s * y[j] for j, s in enumerate(stocks)))
    for i in range(n):
        model.add(sum(cuts[b][i] for b in range(count)) == demands[i])
    # A single extra stock bar costs more than every possible length saving.
    weight = count * stocks[-1] + 1
    total_length = sum(s * choices[b][j] for b in range(count) for j, s in enumerate(stocks))
    model.minimize(weight * sum(active) + total_length)
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
            result.append({'stock': next(s for j, s in enumerate(stocks) if solver.value(choices[b][j])),
                           'counts': [solver.value(cuts[b][i]) for i in range(n)]})
    proven = status == cp_model.OPTIMAL
    return result, 'OPTIMAL' if proven else 'FEASIBLE', '최적해 증명 완료' if proven else '시간 내 최선의 조합 (최적해 미확정)', round(solver.best_objective_bound, 3)


def optimize(request: OptimizeRequest, stock_lengths_by_type=None):
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
        for length in demand:
            if length + kerf + MIN_REMAINDER > max(stocks):
                raise ValueError(f'{material} / {spec}: {mm(length):g}mm 부재는 발주 길이에 들어가지 않습니다. '
                                 '절단 손실과 30mm 초과 잔재를 고려해 더 긴 발주 길이를 지정해 주세요.')
    orders, patterns, statuses = [], [], []
    total_stock = total_parts = total_kerf = total_remnant = 0
    for index, ((material, spec), demand) in enumerate(sorted(grouped.items())):
        stocks = group_stocks(spec)
        kind = classify_material(spec) or '미지원 자재'
        lengths = sorted(demand, reverse=True)
        demands = [demand[v] for v in lengths]
        remaining = max(0, request.time_limit - (time.monotonic() - started))
        allowance = remaining / (len(grouped) - index)
        bars, status, detail, _ = solve_group(lengths, demands, stocks, kerf, allowance)
        # Independent invariant checks protect every output including fallback results.
        actual = [sum(bar['counts'][i] for bar in bars) for i in range(len(lengths))]
        if actual != demands:
            raise RuntimeError('부재 수량 검증 실패')
        statuses.append({'material': material, 'spec': spec, 'material_type': kind,
                         'allowed_lengths': [mm(s) for s in stocks], 'status': status, 'detail': detail})
        identical = Counter((b['stock'], tuple(b['counts'])) for b in bars)
        by_stock = defaultdict(lambda: {'quantity': 0, 'parts_length': 0, 'kerf_loss': 0, 'remnant': 0})
        for (stock, counts), repeats in sorted(identical.items(), reverse=True):
            piece_count = sum(counts)
            parts = sum(l * q for l, q in zip(lengths, counts))
            loss = kerf * piece_count
            remnant = stock - parts - loss
            if remnant < MIN_REMAINDER or stock not in stocks:
                raise RuntimeError('잔재 또는 발주 길이 검증 실패')
            patterns.append({'id': f'CP-{len(patterns) + 1:03d}', 'material': material, 'spec': spec, 'material_type': kind,
                             'stock_length': mm(stock), 'bar_count': repeats,
                             'cuts': [{'length': mm(l), 'quantity': q} for l, q in zip(lengths, counts) if q],
                             'piece_count': piece_count, 'parts_length': mm(parts), 'kerf_loss': mm(loss),
                             'remnant': mm(remnant), 'loss_rate': round(100 * (stock - parts) / stock, 2)})
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
                        'loss_rate': round(100 * (total_stock - total_parts) / total_stock, 2),
                        'optimal': all(s['status'] == 'OPTIMAL' for s in statuses),
                        'elapsed_seconds': round(time.monotonic() - started, 2)},
            'settings': {'stock_lengths': sorted({float(s) for s in request.stock_lengths}) if stock_lengths_by_type is None
                         else sorted({float(s) for values in stock_lengths_by_type.values() for s in values}),
                         'stock_lengths_by_type': {k: [float(s) for s in v] for k, v in stock_lengths_by_type.items()}
                         if stock_lengths_by_type is not None else {}, 'kerf': float(request.kerf),
                         'minimum_remnant_exclusive': 30, 'time_limit': request.time_limit,
                         'objective': '발주 본수 최소 → 총 발주 길이 최소 (동일 본수에서 로스 최소)'},
            'orders': orders, 'patterns': patterns, 'statuses': statuses,
            'bom': [i.model_dump(mode='json') for i in request.items]}
