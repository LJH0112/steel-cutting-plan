"""Reference-style paginated forms: yellow headers, black rules, project metadata."""
import io
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A3, A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, NextPageTemplate, PageBreak, Paragraph, Spacer, Table, TableStyle, Flowable

from .reports import (MATERIAL_COLUMNS, COMBINATION_COLUMNS, MATERIAL_WIDTHS, COMBINATION_WIDTHS,
                      REPORT_ROW_HEIGHT, REPORT_HEADER_HEIGHT, REPORT_FONT_SIZE, REPORT_PAGE_MARGIN, combination_rows)

FONT = 'NanumGothic'
if FONT not in pdfmetrics.getRegisteredFontNames():
    pdfmetrics.registerFont(TTFont(FONT, str(Path(__file__).resolve().parent.parent/'assets/fonts/NanumGothic-Regular.ttf')))
YELLOW = colors.HexColor('#ffff00')


class FixedCell(Flowable):
    """Fit complete text into a fixed cell, without changing any row or column.

    Text uses at most two lines; unusually long identifiers are compressed
    horizontally rather than clipped or omitted. PDF text remains searchable.
    """
    def __init__(self, value, height, alignment='left'):
        super().__init__()
        self.text = '' if value is None else str(value)
        self.height = height
        self.alignment = alignment

    def wrap(self, avail_width, avail_height):
        self.width = avail_width
        return self.width, self.height

    def draw(self):
        text, font, leading = self.text, REPORT_FONT_SIZE, 10
        measure = lambda value: pdfmetrics.stringWidth(value, FONT, font)
        lines = [text]
        if self.alignment == 'left' and measure(text) > self.width and len(text) > 1:
            middle = min(range(1, len(text)), key=lambda i: max(measure(text[:i]), measure(text[i:])))
            lines = [text[:middle], text[middle:]]
        baseline = (self.height - len(lines)*leading)/2 + (leading-font)/2 + leading*(len(lines)-1) + 2
        for line in lines:
            full_width = measure(line)
            scale = min(1, self.width / full_width) if full_width else 1
            width = full_width * scale
            x = self.width-width if self.alignment=='right' else (self.width-width)/2 if self.alignment=='center' else 0
            obj = self.canv.beginText(x, baseline)
            obj.setFont(FONT, font)
            obj.setHorizScale(scale*100)
            obj.textOut(line)
            self.canv.drawText(obj)
            baseline -= leading


