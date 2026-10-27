"""Strict, bounded XLSX import with automatic Korean / English column matching."""
import io
import re
import zipfile
from decimal import Decimal, InvalidOperation

from openpyxl import load_workbook
from pydantic import ValidationError

from .models import BOMItem, millimeters

MAX_UPLOAD = 10 * 1024 * 1024
MAX_ROWS = 10000
FIELDS = ('material', 'spec', 'length', 'quantity')
ALIASES = {
    'material': ['재질', '재료', '강종', '材質', 'material', 'grade', 'steelgrade'],
    'spec': ['규격', '단면', '형강규격', '부재규격', 'spec', 'specification', 'section', 'size', 'profile'],
    'length': ['길이', '길이mm', '절단길이', '절단길이mm', '부재길이', 'length', 'lengthmm', 'cutlength'],
    'quantity': ['수량', '수량ea', '개수', '본수', '소요수량', 'qty', 'quantity', 'count', 'pcs'],
}


def normalized(value):
    return re.sub(r'[^a-z0-9가-힣材質]', '', str(value or '').lower())


def workbook(data):
    if len(data) > MAX_UPLOAD:
        raise ValueError('엑셀 파일은 10MB 이하만 업로드할 수 있습니다.')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(i.file_size for i in archive.infolist()) > 50 * 1024 * 1024:
                raise ValueError('압축 해제 크기가 너무 큰 엑셀 파일입니다.')
        return load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise ValueError('올바른 .xlsx 파일을 업로드해 주세요.') from exc


def find_mapping(headers):
    result = {}
    for field, names in ALIASES.items():
        matches = [i for i, cell in enumerate(headers) if normalized(cell) in names]
        if len(matches) == 1:
            result[field] = matches[0]
    return result


def inspect_bom(data):
    wb = workbook(data)
    try:
        sheets = []
        for ws in wb.worksheets[:30]:
            best = (0, 1, [], {})
            for index, row in enumerate(ws.iter_rows(max_row=30, max_col=100, values_only=True), 1):
                headers = [str(c).strip() if c is not None else '' for c in row]
                mapping = find_mapping(headers)
                if len(mapping) > best[0]:
                    best = (len(mapping), index, headers, mapping)
            _, header_row, headers, mapping = best
            sheets.append({'name': ws.title, 'header_row': header_row, 'headers': headers, 'mapping': mapping})
        if not sheets:
            raise ValueError('읽을 수 있는 시트가 없습니다.')
        return {'sheets': sheets, 'suggested_sheet': max(sheets, key=lambda s: len(s['mapping']))['name']}
    finally:
        wb.close()


def read_headers(data, sheet, header_row):
    if not 1 <= header_row <= 100:
        raise ValueError('헤더 행은 1~100 사이로 설정해 주세요.')
    wb = workbook(data)
    try:
        if sheet not in wb.sheetnames:
            raise ValueError('선택한 시트가 없습니다.')
        headers = next(wb[sheet].iter_rows(min_row=header_row, max_row=header_row, max_col=100, values_only=True))
        headers = [str(c).strip() if c is not None else '' for c in headers]
        return {'headers': headers, 'mapping': find_mapping(headers)}
    finally:
        wb.close()


def parse_bom(data, sheet, header_row, mapping):
    if set(mapping) != set(FIELDS) or len(set(mapping.values())) != 4:
        raise ValueError('재질·규격·길이·수량에 서로 다른 열을 지정해 주세요.')
    if any(type(v) is not int or not 0 <= v < 100 for v in mapping.values()):
        raise ValueError('열 번호가 올바르지 않습니다.')
    if not 1 <= header_row <= 100:
        raise ValueError('헤더 행은 1~100 사이로 설정해 주세요.')
    wb = workbook(data)
    try:
        if sheet not in wb.sheetnames:
            raise ValueError('선택한 시트가 없습니다.')
        ws = wb[sheet]
        items, errors = [], []
        for rownum, row in enumerate(ws.iter_rows(min_row=header_row + 1, max_row=header_row + MAX_ROWS + 1,
                                                  max_col=max(mapping.values()) + 1, values_only=True), header_row + 1):
            values = {key: row[col] for key, col in mapping.items()}
            if all(v is None or str(v).strip() == '' for v in values.values()):
                continue
            if rownum > header_row + MAX_ROWS:
                raise ValueError('데이터 행은 최대 10,000행까지 읽을 수 있습니다.')
            try:
                for key in ('material', 'spec'):
                    if values[key] is None:
                        raise ValueError('재질 또는 규격이 비어 있습니다. 병합 셀도 각 행에 값을 입력해 주세요.')
                    values[key] = str(values[key]).strip()
                values['length'] = millimeters(values['length'])
                raw_qty = values['quantity']
                if isinstance(raw_qty, bool):
                    raise ValueError('수량은 양의 정수여야 합니다.')
                qty = Decimal(str(raw_qty).replace(',', '').strip())
                if not qty.is_finite() or not 0 < qty <= 5000 or qty != qty.to_integral_value():
                    raise ValueError('수량은 1~5,000 사이의 정수여야 합니다.')
                values['quantity'] = int(qty)
                items.append(BOMItem(**values))
            except (ValueError, InvalidOperation, TypeError, ValidationError) as exc:
                message = str(exc)
                if isinstance(exc, ValidationError):
                    message = '; '.join(f"{e['loc'][0]}: {e['msg']}" for e in exc.errors())
                errors.append({'row': rownum, 'message': message[:250]})
                if len(errors) >= 50:
                    break
        if errors:
            return {'items': [], 'errors': errors}
        if not items:
            raise ValueError('BOM 데이터가 없습니다. 시트와 헤더 행을 확인해 주세요.')
        if len(items) > 1000 or sum(i.quantity for i in items) > 5000:
            raise ValueError('BOM은 최대 1,000행, 총 5,000개 부재까지 지원합니다.')
        return {'items': [i.model_dump(mode='json') for i in items], 'errors': []}
    finally:
        wb.close()
