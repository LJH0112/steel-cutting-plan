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
                draw_fitted(can,f"[재질] {group['material']}    [규격] {group['spec']}    [단위중량]",margin,height-101,content_width)
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
    if plan.get('splices') or report['excluded'] or section=='all':
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
        story += [p('원본 행별 수량 검증',heading),table(['행','부재 번호','BOM 수량','완성 계획','제외','제작 절편','판정'],
            [[r[k]for k in ('row','title','required_quantity','planned_quantity','excluded_quantity','component_quantity','status')]for r in report['reconciliation']],
            [30,110,50,50,40,55,100])]
        story += [Spacer(1,8),p('실제 절단 후 잔재 >30mm. 무절단 이음 통재는 날 손실과 잔철 0mm. 원본 각 행의 제작 수량과 길이를 모두 대조했으며, 원자재 총중량은 단위중량 정보가 없으면 공란입니다.')]
    doc.addPageTemplates(templates)
    doc.build(story,canvasmaker=NumberedCanvas)
    return output.getvalue()
