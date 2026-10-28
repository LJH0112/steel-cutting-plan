"""Explicit section classification and editable order-length defaults."""
import re

DEFAULT_STOCK_LENGTHS = {
    'H빔': list(range(8000, 16001, 1000)),
    '각파이프': [6000],
    '잔넬': [10000],
    '앵글': [10000],
    'C형강': [10000],
    '환봉': [6000, 8000],
    'T바': [],  # No assumed stock length: an administrator must supply it.
}
TS_BOM_MATERIAL_TYPES = frozenset(('H빔', '잔넬', 'C형강', '환봉', 'T바'))
NUMBER = r'\d+(?:\.\d+)?'
DIM = rf'[xX×*]{NUMBER}'
SECTION_PATTERNS = (
    ('H빔', rf'H-?{NUMBER}(?:{DIM}){{0,3}}'),
    ('각파이프', rf'P-?{NUMBER}(?:{DIM}){{2}}'),
    ('각파이프', rf'TS-?{NUMBER}(?:{DIM}){{2}}'),
    ('잔넬', rf'\[{NUMBER}(?:{DIM}){{3}}\]?'),
    ('앵글', rf'L-?{NUMBER}(?:{DIM}){{2}}'),
    ('C형강', rf'C-?{NUMBER}(?:{DIM}){{3}}'),
    ('환봉', rf'D{NUMBER}'),
    ('환봉', rf'ROD{NUMBER}'),
    ('T바', rf'T-?{NUMBER}(?:{DIM}){{2,3}}'),
)


def compact(value):
    return re.sub(r'\s+', '', str(value or ''))


def classify_material(spec):
    value = compact(spec).upper()
    return next((name for name, pattern in SECTION_PATTERNS if re.fullmatch(pattern, value)), None)
