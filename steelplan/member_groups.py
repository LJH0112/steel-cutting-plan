"""TS BOM cutting partitions; identifiers and material/spec remain unchanged."""
import re

GENERAL = 'GENERAL'
BK = 'BK'
GROUP_LABELS = {GENERAL: '거더 (일반 부재)', BK: 'BK 전용 구역'}
# Part Mark examples in the actual TS BOM: WBK1, FBK2, WBK3.
# A delimited original ID such as B5_WBK3 is also a BK member.
_BK_MARK = re.compile(r'(?:^|[-_\s])(?:BK|WBK|FBK)(?=\d|[-_]|$)', re.IGNORECASE)


def member_group(item):
    get = item.get if isinstance(item, dict) else lambda name, default='': getattr(item, name, default)
    if get('source_format') == 'ts_bom' and _BK_MARK.search(get('title', '').strip()):
        return BK
    return GENERAL


def group_order(value):
    return 0 if value == GENERAL else 1
