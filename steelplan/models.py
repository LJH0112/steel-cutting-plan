from decimal import Decimal, InvalidOperation
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator


def millimeters(value) -> Decimal:
    try:
        result = Decimal(str(value).strip().replace(',', ''))
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

    @field_validator('material', 'spec')
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
    stock_lengths: list[Decimal] = Field(min_length=1, max_length=10)
    kerf: Decimal = Field(default=Decimal('3'), ge=0, le=100)
    time_limit: int = Field(default=15, ge=1, le=60)

    @field_validator('stock_lengths', mode='before')
    @classmethod
    def validate_stocks(cls, values):
        if not isinstance(values, list):
            raise ValueError('발주 길이 목록이 필요합니다.')
        parsed = sorted({millimeters(v) for v in values})
        if not parsed or any(v <= 30 for v in parsed):
            raise ValueError('발주 길이는 30mm보다 커야 합니다.')
        return parsed

    @field_validator('kerf', mode='before')
    @classmethod
    def validate_kerf(cls, value):
        return millimeters(value)
