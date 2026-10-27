"""Print the shared report tables without changing the calculation engine."""
import io
from pathlib import Path
from xml.sax.saxutils import escape

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from reportlab.lib import colors
from reportlab.lib.pagesizes import A3, A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak

from .reports import MATERIAL_COLUMNS, COMBINATION_COLUMNS, build_report, combination_rows

ROOT = Path(__file__).resolve().parent.parent
FONT = 'NanumGothic'
pdfmetrics.registerFont(TTFont(FONT, str(ROOT / 'assets/fonts/NanumGothic-Regular.ttf')))


def safe_cell(value):
    if isinstance(value, str) and value.startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


def paper(value):
    if value not in ('A4', 'A3'):
        raise ValueError('인쇄 용지는 A4 또는 A3를 선택해 주세요.')
    return value


def styled_sheet(wb, title, headers, rows, widths, paper_size='A4'):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for row in rows:
        ws.append([safe_cell(v) for v in row])
    ws.freeze_panes = 'A2'
    ws.sheet_view.showGridLines = False
    ws.auto_filter.ref = ws.dimensions
    ws.print_title_rows = '1:1'
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.paperSize = ws.PAPERSIZE_A3 if paper(paper_size) == 'A3' else ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.oddFooter.center.text = 'STEELPLAN  |  &P / &N'
    ws.oddFooter.center.size = 9
    for row in ws:
        for c in row:
            c.font = Font(name='맑은 고딕', size=10)
            c.alignment = Alignment(vertical='center', wrap_text=True)
            if isinstance(c.value, (float, int)):
                c.number_format = '#,##0.###'
    for cell in ws[1]:
        cell.font = Font(name='맑은 고딕', bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='087F6B')
    ws.row_dimensions[1].height = 26
    for i, width in enumerate(widths, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = width
    return ws


def summary_text(plan, report):
    s = plan['summary']
    physical = report['output_piece_count']
    text = (f"발주 {s['bar_count']}본 | 총 로스율 {s['loss_rate']}% | "
            f"BOM 완성 부재 {s.get('input_piece_count', s['piece_count'])}개 = "
            f"계산 {s['piece_count']}개 + 제외 {s.get('excluded_piece_count', 0)}개 | 제작 절편 {physical}개")
    if plan.get('splices'):
        text += ' | ' + ('검토 필요 · 제작 확정 아님' if plan['splice_summary']['review_required'] else '이음 상세 사용자 승인 입력됨')
    return text


def notes_text(plan):
    basis = {'unspecified': '미지정(공란)', 'row_total': 'WEIGHT.KG 행 전체 중량', 'per_piece': 'WEIGHT.KG 개당 중량'}[plan['settings'].get('weight_basis', 'unspecified')]
    return (f"단위 mm, kg. 원자재 행의 조합길이·잔철은 수량 전체 합계; 소계·합계의 길이는 총 발주 길이. "
            f"사용율=제작 절편 길이/발주 길이×100. 총 로스율은 잔철+날 손실 기준. "
            f"원자재 중량은 단위중량 미제공으로 공란. 조합중량 해석: {basis}; 누락·오류가 있으면 소계도 공란. "
            f"조합 리스트의 조합길이·잔철·등분은 1본 기준; 총수량=등분×원자재 수량. 비고 수량은 총수량 기준.")


def export_xlsx(plan, paper_size='A4'):
    paper(paper_size)
    report = plan.get('report') or build_report(plan)
    wb = Workbook(); wb.remove(wb.active)
    ws = styled_sheet(wb, '원자재 리스트', [label for _, label in MATERIAL_COLUMNS],
                      [[row[key] for key, _ in MATERIAL_COLUMNS] for row in report['material_rows']],
                      [18, 34, 18, 12, 20, 22, 22, 20, 16], paper_size)
    for index, row in enumerate(report['material_rows'], 2):
        if row['kind'] != 'detail':
            for cell in ws[index]:
                cell.font = Font(name='맑은 고딕', bold=True, size=10)
                cell.fill = PatternFill('solid', fgColor='DDEDE5' if row['kind'] == 'grand_total' else 'F0F5F2')
                cell.border = Border(top=Side(style='thin', color='B8CCC0'))
    ws.append([summary_text(plan, report)])
    ws.merge_cells(start_row=ws.max_row, start_column=1, end_row=ws.max_row, end_column=9)
    ws.row_dimensions[ws.max_row].height = 32
    ws.cell(ws.max_row, 1).alignment = Alignment(wrap_text=True)
    ws.append([notes_text(plan)])
    ws.merge_cells(start_row=ws.max_row, start_column=1, end_row=ws.max_row, end_column=9)
    ws.row_dimensions[ws.max_row].height = 45
    ws.cell(ws.max_row, 1).alignment = Alignment(wrap_text=True)
    combo = styled_sheet(wb, '조합 리스트', [label for _, label in COMBINATION_COLUMNS], [],
                         [12, 18, 12, 20, 18, 16, 18, 12, 14, 70], paper_size)
    for group in report['combination_groups']:
        combo.append([f"{group['material']} / {group['spec']} / {group['material_type']}"])
        group_row = combo.max_row
        combo.merge_cells(start_row=group_row, start_column=1, end_row=group_row, end_column=10)
        combo.cell(group_row, 1).fill = PatternFill('solid', fgColor='E5EEE8')
        combo.cell(group_row, 1).font = Font(name='맑은 고딕', bold=True)
        for pattern in group['patterns']:
            start = combo.max_row + 1
            for row in combination_rows(pattern):
                combo.append([safe_cell(row[key]) for key, _ in COMBINATION_COLUMNS])
            end = combo.max_row
            for col in range(1, 7):
                if end > start:
                    combo.merge_cells(start_row=start, start_column=col, end_row=end, end_column=col)
            for row in combo.iter_rows(min_row=start, max_row=end):
                for cell in row:
                    cell.alignment = Alignment(vertical='center', wrap_text=True)
                    cell.font = Font(name='맑은 고딕', size=10)
                    if isinstance(cell.value, (float, int)): cell.number_format = '#,##0.###'
    combo.auto_filter.ref = f'A1:J{combo.max_row}'
    styled_sheet(wb, 'BOM', ['재질', '규격', '길이 (mm)', '수량', '자재 종류', '원본 행', '부재 번호 (TITLE)',
                            '부재 분류 (ITEM)', 'WEIGHT.KG', 'PAINT.M', '계산 여부'],
                 [[i['material'], i['spec'], float(i['length']), i['quantity'], i.get('material_type', ''),
                   i.get('source_row'), i.get('title', ''), i.get('item_class', ''), i.get('weight', ''),
                   i.get('paint_area', ''), '포함' if i.get('included', True) else '제외'] for i in plan['bom']],
                 [18, 32, 18, 12, 18, 12, 24, 24, 18, 18, 15], paper_size)
    cfg = plan['settings']
    settings = [['요약', summary_text(plan, report)], ['설명', notes_text(plan)],
                ['절단날 손실 (mm/개)', cfg['kerf']], ['절단 후 잔재', '>30mm; 무절단 이음 통재는 절단 대상 아님'],
                ['계산 상태', '최적해 증명' if plan['summary']['optimal'] else '실행 가능 계획 / 최적해 미확정'],
                ['인쇄 용지', paper_size + ' 가로']]
    settings.extend([kind + ' 발주 길이', ', '.join(f'{v:g}' for v in values)] for kind, values in cfg.get('stock_lengths_by_type', {}).items())
    settings.extend([f"{r['material']} / {r['spec']}", r['detail']] for r in plan['statuses'])
    if plan.get('splices'):
        settings.extend([['이음 제작 여유 (mm/이음)', cfg['splice_allowance']],
                         ['재사용 잔재 최소 길이 (mm)', cfg['reusable_remnant_min']],
                         ['이음 여유 합계 (mm)', plan['summary']['total_splice_allowance']],
                         ['이음 여유 포함 순 로스율 (%)', plan['summary']['net_loss_rate']]])
    styled_sheet(wb, '계산 설정', ['항목', '값'], settings, [38, 130], paper_size)
    styled_sheet(wb, '계산 제외 부재', ['원본 행', '부재 번호', '자재 종류', '재질', '규격', '길이 (mm)', '수량', '허용 발주 길이 (mm)', '제외 원인'],
                 [[e['row'], e['title'], e['material_type'], e['material'], e['spec'], float(e['length']), e['quantity'],
                   ', '.join(f'{v:g}' for v in e['allowed_lengths']), e['display_reason']] for e in report['excluded']],
                 [12, 24, 18, 18, 32, 20, 12, 45, 65], paper_size)
    if plan.get('splices'):
        styled_sheet(wb, '이음 제작 검토', ['원본 행', '부재 번호', '재질', '규격', '완성 길이', '수량', '본재 길이',
                       '통재 본수/부재', '연결재 길이', '이음 수/부재', '여유/이음', '상태'],
                     [[a.get(k) for k in ('row', 'title', 'material', 'spec', 'finished_length', 'quantity', 'base_length',
                        'whole_bars_per_part', 'connector_length', 'joint_count', 'allowance_per_joint', 'status')]
                      for a in plan['splices']], [12, 22, 18, 32, 18, 12, 18, 18, 18, 18, 18, 22], paper_size)
        styled_sheet(wb, '잔재 재사용', ['원본 행', '부재 번호', '재질', '규격', '연결재 길이', '수량', '원자재 패턴', '사용 전 잔재', '사용 후 잔재'],
                     [[a.get(k) for k in ('row', 'title', 'material', 'spec', 'length', 'quantity', 'pattern_id', 'before', 'after')]
                      for a in plan['remnant_reuse']], [12, 22, 18, 32, 20, 12, 20, 20, 20], paper_size)
    output = io.BytesIO(); wb.save(output)
    return output.getvalue()


def export_pdf(plan, paper_size='A4'):
    page_size = landscape(A3 if paper(paper_size) == 'A3' else A4)
    report = plan.get('report') or build_report(plan)
    output = io.BytesIO()
    doc = SimpleDocTemplate(output, pagesize=page_size, rightMargin=25, leftMargin=25,
                            topMargin=26, bottomMargin=32, title='STEELPLAN 원자재 리스트 · 조합 리스트')
    width = page_size[0] - 50
    body = ParagraphStyle('body', fontName=FONT, fontSize=8 if paper_size == 'A4' else 10, leading=12, wordWrap='CJK')
    heading = ParagraphStyle('heading', parent=body, fontSize=14, leading=20, spaceBefore=8, spaceAfter=8)
    def p(value, style=body):
        return Paragraph(escape('' if value is None else str(value)).replace('\n', '<br/>'), style)
    def display(value):
        return f'{value:,.3f}'.rstrip('0').rstrip('.') if isinstance(value, float) else value
    def table(headers, rows, proportions, extra=(), group_title=None):
        data = [[p(c) for c in headers]] + [[p(display(c)) for c in row] for row in rows]
        offset = int(group_title is not None)
        if group_title is not None:
            data.insert(0, [p(group_title)] + [''] * (len(headers) - 1))
        shifted = [(command, (start[0], start[1] + offset), (end[0], end[1] + offset), *values)
                   for command, start, end, *values in extra]
        t = Table(data, colWidths=[width * v / sum(proportions) for v in proportions], repeatRows=1+offset,
                  hAlign='LEFT', splitByRow=1, splitInRow=1)
        t.setStyle(TableStyle([('BACKGROUND', (0,0),(-1,offset),colors.HexColor('#dfece5')),
            ('VALIGN',(0,0),(-1,-1),'TOP'), ('LEFTPADDING',(0,0),(-1,-1),5),
            ('RIGHTPADDING',(0,0),(-1,-1),5), ('TOPPADDING',(0,0),(-1,-1),5),
            ('BOTTOMPADDING',(0,0),(-1,-1),5),
            ('LINEBELOW',(0,0),(-1,0),.6,colors.HexColor('#658473')),
            ('INNERGRID',(0,0),(-1,-1),.2,colors.HexColor('#dae2dc')), *shifted,
            *([('SPAN',(0,0),(-1,0))] if offset else [])]))
        return t
    story = [p('원자재 리스트', heading), p(summary_text(plan, report)), Spacer(1,8)]
    raw_rows = [[r[k] for k, _ in MATERIAL_COLUMNS] for r in report['material_rows']]
    shading = [('BACKGROUND',(0,i),(-1,i),colors.HexColor('#edf3ef')) for i,r in enumerate(report['material_rows'],1) if r['kind']!='detail']
    story += [table([l for _,l in MATERIAL_COLUMNS], raw_rows, [60,165,65,45,70,80,80,65,65], shading), Spacer(1,8), p(notes_text(plan))]
    story += [PageBreak(), p('조합 리스트', heading), p(summary_text(plan, report)), p('동일 패턴은 수량으로 묶음. 조합길이·잔철·등분은 1본 기준, 총수량=등분×수량. 비고는 원본 BOM 번호와 배정 총수량.')]
    if plan.get('splices'):
        status = '검토 필요 · 이음 위치/상세 승인 전 제작 확정 아님' if plan['splice_summary']['review_required'] else '이음 상세 사용자 승인 입력됨'
        story += [p(status, heading), p(f"무절단 6m 통재 {plan['splice_summary']['whole_stock_bars']}본; 잔재 연결재 {plan['splice_summary']['reused_connectors']}개; 연결재 추가 발주 {plan['splice_summary']['extra_connector_stock_bars']}본. 이음 여유 {plan['settings']['splice_allowance']}mm/이음, 합계 {plan['summary']['total_splice_allowance']}mm; 날 손실과 별도.")]
        story.append(table(['부재 번호','규격','완성 길이','수량','본재 길이','연결재 길이','이음 수/부재','상태'],
            [[a['title'],a['spec'],a['finished_length'],a['quantity'],a['base_length'],a['connector_length'],a['joint_count'],a['status']] for a in plan['splices']],
            [90,170,65,40,65,65,60,130]))
    for group in report['combination_groups']:
        story += [Spacer(1,8)]
        rows, shading = [], []
        for pattern in group['patterns']:
            start = len(rows) + 1
            for index, r in enumerate(combination_rows(pattern)):
                rows.append([r[k] if index == 0 or col == 0 or col >= 6 else None for col,(k,_) in enumerate(COMBINATION_COLUMNS)])
            end = len(rows)
            if pattern['number'] % 2:
                shading.append(('BACKGROUND',(0,start),(-1,end),colors.HexColor('#f3f6f4')))
            shading.append(('LINEBELOW',(0,end),(-1,end),.7,colors.HexColor('#a5baad')))
        story.append(table([l for _,l in COMBINATION_COLUMNS], rows, [34,60,40,75,60,60,65,40,50,190], shading,
                           group_title=f"{group['material']} / {group['spec']} / {group['material_type']}"))
    story += [Spacer(1,10), p('계산 제외 부재', heading)]
    if report['excluded']:
        story.append(table(['원본 행','부재 번호','규격','절단 길이','수량','제외 사유'],
            [[e['row'],e['title'],e['spec'],float(e['length']),e['quantity'],e['display_reason']] for e in report['excluded']], [45,100,180,80,40,230]))
    else:
        story.append(p('계산 제외 부재 없음. 6m 초과 각파이프는 이음 제작 검토에 포함합니다.'))
    story += [p('계산 조건', heading), p(notes_text(plan)), p(f"날 손실 {plan['settings']['kerf']}mm/개. 절단 후 잔재 >30mm. 통재 본재/통재 연결재는 무절단이므로 날 손실·잔재 0mm. 잔재는 동일 재질·규격만 재사용하며, 연결재 절단 후에도 >30mm를 남깁니다.")]
    story.extend(p(f"{r['material']} / {r['spec']}: {r['detail']}") for r in plan['statuses'])
    def footer(canvas, document):
        canvas.setFont(FONT,8); canvas.setFillColor(colors.HexColor('#667c6e'))
        canvas.drawString(25,16,'STEELPLAN | 단위 mm, kg | '+paper_size+' 가로')
        canvas.drawRightString(page_size[0]-25,16,f'{document.page}')
    doc.build(story,onFirstPage=footer,onLaterPages=footer)
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
