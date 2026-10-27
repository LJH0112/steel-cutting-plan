"""Validate every input row before allowing explicitly consented exclusions."""
import hashlib
import json

from .materials import classify_material
from .optimizer import MIN_REMAINDER, optimize, units


class PlanningIssues(ValueError):
    def __init__(self, validation):
        self.validation = validation
        self.issues = validation['issues']
        super().__init__('계산 예외가 있습니다. 설정을 수정하거나 예외 부재 제외에 동의한 뒤 다시 계산해 주세요.')


def assess_items(items, kerf, settings):
    eligible, issues = [], []
    for index, item in enumerate(items):
        kind = classify_material(item.spec)
        allowed = settings.stock_lengths_by_type.get(kind, [])
        code = reason = None
        if kind is None:
            code, reason = 'UNSUPPORTED_MATERIAL', '미지원 자재: DESCRIPTION 규격 형식을 확인해 주세요.'
        elif item.length > max(allowed):
            code, reason = 'STOCK_LENGTH_EXCEEDED', '발주 가능 길이 초과'
        elif units(item.length) + units(kerf) + MIN_REMAINDER > units(max(allowed)):
            code, reason = 'CUTTING_ALLOWANCE', '절단날 손실 및 잔재 30mm 초과 조건을 만족할 발주 길이가 없습니다.'
        if code:
            issues.append({**item.model_dump(mode='json'), 'row': item.source_row or index + 1,
                           'input_index': index, 'material_type': kind or '미지원 자재',
                           'code': code, 'reason': reason,
                           'allowed_lengths': [float(s) for s in allowed]})
        else:
            eligible.append(item)
    return eligible, issues


def validate_plan_input(request, settings):
    if request.stock_lengths is not None:
        raise ValueError('전역 발주 길이는 사용할 수 없습니다. 관리자 설정의 자재 종류별 길이를 사용해 주세요.')
    if sum(item.quantity for item in request.items) > 5000:
        raise ValueError('총 부재 수량은 5,000개 이하여야 합니다.')
    eligible, issues = assess_items(request.items, request.kerf, settings)
    snapshot = {'items': [i.model_dump(mode='json') for i in request.items],
                'settings': settings.model_dump(mode='json'), 'kerf': str(request.kerf.normalize())}
    token = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    return {'issues': issues, 'exclusion_token': token, 'eligible_rows': len(eligible),
            'eligible_pieces': sum(item.quantity for item in eligible),
            'excluded_pieces': sum(issue['quantity'] for issue in issues)}


def plan_by_material(request, settings):
    validation = validate_plan_input(request, settings)
    issues = validation['issues']
    if issues and (not request.exclude_invalid or request.exclusion_token != validation['exclusion_token']):
        raise PlanningIssues(validation)
    eligible, _ = assess_items(request.items, request.kerf, settings)
    if not eligible:
        raise ValueError('계산 가능한 정상 부재가 없습니다. 예외 목록과 관리자 설정을 확인해 주세요.')
    plan = optimize(request.model_copy(update={'items': eligible}), settings.stock_lengths_by_type)
    plan['excluded'] = issues
    plan['bom'] = [item.model_dump(mode='json') for item in request.items]
    excluded_indices = {issue['input_index'] for issue in issues}
    for index, item in enumerate(plan['bom']):
        item['included'] = index not in excluded_indices
        item['material_type'] = classify_material(item['spec']) or '미지원 자재'
    plan['summary'].update(input_rows=len(request.items), input_piece_count=sum(i.quantity for i in request.items),
                           excluded_rows=len(issues), excluded_piece_count=validation['excluded_pieces'])
    return plan
