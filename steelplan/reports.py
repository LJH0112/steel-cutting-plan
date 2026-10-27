"""Shared, quantity-checked tables for the screen, Excel and PDF."""
from collections import Counter, defaultdict, deque
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fractions import Fraction

from .optimizer import units

MATERIAL_COLUMNS = [
    ('material', '재질'), ('spec', '규격'), ('length', '길이'), ('quantity', '수량'),
    ('total_weight', '총중량,KG'), ('combined_length', '조합길이'),
    ('combined_weight', '조합중량,KG'), ('remnant', '잔철'), ('utilization', '사용율'), ('remarks', '비고'),
]
COMBINATION_COLUMNS = [
    ('number', '번호'), ('stock_length', '원자재'), ('quantity', '수량'), ('combined_length', '조합길이'),
    ('remnant', '잔철'), ('utilization', '사용율'), ('length', '길이'),
    ('split_count', '등분'), ('total_quantity', '총수량'), ('total_weight', '총중량,KG'), ('remarks', '비고'),
]


def rounded(value, digits=3):
    if value is None:
        return None
    number = Decimal(value.numerator) / Decimal(value.denominator) if isinstance(value, Fraction) else Decimal(str(value))
    return float(number.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))


def known_weight(item, basis):
    if basis == 'unspecified':
        return None
    try:
        value = Decimal(''.join(str(item.get('weight', '')).split()).replace(',', ''))
        if not value.is_finite() or not 0 <= value <= 10**12:
            return None
        weight = Fraction(value)
        return weight / item['quantity'] if basis == 'row_total' else weight
    except (ValueError, InvalidOperation, ZeroDivisionError):
        return None


def complete_sum(values):
    values = list(values)
    return None if not values or any(v is None for v in values) else sum(values, Fraction(0))


def exclusion_reason(issue):
    return '발주 길이 초과' if issue['code'] == 'STOCK_LENGTH_EXCEEDED' else issue['reason']


