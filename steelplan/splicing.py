"""Provisional splice planning around the unchanged cutting engine.

Whole 6m base bars have no saw cut. Every actual cut still leaves >30mm.
Available, matching-section remnants are consumed before new connector stock.
"""
import time
from collections import Counter, defaultdict
from decimal import Decimal

from .materials import classify_material
from .models import BOMItem
from .optimizer import MIN_REMAINDER, mm, optimize, units


def needs_splice(item):
    return classify_material(item.spec) == '각파이프' and item.length > 6000


def splice_layout(length, allowance, kerf):
    target, extra, stock = units(length), units(allowance), units(6000)
    # k whole bars joined with k-1 allowances, e.g. 11995 = 6000*2 - 5.
    whole, remainder = divmod(target - extra, stock - extra)
    if remainder == 0 and whole >= 2:
        return whole, 0, whole - 1
    max_tail = stock - units(kerf) - MIN_REMAINDER
    count = max(1, (target - max_tail + (stock - extra) - 1) // (stock - extra))
    tail = target - count * (stock - extra)
    if not 0 < tail <= max_tail:
        raise ValueError('이음 제작 길이 조합을 만들 수 없습니다. 제작 여유와 날 손실을 확인해 주세요.')
    return count, tail, count


def _pattern(material, spec, stock, counts, repeats=1, uncut=False):
    parts = sum(length * qty for length, qty in counts.items())
    return {'material': material, 'spec': spec, 'material_type': classify_material(spec),
            'stock_length': mm(stock), 'bar_count': repeats,
            'cuts': [{'length': mm(length), 'quantity': qty} for length, qty in sorted(counts.items(), reverse=True)],
            'piece_count': sum(counts.values()), 'parts_length': mm(parts), 'uncut': uncut}


def optimize_with_splices(request, settings):
    started = time.monotonic()
    normal = [i for i in request.items if not needs_splice(i)]
    joined = [i for i in request.items if needs_splice(i)]
    if not joined:
        return optimize(request, settings.stock_lengths_by_type)
    allowance, kerf = request.splice_allowance, units(request.kerf)
    cut_bom = [i.model_dump(mode='json') for i in normal]
    assemblies, tails, whole_counts = [], [], Counter()
    for item in joined:
        full, tail, joints = splice_layout(item.length, allowance, request.kerf)
        row = item.model_dump(mode='json')
        meta = {'assembly_length': float(item.length), 'weight': ''}
        cut_bom.append({**row, **meta, 'length': '6000', 'role': '본재', 'quantity': item.quantity})
        if full > 1:
            cut_bom.append({**row, **meta, 'length': '6000', 'role': '연결재(통재)', 'quantity': item.quantity * (full - 1)})
        whole_counts[(item.material, item.spec)] += full * item.quantity
        if tail:
            tails.append(BOMItem(**{**row, 'length': Decimal(tail) / 1000, 'weight': ''}))
            cut_bom.append({**row, **meta, 'length': str(Decimal(tail) / 1000), 'role': '연결재', 'quantity': item.quantity})
        assemblies.append({'row': item.source_row, 'input_index': item.input_index,
                           'title': item.title, 'material': item.material, 'spec': item.spec,
                           'quantity': item.quantity, 'finished_length': float(item.length), 'base_length': 6000,
                           'whole_bars_per_part': full, 'connector_length': mm(tail) if tail else 6000,
                           'tail_connector_count': int(bool(tail)), 'whole_connector_count': full - 1,
                           'joint_count': joints, 'allowance_per_joint': float(allowance),
                           'status': '사용자 승인 입력됨' if request.splice_details_approved else '검토 필요',
                           'review_required': not request.splice_details_approved})
    if normal:
        plan = optimize(request.model_copy(update={'items': normal, 'time_limit': max(1, request.time_limit // 2)}),
                        settings.stock_lengths_by_type)
    else:
        plan = {'patterns': [], 'statuses': [], 'summary': {}, 'settings': {
            'stock_lengths': sorted({float(v) for values in settings.stock_lengths_by_type.values() for v in values}),
            'stock_lengths_by_type': {k: [float(v) for v in values] for k, values in settings.stock_lengths_by_type.items()},
            'kerf': float(request.kerf), 'minimum_remnant_exclusive': 30,
            'objective': '발주 본수 최소 → 총 발주 길이 최소 (잔재 우선 이음 검토 계획)'}}
    kept, ledger = [], []
    for p in plan['patterns']:
        if p['material_type'] != '각파이프':
            kept.append(p)
            continue
        for _ in range(p['bar_count']):
            ledger.append({'material': p['material'], 'spec': p['spec'], 'stock': units(p['stock_length']),
                           'counts': Counter({units(c['length']): c['quantity'] for c in p['cuts']}),
                           'remnant': units(p['remnant']), 'source': f'잔재-{len(ledger) + 1}'})
    remaining, reused = [], []
    for tail in sorted(tails, key=lambda i: -i.length):
        unfilled, size = 0, units(tail.length)
        for _ in range(tail.quantity):
            candidates = [(bar['remnant'], index) for index, bar in enumerate(ledger)
                          if (bar['material'], bar['spec']) == (tail.material, tail.spec)
                          and bar['remnant'] >= units(request.reusable_remnant_min)
                          and bar['remnant'] >= size + kerf + MIN_REMAINDER]
            if not candidates:
                unfilled += 1
                continue
            _, index = min(candidates)
            bar = ledger[index]
            before = bar['remnant']
            bar['counts'][size] += 1
            bar['remnant'] -= size + kerf
            reused.append({'source': bar['source'], 'row': tail.source_row, 'title': tail.title,
                           'material': tail.material, 'spec': tail.spec, 'length': mm(size), 'quantity': 1,
                           'before': mm(before), 'after': mm(bar['remnant'])})
        if unfilled:
            remaining.append(tail.model_copy(update={'quantity': unfilled}))
    for bar in ledger:
        p = _pattern(bar['material'], bar['spec'], bar['stock'], bar['counts'])
        p['source_bars'] = [bar['source']]
        kept.append(p)
    extra_bars = 0
    if remaining:
        budget = max(1, int(request.time_limit - (time.monotonic() - started)))
        additional = optimize(request.model_copy(update={'items': remaining, 'time_limit': budget}), settings.stock_lengths_by_type)
        extra_bars = additional['summary']['bar_count']
        kept.extend(additional['patterns'])
    for (material, spec), qty in sorted(whole_counts.items()):
        kept.append(_pattern(material, spec, units(6000), {units(6000): 1}, qty, uncut=True))
    # Recombine identical physical cutting patterns, preserving procurement counts.
    merged = {}
    for p in kept:
        key = (p['material'], p['spec'], p['stock_length'], bool(p.get('uncut')),
               tuple((c['length'], c['quantity']) for c in p['cuts']))
        if key in merged:
            merged[key]['bar_count'] += p['bar_count']
            merged[key].setdefault('source_bars', []).extend(p.get('source_bars', []))
        else:
            merged[key] = {**p, 'source_bars': list(p.get('source_bars', []))}
    patterns, orders, stock_total, parts_total, kerf_total, rem_total = [], {}, 0, 0, 0, 0
    for number, p in enumerate(merged.values(), 1):
        stock, parts = units(p['stock_length']), units(p['parts_length'])
        loss = 0 if p.get('uncut') else kerf * p['piece_count']
        remnant = stock - parts - loss
        if not p.get('uncut') and remnant < MIN_REMAINDER:
            raise RuntimeError('이음 연결재 절단 후 잔재 검증 실패')
        p.update(id=f'CP-{number:03d}', kerf_loss=mm(loss), remnant=mm(remnant),
                 loss_rate=round(100 * (stock - parts) / stock, 2))
        patterns.append(p)
        for record in reused:
            if record['source'] in p['source_bars']:
                record['pattern_id'] = p['id']
        qty = p['bar_count']
        stock_total += stock * qty; parts_total += parts * qty; kerf_total += loss * qty; rem_total += remnant * qty
        key = p['material'], p['spec'], p['stock_length']
        order = orders.setdefault(key, {'material': key[0], 'spec': key[1], 'material_type': p['material_type'],
                                       'stock_length': key[2], 'quantity': 0, 'total_length': 0,
                                       'parts_length': 0, 'kerf_loss': 0, 'remnant': 0})
        order['quantity'] += qty
        for field, value in [('total_length', stock), ('parts_length', parts), ('kerf_loss', loss), ('remnant', remnant)]:
            order[field] += value * qty
    for order in orders.values():
        order['loss_rate'] = round(100 * (order['kerf_loss'] + order['remnant']) / order['total_length'], 2)
        for field in ('total_length', 'parts_length', 'kerf_loss', 'remnant'):
            order[field] = mm(order[field])
    finished = sum(units(i.length) * i.quantity for i in request.items)
    joint_extra = sum(units(allowance) * a['joint_count'] * a['quantity'] for a in assemblies)
    if parts_total - joint_extra != finished:
        raise RuntimeError('이음 제작 여유 및 완성 길이 검증 실패')
    plan.update(patterns=patterns, orders=list(orders.values()), cut_bom=cut_bom, splices=assemblies, remnant_reuse=reused)
    plan['summary'] = {'bar_count': sum(o['quantity'] for o in orders.values()),
                       'piece_count': sum(i.quantity for i in request.items), 'cut_piece_count': sum(i['quantity'] for i in cut_bom),
                       'group_count': len({(i.material, i.spec) for i in request.items}),
                       'total_stock_length': mm(stock_total), 'total_parts_length': mm(parts_total),
                       'total_kerf_loss': mm(kerf_total), 'total_remnant': mm(rem_total),
                       'finished_parts_length': mm(finished), 'total_splice_allowance': mm(joint_extra),
                       'loss_rate': round(100 * (stock_total - parts_total) / stock_total, 2),
                       'net_loss_rate': round(100 * (stock_total - finished) / stock_total, 2),
                       'optimal': False, 'elapsed_seconds': round(time.monotonic() - started, 2)}
    plan['splice_summary'] = {'finished_quantity': sum(i.quantity for i in joined),
                              'whole_stock_bars': sum(whole_counts.values()), 'reused_connectors': len(reused),
                              'extra_connector_stock_bars': extra_bars,
                              'review_required': not request.splice_details_approved}
    plan['settings'].update(time_limit=request.time_limit, splice_allowance=float(allowance),
                            reusable_remnant_min=float(request.reusable_remnant_min),
                            splice_details_approved=request.splice_details_approved)
    existing = {(s['material'], s['spec']): s for s in plan['statuses']}
    for item in joined:
        existing[item.material, item.spec] = {'material': item.material, 'spec': item.spec, 'material_type': '각파이프',
            'status': 'FEASIBLE', 'detail': '이음 검토 계획: 잔재 우선 배정 후 부족 연결재 최적화 (전체 최적성 미확정)'}
    plan['statuses'] = list(existing.values())
    return plan
