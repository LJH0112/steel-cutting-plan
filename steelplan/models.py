from decimal import Decimal, InvalidOperation
from datetime import date, datetime, timedelta, timezone
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .materials import DEFAULT_STOCK_LENGTHS, compact


def millimeters(value) -> Decimal:
    try:
        result = Decimal(re.sub(r'\s+', '', str(value)).replace(',', ''))
    except (InvalidOperation, ValueError):
        raise ValueError('숫자를 입력해 주세요.')
    if not result.is_finite() or result < 0 or result > 100000:
        raise ValueError('길이는 0~100,000mm 범위여야 합니다.')
    if result != result.quantize(Decimal('0.001')):
        raise ValueError('길이는 소수점 셋째 자리까지만 입력할 수 있습니다.')
    return result


class BOMItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    material: str = Field(min_length=1, max_length=100)
    spec: str = Field(min_length=1, max_length=150)
    length: Decimal = Field(gt=0, le=100000)
    quantity: int = Field(gt=0, le=5000, strict=True)
    source_row: int | None = Field(default=None, ge=1, le=10101)
    title: str = Field(default='', max_length=150)
    item_class: str = Field(default='', max_length=150)
    weight: str = Field(default='', max_length=100)
    paint_area: str = Field(default='', max_length=100)
    input_index: int | None = Field(default=None, ge=0, le=999)

    @field_validator('material', 'spec', mode='before')
    @classmethod
    def remove_spaces(cls, value):
        return compact(value) if isinstance(value, str) else value

    @field_validator('material', 'spec', 'title', 'item_class', 'weight', 'paint_area')
    @classmethod
    def validate_text(cls, value):
        if re.search(r'[\x00-\x1f\x7f]', value):
            raise ValueError('재질과 규격에 줄바꿈이나 제어 문자를 사용할 수 없습니다.')
        return value

    @field_validator('length', mode='before')
    @classmethod
    def validate_length(cls, value):
        return millimeters(value)


class OptimizeRequest(BaseModel):
    items: list[BOMItem] = Field(min_length=1, max_length=1000)
    stock_lengths: list[Decimal] | None = Field(default=None, min_length=1, max_length=20)
    kerf: Decimal = Field(default=Decimal('3'), ge=0, le=100)
    time_limit: int = Field(default=15, ge=1, le=60)
    exclude_invalid: bool = Field(default=False, strict=True)
    exclusion_token: str | None = Field(default=None, min_length=64, max_length=64)
    weight_basis: Literal['unspecified', 'row_total', 'per_piece'] = 'unspecified'
    splice_allowance: Decimal = Field(default=Decimal('5'), ge=0, le=1000)
    reusable_remnant_min: Decimal = Field(default=Decimal('5'), ge=0, le=100000)
    splice_details_approved: bool = Field(default=False, strict=True)
    enable_splice_planning: bool = Field(default=True, strict=True)
    project_name: str = Field(default='', max_length=200)
    project_section: str = Field(default='', max_length=200)
    source_filename: str = Field(default='', max_length=250)
    report_date: date = Field(default_factory=lambda: datetime.now(timezone(timedelta(hours=9))).date())

    @field_validator('project_name', 'project_section', 'source_filename')
    @classmethod
    def validate_report_text(cls, value):
        if re.search(r'[\x00-\x1f\x7f]', value):
            raise ValueError('보고서 정보에 줄바꿈이나 제어 문자를 사용할 수 없습니다.')
        return value.strip()

    @field_validator('stock_lengths', mode='before')
    @classmethod
    def validate_stocks(cls, values):
        if values is None:
            return None
        if not isinstance(values, list):
            raise ValueError('발주 길이 목록이 필요합니다.')
        parsed = sorted({millimeters(v) for v in values})
        if not parsed or any(v <= 30 for v in parsed):
            raise ValueError('발주 길이는 30mm보다 커야 합니다.')
        return parsed

    @field_validator('kerf', 'splice_allowance', 'reusable_remnant_min', mode='before')
    @classmethod
    def validate_kerf(cls, value):
        return millimeters(value)


class StockSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    stock_lengths_by_type: dict[str, list[Decimal]]

    @field_validator('stock_lengths_by_type', mode='before')
    @classmethod
    def validate_lengths(cls, value):
        if not isinstance(value, dict) or set(value) != set(DEFAULT_STOCK_LENGTHS):
            raise ValueError('6개 자재 종류의 발주 길이를 모두 지정해 주세요.')
        result = {}
        for name, lengths in value.items():
            if not isinstance(lengths, list) or not 1 <= len(lengths) <= 20:
                raise ValueError(f'{name}: 발주 길이는 1~20개까지 지정할 수 있습니다.')
            parsed = sorted({millimeters(length) for length in lengths})
            if any(length <= 30 for length in parsed):
                raise ValueError(f'{name}: 발주 길이는 30mm보다 커야 합니다.')
            result[name] = parsed
        if result['각파이프'] != [Decimal('6000')]:
            raise ValueError('각파이프 발주 길이는 6000mm 고정입니다. 긴 부재는 이음 제작 검토 대상으로 처리합니다.')
        return result
