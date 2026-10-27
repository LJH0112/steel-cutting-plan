import io
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from reportlab.graphics.shapes import Drawing, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether

ROOT = Path(__file__).resolve().parent.parent
FONT = 'NanumGothic'
pdfmetrics.registerFont(TTFont(FONT, str(ROOT / 'assets/fonts/NanumGothic-Regular.ttf')))
PALETTE = ['#087f6b', '#2f9e8c', '#59b5a2', '#80c9b8', '#397f99', '#63a2b9']


def cut_text(cuts):
    return ' + '.join(f"{c['length']:g} × {c['quantity']}" for c in cuts)


def safe_cell(value):
    # Never let untrusted BOM text become an Excel formula.
    if isinstance(value, str) and value.startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


def styled_sheet(wb, title, headers, rows, widths):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for row in rows:
        ws.append([safe_cell(value) for value in row])
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = ws.dimensions
    ws.sheet_view.showGridLines = False
    for cell in ws[1]:
        cell.font = Font(name='맑은 고딕', bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='087F6B')
        cell.alignment = Alignment(vertical='center', wrap_text=True)
    ws.row_dimensions[1].height = 32
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = Font(name='맑은 고딕', size=10)
            cell.alignment = Alignment(vertical='center', wrap_text=True)
            if cell.row % 2 == 0:
                cell.fill = PatternFill('solid', fgColor='F1F7F5')
    for i, width in enumerate(widths, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = width
    ws.print_title_rows = '1:1'
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    return ws


def export_xlsx(plan):
    wb = Workbook()
    wb.remove(wb.active)
    styled_sheet(wb, '발주 원자재',
                 ['재질', '규격', '발주 길이 (mm)', '발주 수량 (본)', '총 발주 길이 (mm)', '제품 길이 (mm)',
                  '절단 손실 (mm)', '잔재 합계 (mm)', '로스율 (%)', '자재 종류'],
                 [[o[k] for k in ('material', 'spec', 'stock_length', 'quantity', 'total_length', 'parts_length',
                                 'kerf_loss', 'remnant', 'loss_rate', 'material_type')] for o in plan['orders']],
                 [18, 30, 20, 18, 24, 22, 20, 22, 16, 18])
    styled_sheet(wb, 'CUTTING PLAN',
                 ['패턴 ID', '재질', '규격', '원자재 길이 (mm)', '반복 본수', '절단 조합 (mm × 개/본)',
                  '제품 개수/본', '제품 길이/본 (mm)', '절단 손실/본 (mm)', '잔재/본 (mm)', '로스율 (%)', '자재 종류'],
                 [[p['id'], p['material'], p['spec'], p['stock_length'], p['bar_count'], cut_text(p['cuts']),
                   p['piece_count'], p['parts_length'], p['kerf_loss'], p['remnant'], p['loss_rate'], p['material_type']]
                  for p in plan['patterns']], [16, 18, 30, 22, 15, 65, 16, 24, 24, 20, 16, 18])
    styled_sheet(wb, 'BOM', ['재질', '규격', '길이 (mm)', '수량', '자재 종류', '원본 행', '부재 번호 (TITLE)',
                            '부재 분류 (ITEM)', 'WEIGHT.KG', 'PAINT.M', '계산 여부'],
                 [[i['material'], i['spec'], float(i['length']), i['quantity'], i.get('material_type', ''),
                   i.get('source_row'), i.get('title', ''), i.get('item_class', ''), i.get('weight', ''),
                   i.get('paint_area', ''), '포함' if i.get('included', True) else '제외'] for i in plan['bom']],
                 [20, 35, 20, 15, 18, 12, 24, 24, 18, 18, 15])
    s, cfg = plan['summary'], plan['settings']
    settings = [['발주 길이 (mm)', ', '.join(f'{x:g}' for x in cfg['stock_lengths'])],
                ['절단날 손실 (mm/개)', cfg['kerf']], ['잔재 규칙', '각 원자재 잔재 > 30mm (30mm 이하 불가)'],
                ['목적함수', cfg['objective']], ['상태', '최적해 증명 완료' if s['optimal'] else '실행 가능 해 / 최적해 미확정'],
                ['시간 제한 (초)', cfg['time_limit']], ['계산 시간 (초)', s['elapsed_seconds']],
                ['총 발주 본수', s['bar_count']], ['총 부재 개수', s['piece_count']], ['전체 로스율 (%)', s['loss_rate']],
                ['로스율 산식', '(발주 길이 - 제품 길이) / 발주 길이 × 100; 절단 손실과 잔재 포함'],
                ['절단 손실 산식', '부재 1개당 1회 절단; 길이 × 수량 + 날 손실 × 수량 + 잔재 = 원자재 길이'],
                ['생성 시각 (UTC)', datetime.now(timezone.utc).isoformat(timespec='seconds')]]
    settings.extend([f"{r['material']} / {r['spec']}", r['detail']] for r in plan['statuses'])
    settings.extend([f'{kind} 발주 길이 (mm)', ', '.join(f'{v:g}' for v in lengths)]
                    for kind, lengths in cfg.get('stock_lengths_by_type', {}).items())
    settings.append(['계산 제외 부재 수량', s.get('excluded_piece_count', 0)])
    styled_sheet(wb, '계산 설정', ['항목', '값'], settings, [32, 110])
    if 'excluded' in plan:
        styled_sheet(wb, '계산 제외 부재', ['원본 행', '부재 번호', '자재 종류', '재질', '규격', '길이 (mm)', '수량', '허용 발주 길이 (mm)', '제외 원인'],
                     [[e['row'], e['title'], e['material_type'], e['material'], e['spec'], float(e['length']),
                       e['quantity'], ', '.join(f'{v:g}' for v in e['allowed_lengths']), e['reason']]
                      for e in plan['excluded']], [12, 24, 18, 18, 32, 20, 12, 45, 65])
    output = io.BytesIO()
    wb.save(output)
    return output.getvalue()


def export_pdf(plan):
    output = io.BytesIO()
    doc = SimpleDocTemplate(output, pagesize=landscape(A4), rightMargin=32, leftMargin=32,
                            topMargin=32, bottomMargin=36, title='철골 발주 및 CUTTING PLAN', author='STEEL PLAN')
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='KTitle', fontName=FONT, fontSize=21, leading=29, textColor=colors.HexColor('#163b33')))
    styles.add(ParagraphStyle(name='KHead', fontName=FONT, fontSize=13, leading=19, spaceBefore=14, spaceAfter=9))
    styles.add(ParagraphStyle(name='KBody', fontName=FONT, fontSize=9, leading=14, wordWrap='CJK'))
    styles.add(ParagraphStyle(name='KSmall', fontName=FONT, fontSize=8, leading=12, wordWrap='CJK'))

    def p(value, style='KBody'):
        return Paragraph(escape(str(value)), styles[style])

    def table(headers, rows, widths):
        data = [[p(c, 'KSmall') for c in headers]] + [[p(c, 'KSmall') for c in row] for row in rows]
        t = Table(data, colWidths=widths, repeatRows=1, hAlign='LEFT')
        t.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e4f0eb')),
                               ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f7f9f8')]),
                               ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                               ('TOPPADDING', (0, 0), (-1, -1), 8),
                               ('LINEBELOW', (0, 0), (-1, 0), .5, colors.HexColor('#a7c4b9'))]))
        return t

    s, cfg = plan['summary'], plan['settings']
    story = [p('STEEL PLAN  /  철골 발주 및 절단 계획', 'KTitle'), Spacer(1, 12),
             p(f"발주 {s['bar_count']}본  ·  부재 {s['piece_count']}개  ·  전체 로스율 {s['loss_rate']}%  ·  "
               + ('최적해 증명 완료' if s['optimal'] else '실행 가능 해 / 최적해 미확정')),
             p(f"날 손실: {cfg['kerf']:g}mm/개 / 잔재: 각 본 30mm 초과 / 계산 제외: {s.get('excluded_piece_count', 0)}개"),
             *[p(f"{kind} 발주 길이: {', '.join(f'{x:g}' for x in lengths)}mm")
               for kind, lengths in cfg.get('stock_lengths_by_type', {}).items()],
             p('최적화 순서: 발주 본수 최소 → 총 발주 길이 최소. 로스율은 절단 손실과 잔재를 포함합니다.'),
             p('01  발주 원자재 목록', 'KHead'),
             table(['종류', '재질', '규격', '발주 길이 mm', '수량 본', '총 길이 mm', '잔재 합계 mm', '로스율 %'],
                   [[o['material_type'], o['material'], o['spec'], f"{o['stock_length']:g}", o['quantity'], f"{o['total_length']:g}",
                     f"{o['remnant']:g}", o['loss_rate']] for o in plan['orders']], [55, 65, 165, 85, 60, 110, 110, 85]),
             p('02  CUTTING PLAN', 'KHead'), p('길이 및 잔재는 원자재 1본 기준입니다. 동일 조합은 반복 본수로 표시합니다.')]
    for pattern in plan['patterns']:
        width = 735
        drawing = Drawing(width, 40)
        cursor = 0
        for index, cut in enumerate(pattern['cuts']):
            segment = width * cut['length'] * cut['quantity'] / pattern['stock_length']
            drawing.add(Rect(cursor, 8, segment, 23, fillColor=colors.HexColor(PALETTE[index % len(PALETTE)]), strokeColor=colors.white, strokeWidth=.7))
            if segment > 55:
                drawing.add(String(cursor + segment / 2, 16, f"{cut['length']:g} x {cut['quantity']}",
                                   fontName=FONT, fontSize=8, fillColor=colors.white, textAnchor='middle'))
            cursor += segment
        kerf_width = width * pattern['kerf_loss'] / pattern['stock_length']
        if kerf_width:
            drawing.add(Rect(cursor, 8, kerf_width, 23, fillColor=colors.HexColor('#dfad50'), strokeWidth=0))
        cursor += kerf_width
        drawing.add(Rect(cursor, 8, width - cursor, 23, fillColor=colors.HexColor('#e2e8e5'), strokeWidth=0))
        story.append(KeepTogether([
            Spacer(1, 12), p(f"{pattern['id']}  |  {pattern['material_type']} / {pattern['material']} / {pattern['spec']}  |  "
                              f"{pattern['stock_length']:g}mm × {pattern['bar_count']}본", 'KHead'),
            drawing, p(cut_text(pattern['cuts']) + '  (mm × 개/본)'),
            p(f"제품 {pattern['piece_count']}개/본 · 날 손실 {pattern['kerf_loss']:g}mm · "
              f"잔재 {pattern['remnant']:g}mm · 로스율 {pattern['loss_rate']}%")]))
    story.extend([Spacer(1, 18), p('그룹별 계산 상태', 'KHead')])
    story.extend(p(f"{r['material']} / {r['spec']}: {r['detail']}") for r in plan['statuses'])
    story.extend([Spacer(1, 12), p('03  계산 제외 부재', 'KHead')])
    if plan.get('excluded'):
        story.append(table(['행', '부재 번호', '종류 / 규격', '길이 mm / 수량', '허용 길이 mm / 원인'],
                           [[e['row'], e['title'], f"{e['material_type']} / {e['spec']}",
                             f"{e['length']} / {e['quantity']}개",
                             ', '.join(f'{v:g}' for v in e['allowed_lengths']) + ' / ' + e['reason']]
                            for e in plan['excluded']], [40, 100, 220, 105, 270]))
    else:
        story.append(p('계산 제외 부재 없음'))

    def footer(canvas, document):
        canvas.setFont(FONT, 8)
        canvas.setFillColor(colors.HexColor('#65766d'))
        canvas.drawString(32, 20, 'STEEL PLAN  ·  단위 mm  ·  잔재 > 30mm  ·  초록: 제품 / 황색: 절단 손실 / 회색: 잔재')
        canvas.drawRightString(809, 20, str(document.page))

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


def sample_xlsx():
    wb = Workbook()
    ws = wb.active
    ws.title = 'BOM'
    ws.append(['재질', '규격', '길이 (mm)', '수량'])
    for row in [('SS275', 'H-300×300×10×15', 3500, 6), ('SS275', 'H-300×300×10×15', 2400, 8),
                ('SS275', 'H-300×300×10×15', 1800, 6), ('SM355', 'H-400×200×8×13', 4200, 4),
                ('SM355', 'H-400×200×8×13', 2700, 6), ('SS275', 'C-200×80×7.5×11', 2900, 8),
                ('SS275', 'C-200×80×7.5×11', 1500, 10)]:
        ws.append(row)
    for col, width in [('A', 20), ('B', 32), ('C', 20), ('D', 15)]:
        ws.column_dimensions[col].width = width
    ws.freeze_panes = 'A2'
    output = io.BytesIO()
    wb.save(output)
    return output.getvalue()
