"""Validate every input row before allowing explicitly consented exclusions."""
import hashlib
import json

from .materials import classify_material, TS_BOM_MATERIAL_TYPES
from .optimizer import MIN_REMAINDER, optimize, units
from .reports import build_report
from .splicing import needs_splice, optimize_with_splices


class PlanningIssues(ValueError):
    def __init__(self, validation):
        self.validation = validation
        self.issues = validation['issues']
        super().__init__('계산 예외가 있습니다. 설정을 수정하거나 예외 부재 제외에 동의한 뒤 다시 계산해 주세요.')


def assess_items(items, kerf, settings, enable_splice_planning=False):
    eligible, issues = [], []
    for index, item in enumerate(items):
        kind = classify_material(item.spec)
        allowed = settings.stock_lengths_by_type.get(kind, [])
        code = reason = None
        if kind is None:
            code, reason = 'UNSUPPORTED_MATERIAL', '미지원 자재: DESCRIPTION 규격 형식을 확인해 주세요.'
        elif item.source_format == 'ts_bom' and kind not in TS_BOM_MATERIAL_TYPES:
            code, reason = 'OUTSIDE_PROFILE_SCOPE', 'TS BOM LIST 대상 외 자재: H빔·잔넬·C형강·환봉·T바만 최적화'
        elif not allowed:
            code, reason = 'STOCK_LENGTH_UNCONFIGURED', f'{kind} 발주 길이 미설정: 관리자 설정에서 승인된 길이를 입력해 주세요.'
        elif enable_splice_planning and needs_splice(item):
            pass  # Deliberately retained for whole-stock base + connector fabrication.
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
    if request.planning_mode=='manufacturing':
        from .manufacturing import validate_manufacturing
        return validate_manufacturing(request,settings)
    if request.import_audit and any(i.source_format != 'ts_bom' or i.source_sheet != 'TS BOM LIST' for i in request.items):
        raise ValueError('TS BOM LIST 가져오기 결과에는 해당 시트의 대상 부재만 사용할 수 있습니다.')
    if request.stock_lengths is not None:
        raise ValueError('전역 발주 길이는 사용할 수 없습니다. 관리자 설정의 자재 종류별 길이를 사용해 주세요.')
    if sum(item.quantity for item in request.items) > 5000:
        raise ValueError('총 부재 수량은 5,000개 이하여야 합니다.')
    eligible, issues = assess_items(request.items, request.kerf, settings, request.enable_splice_planning)
    snapshot = {'items': [i.model_dump(mode='json') for i in request.items],
                'settings': settings.model_dump(mode='json'), 'kerf': str(request.kerf.normalize()),
                'splice_allowance': str(request.splice_allowance.normalize()),
                'reusable_remnant_min': str(request.reusable_remnant_min.normalize()),
                'enable_splice_planning': request.enable_splice_planning,
                'import_audit': request.import_audit.model_dump(mode='json') if request.import_audit else None}
    token = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    return {'issues': issues, 'exclusion_token': token, 'eligible_rows': len(eligible),
            'splice_candidates': [{**i.model_dump(mode='json'), 'status': '검토 필요', 'base_length': 6000}
                                  for i in eligible if needs_splice(i)],
            'eligible_pieces': sum(item.quantity for item in eligible),
            'excluded_pieces': sum(issue['quantity'] for issue in issues)}


def plan_by_material(request, settings):
    if request.planning_mode=='manufacturing':
        from .manufacturing import plan_manufacturing
        return plan_manufacturing(request,settings)
    validation = validate_plan_input(request, settings)
    issues = validation['issues']
    if issues and (not request.exclude_invalid or request.exclusion_token != validation['exclusion_token']):
        raise PlanningIssues(validation)
    eligible, _ = assess_items(request.items, request.kerf, settings, request.enable_splice_planning)
    if not eligible:
        raise ValueError('계산 가능한 정상 부재가 없습니다. 예외 목록과 관리자 설정을 확인해 주세요.')
    excluded_indices = {issue['input_index'] for issue in issues}
    eligible = [item.model_copy(update={'input_index': index}) for index, item in enumerate(request.items)
                if index not in excluded_indices]
    if request.enable_splice_planning:
        plan = optimize_with_splices(request.model_copy(update={'items': eligible}), settings)
    else:
        plan = optimize(request.model_copy(update={'items': eligible}), settings.stock_lengths_by_type)
    plan['excluded'] = issues
    if request.import_audit:
        plan['import_audit'] = request.import_audit.model_dump(mode='json')
    plan['bom'] = [item.model_dump(mode='json') for item in request.items]
    for index, item in enumerate(plan['bom']):
        item['input_index'] = index
        item['included'] = index not in excluded_indices
        item['material_type'] = classify_material(item['spec']) or '미지원 자재'
    plan['summary'].update(input_rows=len(request.items), input_piece_count=sum(i.quantity for i in request.items),
                           excluded_rows=len(issues), excluded_piece_count=validation['excluded_pieces'])
    if plan['summary']['piece_count'] + plan['summary']['excluded_piece_count'] != plan['summary']['input_piece_count']:
        raise RuntimeError('완성 부재 수량 검증 실패')
    plan['settings']['weight_basis'] = request.weight_basis
    plan['report_metadata'] = {'project_name': request.project_name, 'project_section': request.project_section,
                              'source_filename': request.source_filename, 'report_date': request.report_date.isoformat()}
    plan['report'] = build_report(plan)
    return plan