def build_report(plan):
    basis = plan['settings'].get('weight_basis', 'unspecified')
    pool = defaultdict(deque)
    source = plan.get('cut_bom', plan['bom'])
    for index, item in enumerate(source):
        if item.get('included', True):
            pool[(item['material'], item['spec'], units(item['length']))].append({
                'index': index, 'row': item.get('source_row') or index + 1,
                'input_index': item.get('input_index') if item.get('input_index') is not None else index,
                'title': item.get('title') or f"원본 {item.get('source_row') or index + 1}행",
                'remaining': item['quantity'], 'weight': known_weight(item, basis),
                'role': item.get('role', ''), 'assembly_length': item.get('assembly_length'),
            })
    groups, weights = {}, defaultdict(list)
    produced = 0
    allocations_by_source = defaultdict(int)
    allocated_quantity, allocated_length = Counter(), Counter()
    group_weights = defaultdict(list)
    for number, pattern in enumerate(plan['patterns'], 1):
        key = pattern['material'], pattern['spec']
        group = groups.setdefault(key, {'material': key[0], 'spec': key[1],
                                       'material_type': pattern['material_type'], 'patterns': []})
        p = {'number': number, 'id': pattern['id'], 'stock_length': pattern['stock_length'],
             'quantity': pattern['bar_count'], 'combined_length': pattern['parts_length'],
             'remnant': pattern['remnant'], 'utilization': rounded(100 * Fraction(units(pattern['parts_length']), units(pattern['stock_length'])), 2),
             'kerf_loss': pattern['kerf_loss'], 'cuts': [], 'uncut': pattern.get('uncut', False)}
        pattern_weights = []
        for cut in pattern['cuts']:
            count = cut['quantity'] * pattern['bar_count']
            remaining, assigned, cut_weights = count, [], []
            queue = pool[(key[0], key[1], units(cut['length']))]
            while remaining:
                if not queue:
                    raise ValueError('출력 수량 검증 실패: BOM보다 많은 절단 부재가 있습니다.')
                row = queue[0]
                take = min(remaining, row['remaining'])
                assigned.append({'source_index': row['index'], 'row': row['row'], 'title': row['title'],
                                 'input_index': row['input_index'],
                                 'quantity': take, 'role': row['role'], 'assembly_length': row['assembly_length']})
                mass = None if row['weight'] is None else row['weight'] * take
                pattern_weights.append(mass)
                cut_weights.append(mass)
                allocations_by_source[row['index']] += take
                allocated_quantity[row['input_index']] += take
                allocated_length[row['input_index']] += units(cut['length']) * take
                remaining -= take
                row['remaining'] -= take
                if row['remaining'] == 0:
                    queue.popleft()
            remarks = '; '.join(f"{a['title']} × {a['quantity']}" +
                (f" [{a['role']} {cut['length']:g}mm, 완성 {a['assembly_length']:g}mm]" if a['role'] else '') for a in assigned)
            p['cuts'].append({'length': cut['length'], 'split_count': cut['quantity'],
                              'total_quantity': count, 'total_weight': rounded(complete_sum(cut_weights)),
                              'remarks': remarks, 'allocations': assigned})
            produced += count
        weights[(key[0], key[1], pattern['stock_length'])].append(complete_sum(pattern_weights))
        group_weights[key].append(complete_sum(pattern_weights))
        group['patterns'].append(p)
    if any(queue for queue in pool.values()):
        raise ValueError('출력 수량 검증 실패: BOM에 계산되지 않은 부재가 있습니다.')
    expected = sum(i['quantity'] for i in source if i.get('included', True))
    if expected != produced:
        raise ValueError('출력 부재 수량이 BOM과 일치하지 않습니다.')

    rows = []
    by_spec = defaultdict(list)
    for order in plan['orders']:
        mass = complete_sum(weights[(order['material'], order['spec'], order['stock_length'])])
        row = {'kind': 'detail', 'material': order['material'], 'spec': order['spec'],
               'length': order['stock_length'], 'quantity': order['quantity'], 'total_weight': None,
               'combined_length': order['parts_length'], 'combined_weight': rounded(mass),
               'remnant': order['remnant'], 'utilization': rounded(100 * Fraction(units(order['parts_length']), units(order['total_length'])), 2),
               'stock_total': order['total_length'], '_mass': mass, 'remarks': ''}
        by_spec[(row['material'], row['spec'])].append(row)

    def total(details, material, spec, kind):
        stock = sum(units(d['stock_total']) for d in details)
        parts = sum(units(d['combined_length']) for d in details)
        return {'kind': kind, 'material': material, 'spec': spec, 'length': stock / 1000,
                'quantity': sum(d['quantity'] for d in details), 'total_weight': None,
                'combined_length': parts / 1000, 'combined_weight': rounded(complete_sum(d['_mass'] for d in details)),
                'remnant': rounded(sum(Decimal(str(d['remnant'])) for d in details)),
                'utilization': rounded(100 * Fraction(parts, stock), 2) if stock else 0, 'remarks': ''}

    details = []
    for (material, spec), group_rows in sorted(by_spec.items()):
        rows.extend(sorted(group_rows, key=lambda r: r['length']))
        rows.append(total(group_rows, material, spec + ' 소계', 'subtotal'))
        details.extend(group_rows)
    rows.append(total(details, '전체 합계', '', 'grand_total'))
    for row in rows:
        row.pop('_mass', None)
    for key, group in groups.items():
        bars = sum(p['quantity'] for p in group['patterns'])
        stock = sum(units(p['stock_length']) * p['quantity'] for p in group['patterns'])
        combined = sum(units(p['combined_length']) * p['quantity'] for p in group['patterns'])
        group['unit_weight'] = None  # Never infer unit mass from a sample PDF or BOM.
        group['total'] = {'number': '합 계', 'stock_length': stock / 1000, 'quantity': bars,
                          'combined_length': combined / 1000,
                          'remnant': sum(units(p['remnant']) * p['quantity'] for p in group['patterns']) / 1000,
                          'utilization': rounded(100 * Fraction(combined, stock), 2), 'length': None,
                          'split_count': None, 'total_quantity': sum(c['total_quantity'] for p in group['patterns'] for c in p['cuts']),
                          'total_weight': rounded(complete_sum(group_weights[key])), 'remarks': ''}
    display_rows = []
    for row in rows:
        display = dict(row)
        if row['kind'] == 'detail':
            for key in ('total_weight', 'combined_length', 'combined_weight', 'remnant', 'utilization'):
                display[key] = None
        elif row['kind'] == 'subtotal':
            display['material'], display['spec'] = '', '소 계'
        else:
            display['material'], display['spec'] = '', '합 계'
        display_rows.append(display)
    assemblies = {a['input_index']: a for a in plan.get('splices', [])}
    reconciliation = []
    for index, item in enumerate(plan['bom']):
        included, qty = item.get('included', True), item['quantity']
        assembly = assemblies.get(index)
        expected_pieces = qty * (assembly['whole_bars_per_part'] + assembly['tail_connector_count']) if assembly else qty
        expected_length = units(item['length']) * qty
        if assembly:
            expected_length += units(assembly['allowance_per_joint']) * assembly['joint_count'] * qty
        if (allocated_quantity[index], allocated_length[index]) != ((expected_pieces, expected_length) if included else (0, 0)):
            raise ValueError(f"원본 {item.get('source_row') or index + 1}행의 제작 수량 또는 길이가 일치하지 않습니다.")
        reconciliation.append({'row': item.get('source_row') or index + 1, 'title': item.get('title', ''),
            'material': item['material'], 'spec': item['spec'], 'required_quantity': qty,
            'planned_quantity': qty if included else 0, 'excluded_quantity': 0 if included else qty,
            'component_quantity': allocated_quantity[index], 'status': '이음 승인 대상' if assembly else '계산 포함' if included else '사용자 동의 제외'})
    excluded = [{**e, 'display_reason': exclusion_reason(e)} for e in plan.get('excluded', [])]
    return {'material_columns': [{'key': k, 'label': label} for k, label in MATERIAL_COLUMNS],
            'combination_columns': [{'key': k, 'label': label} for k, label in COMBINATION_COLUMNS],
            'material_rows': rows, 'material_display_rows': display_rows,
            'combination_groups': list(groups.values()), 'excluded': excluded, 'reconciliation': reconciliation,
            'weight_basis': basis, 'output_piece_count': produced, 'quantity_verified': True,
            'allocations_by_source': dict(allocations_by_source)}


def combination_rows(pattern, max_note_chars=30000):
    """Keep Excel cells below its text limit without losing BOM references."""
    for cut in pattern['cuts']:
        notes = cut['remarks']
        chunks = [notes[i:i + max_note_chars] for i in range(0, len(notes), max_note_chars)] or ['']
        for index, chunk in enumerate(chunks):
            yield {**pattern, 'length': cut['length'] if not index else None,
                   'split_count': cut['split_count'] if not index else None,
                   'total_quantity': cut['total_quantity'] if not index else None,
                   'total_weight': cut['total_weight'] if not index else None, 'remarks': chunk}