def render_pdf(plan, report, summary, notes, paper_size, orientation, section):
    if orientation not in ('portrait', 'landscape') or section not in ('all', 'materials', 'combinations'):
        raise ValueError('인쇄 방향 또는 PDF 구역이 올바르지 않습니다.')
    size = A3 if paper_size == 'A3' else A4
    if orientation == 'landscape': size = landscape(size)
    width, height = size
    margin, footer_y = REPORT_PAGE_MARGIN, 30
    content_width = width - 2*margin
    meta = plan.get('report_metadata', {})
    body = ParagraphStyle('reference-body', fontName=FONT, fontSize=8 if paper_size=='A4' else 10,
                          leading=11 if paper_size=='A4' else 14, wordWrap='CJK')
    heading = ParagraphStyle('reference-heading', parent=body, fontSize=11, leading=16, spaceBefore=8, spaceAfter=8)
    def p(value, style=body):
        return Paragraph(escape('' if value is None else str(value)).replace('\n','<br/>'), style)
    def number(value, comma=False):
        if value is None: return ''
        if not isinstance(value,(float,int)): return str(value)
        text = f'{value:,.3f}' if comma else f'{value:.3f}'
        return text.rstrip('0').rstrip('.')
    def draw_fitted(can, text, x, y, max_width, font_size=9):
        while font_size>6 and pdfmetrics.stringWidth(text,FONT,font_size)>max_width: font_size-=.25
        can.setFont(FONT,font_size); can.drawString(x,y,text)
    def header(title, group=None):
        def draw(can, doc):
            can.saveState();can.setFillColor(colors.black)
            can.setFont(FONT,19 if paper_size=='A4' else 24)
            can.drawCentredString(width/2,height-39,title)
            draw_fitted(can,'[공 사 명]  '+meta.get('project_name',''),margin,height-68,content_width-130)
            draw_fitted(can,'[공사구분]  '+meta.get('project_section',''),margin,height-82,content_width-130)
            can.setFont(FONT,9);can.drawRightString(width-margin,height-68,'[날 짜]  '+meta.get('report_date',''))
            if group:
                sources=' / '.join(f"{p['number']}={p['inventory_id']}" for p in group['patterns'] if p.get('inventory_id'))
                approved=', '.join(str(p['number']) for p in group['patterns'] if p.get('approval_id'))
                draw_fitted(can,f"[재질] {group['material']}    [규격] {group['spec']}    [단위중량]"+('    [재고 패턴] '+sources if sources else '')+('    [승인 패턴] '+approved if approved else ''),margin,height-101,content_width)
            can.restoreState()
        return draw
    class NumberedCanvas(canvas.Canvas):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs);self.states=[]
        def showPage(self):
            self.states.append(dict(self.__dict__));self._startPage()
        def save(self):
            total=len(self.states)
            for state in self.states:
                self.__dict__.update(state)
                self.saveState();self.setFont(FONT,9);self.setFillColor(colors.black)
                self.drawRightString(width-margin,height-82,f'{self._pageNumber} / {total}')
                self.setLineWidth(.65);self.line(margin,footer_y+11,width-margin,footer_y+11)
                draw_fitted(self,'[파 일 명]  '+meta.get('source_filename',''),margin,footer_y-1,content_width)
                self.restoreState();super().showPage()
            super().save()
    def table(headers, rows, ratios, *, comma=True, spans=(), totals=(), ends=()):
        data = [[FixedCell(label, REPORT_HEADER_HEIGHT-4, 'center') for label in headers]]
        data += [[FixedCell(number(value,comma), REPORT_ROW_HEIGHT-4,
                            'right' if isinstance(value,(float,int)) else 'center' if value in ('소 계','합 계') else 'left')
                  for value in row] for row in rows]
        t=Table(data,colWidths=[content_width*v/sum(ratios)for v in ratios],repeatRows=1,
                rowHeights=[REPORT_HEADER_HEIGHT]+[REPORT_ROW_HEIGHT]*len(rows),
                hAlign='LEFT',splitByRow=1)
        commands=[('BACKGROUND',(0,0),(-1,0),YELLOW),('VALIGN',(0,0),(-1,-1),'MIDDLE'),
                  ('LEFTPADDING',(0,0),(-1,-1),4),('RIGHTPADDING',(0,0),(-1,-1),4),
                  ('TOPPADDING',(0,0),(-1,-1),2),('BOTTOMPADDING',(0,0),(-1,-1),2),
                  ('BOX',(0,0),(-1,-1),.7,colors.black),('LINEBELOW',(0,0),(-1,0),.7,colors.black)]
        for col in range(len(headers)-1):commands.append(('LINEAFTER',(col,0),(col,-1),.35,colors.black))
        for row in totals:
            commands.extend([('BACKGROUND',(0,row),(-1,row),YELLOW),('LINEABOVE',(0,row),(-1,row),.5,colors.black),('LINEBELOW',(0,row),(-1,row),.5,colors.black)])
        for row in ends:commands.append(('LINEBELOW',(0,row),(-1,row),.5,colors.black))
        commands.extend(spans);t.setStyle(TableStyle(commands));return t
    output=io.BytesIO()
    doc=BaseDocTemplate(output,pagesize=size,leftMargin=margin,rightMargin=margin,title='원자재 물량 집계표 · 조합 리스트')
    templates=[];story=[]
    def start_page(identity,title,group=None):
        top=height-(112 if group else 99)
        frame=Frame(margin,footer_y+23,content_width,top-(footer_y+23),leftPadding=0,rightPadding=0,topPadding=0,bottomPadding=0,id=identity)
        templates.append(PageTemplate(id=identity,frames=[frame],onPage=header(title,group)))
        if story:story.extend([NextPageTemplate(identity),PageBreak()])
    if section in ('all','materials'):
        start_page('materials','원자재 물량 집계표')
        rows=[[r[k]for k,_ in MATERIAL_COLUMNS]for r in report['material_display_rows']]
        total_rows=[i for i,r in enumerate(report['material_display_rows'],1)if r['kind']!='detail']
        story += [table([label for _,label in MATERIAL_COLUMNS],rows,MATERIAL_WIDTHS,comma=True,totals=total_rows),Spacer(1,8),p(summary)]
    if section in ('all','combinations'):
        for gi,group in enumerate(report['combination_groups']):
            start_page(f'combination-{gi}','조합 리스트',group)
            rows=[];spans=[];ends=[]
            for pattern in group['patterns']:
                printed=list(combination_rows(pattern))
                start=len(rows)+1
                merge=len(printed)<=8
                for index,row in enumerate(printed):
                    cut_merge = 1 < row['length_rowspan'] <= 8
                    rows.append([row[key] if index==0 or col>=6 or (col==0 and not merge) else None
                                 for col,(key,_)in enumerate(COMBINATION_COLUMNS)])
                    if cut_merge:
                        for col in (6, 7):
                            spans.append(('SPAN',(col,len(rows)),(col,len(rows)+row['length_rowspan']-1)))
                    if not row['length_start']:
                        rows[-1][6:8] = [None, None]
                end=len(rows)
                if merge and end>start:
                    spans.extend(('SPAN',(col,start),(col,end))for col in range(6))
                ends.append(end)
            rows.append([group['total'][k]for k,_ in COMBINATION_COLUMNS])
            story.append(table([label for _,label in COMBINATION_COLUMNS],rows,COMBINATION_WIDTHS,spans=spans,ends=ends,totals=[len(rows)]))
    # Approval and validation details use their own section, keeping the reference forms uncluttered.
    if plan.get('splices') or plan.get('manufacturing') or report['excluded'] or report.get('import_exclusions') or section=='all':
        start_page('validation','수량 검증 · 이음 승인 대상')
        story += [p(summary,heading),p(notes),Spacer(1,8)]
        if plan.get('splices'):
            summary_splice=plan['splice_summary']
            status='검토 필요 · 이음 위치/연결 상세 승인 전 제작 확정 아님' if summary_splice['review_required'] else '사용자 승인 입력됨'
            story += [p(status,heading),p(f"통재 6m {summary_splice['whole_stock_bars']}본; 잔재 연결재 {summary_splice['reused_connectors']}개; 연결재 추가 발주 {summary_splice['extra_connector_stock_bars']}본. 이음 여유 {plan['settings']['splice_allowance']}mm/이음, 절단날 손실 {plan['settings']['kerf']}mm/절단. 두 손실은 별도 계산.")]
            story.append(table(['부재 번호','규격','완성길이','수량','본재길이','연결재길이','이음/부재','승인 상태'],
                [[a['title'],a['spec'],a['finished_length'],a['quantity'],a['base_length'],a['connector_length'],a['joint_count'],a['status']]for a in plan['splices']],
                [80,110,55,35,55,55,45,75]))
        if report['excluded']:
            story += [p('사용자가 확인하고 제외한 부재',heading),table(['행','부재 번호','규격','절단길이','수량','사유'],
                [[e['row'],e['title'],e['spec'],float(e['length']),e['quantity'],e['display_reason']]for e in report['excluded']],[30,80,110,55,35,140])]
        story += [p('원본 행별 수량 검증',heading),table(['행','Part Mark','BOM 수량','완성 확정','미확정','제외','제작 절편','판정'],
            [[r[k]for k in ('row','title','required_quantity','planned_quantity','pending_quantity','excluded_quantity','component_quantity','status')]for r in report['reconciliation']],
            [30,100,45,45,45,35,50,90])]
        story += [Spacer(1,8),p('실제 절단 잔재는 기본 >30mm이며 승인 ID가 있는 예외만 0~30mm를 허용합니다. 승인 이음의 무절단 본재는 날 손실 0mm. 원본 각 행의 제작 수량·길이·미확정 수량을 대조했습니다. 원자재 총중량은 단위중량 미제공 시 공란입니다.')]
        if plan.get('manufacturing'):
            start_page('purchase-comparison','신규 발주 · 현장 잔재 비교')
            story += [p('신규 발주 총길이를 먼저 최소화하고 같은 길이에서 신규 본수를 최소화합니다. 비교안별 제작 확정·미확정 수량이 다르면 동일 제작 범위의 절감액처럼 해석하지 않습니다. 로스는 날 손실·이음 여유·사용 후 잔재를 포함합니다.'),Spacer(1,8),
                table(['구분','발주 본수','신규 총길이','잔재 사용량','로스율 %','확정 수량','미확정 수량','최대 길이'],
                    [[r[k]for k in ('label','bar_count','total_new_stock_length','inventory_used_length','loss_rate','confirmed_quantity','pending_quantity','max_stock_length')]for r in plan['comparison']],
                    [120,45,75,70,45,45,45,65])]
            limits=plan['manufacturing']
            story += [Spacer(1,8),p(f"운송 원자재 상한: {limits['transport_max_length'] or '미입력'}mm / 가공 원자재 상한: {limits['processing_max_length'] or '미입력'}mm. 미입력은 추가 상한을 적용하지 않으며 자재별 발주 규칙은 유지합니다.")]
            if plan['pending']:
                story += [p('제작·발주 미확정 부재',heading),table(['행','Ass Mark','Part Mark','규격','길이','미확정 수량','사유'],
                    [[r[k]for k in ('row','assembly_mark','title','spec','length','quantity','reason')]for r in plan['pending']],
                    [30,50,65,100,55,45,145])]
            if limits['inventory']:
                used={r['id']:r for r in plan['inventory_usage']}
                story += [p('입력 잔재 · 보유 확인 및 사용 내역',heading),table(['재고 ID','재질','규격','보유 확인','사용 본수','절단 사용량','사용 후 잔재'],
                    [[r['id'],r['material'],r['spec'],'확인' if r['confirmed'] else '미확인·미사용',
                      used.get(r['id'],{}).get('used_quantity',0),used.get(r['id'],{}).get('used_length',0),used.get(r['id'],{}).get('remnant',0)]for r in limits['inventory']],
                    [70,50,110,70,50,70,70])]
            candidates=plan['manufacturing']['candidates']
            if candidates:
                start_page('approval-record','잔재 예외 · 이음 승인 기록')
                story += [p('승인 ID는 표시한 원본 수량·절단 패턴·재고·날 손실·이음 여유·길이 상한에만 유효합니다. 미승인 이음 및 보유 미확인 재고는 확정 발주에 포함하지 않습니다.'),Spacer(1,8),
                    table(['후보 ID','종류','규격','대상 수량','본재/원자재','연결재','잔철','재고 ID','승인 상태'],
                        [[c['id'],c['kind'],c['spec'],c['quantity'],c.get('base_length',c.get('stock_length')),c.get('connector_length'),c.get('remnant'),c.get('inventory_id'),
                          '승인 적용' if c['id'] in plan['manufacturing']['approved_candidate_ids'] else '미적용'] for c in candidates],
                        [90,65,95,40,60,45,40,55,55])]
        if plan.get('import_audit'):
            start_page('part-mark-check', 'Part Mark 수량 검증')
            story += [p('필요 수량 = 제작 확정 + 미확정 + 계산 예외 수량. 원본 Part Mark별 수량과 각 행의 제작 길이를 대조합니다.'),Spacer(1,8),
                table(['Part Mark','Grade','PROFILE','필요 수량','제작 확정','미확정','계산 예외','판정'],
                    [[r[k]for k in ('title','material','spec','required_quantity','planned_quantity','pending_quantity','excluded_quantity','status')]
                     for r in report['part_mark_reconciliation']], [75,50,115,45,45,45,45,90])]
            start_page('source-trace', '원본 Part Mark · Ass Mark 추적')
            story += [p('조합 비고는 Ass Mark_Part Mark로 표시하고, 아래 표는 원본 식별자를 각각 보존합니다. 조립 요약행 수량을 다시 곱하지 않습니다.'), Spacer(1,8),
                table(['행','Ass Mark','Part Mark','Grade','PROFILE','길이','원본 수량','계산 여부'],
                    [[i.get('source_row'),i.get('assembly_mark',''),i.get('title',''),i['material'],i['spec'],float(i['length']),i['quantity'],
                      '미확정 있음' if i.get('pending_quantity') else '포함' if i.get('included',True) else '계산 예외'] for i in plan['bom']],
                    [30,55,65,50,105,55,40,55])]
        if report.get('import_exclusions'):
            start_page('import-exclusions', '가져오기 제외 내역')
            story += [p('TS BOM LIST만 최적화 대상으로 읽었습니다. 아래 다른 시트의 셀 내용은 읽지 않았으며, 조립 요약행·소계/합계·대상 외 PROFILE은 절단 부재로 중복 집계하지 않습니다.'),Spacer(1,8),
                table(['시트','행','Ass Mark','Part Mark','PROFILE','길이','수량','제외 사유'],
                    [[e[k]for k in ('sheet','row','assembly_mark','title','spec','length','quantity','reason')]
                     for e in report['import_exclusions']], [65,30,40,45,90,40,35,120])]
    doc.addPageTemplates(templates)
    doc.build(story,canvasmaker=NumberedCanvas)
    return output.getvalue()
