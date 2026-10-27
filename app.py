"""Run: python -m uvicorn app:app --host 127.0.0.1 --port 8000."""
import json
import logging
import threading
import time
from collections import OrderedDict
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from steelplan.bom import MAX_UPLOAD, inspect_bom, parse_bom, read_headers
from steelplan.exports import export_pdf, export_xlsx, sample_xlsx
from steelplan.models import OptimizeRequest
from steelplan.optimizer import optimize

ROOT = Path(__file__).resolve().parent
app = FastAPI(title='STEEL PLAN · 철골 절단 최적화', version='1.0.0')
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
app.mount('/fonts', StaticFiles(directory=ROOT / 'assets/fonts'), name='fonts')
RESULTS = OrderedDict()
RESULT_LOCK = threading.Lock()
SOLVE_LOCK = threading.Lock()
RESULT_TTL = 3600


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    details = ['.'.join(str(x) for x in e['loc'][1:]) + ': ' + e['msg'] for e in exc.errors()]
    return JSONResponse(status_code=422, content={'detail': '입력값을 확인해 주세요. ' + ' / '.join(details[:8])})


@app.get('/', include_in_schema=False)
def index():
    return FileResponse(ROOT / 'templates/index.html')


@app.get('/api/health')
def health():
    return {'status': 'ok'}


@app.get('/api/sample')
def sample():
    return Response(sample_xlsx(), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': 'attachment; filename="sample_bom.xlsx"'})


async def upload_bytes(file):
    try:
        if not file.filename or not file.filename.lower().endswith('.xlsx'):
            raise HTTPException(400, '.xlsx 형식만 지원합니다. .xls 파일은 .xlsx로 저장해 주세요.')
        data = await file.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, '업로드 파일은 10MB 이하만 가능합니다.')
        return data
    finally:
        await file.close()


async def import_operation(function, *args):
    try:
        return await run_in_threadpool(function, *args)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logging.exception('BOM import failed')
        raise HTTPException(400, '엑셀 파일을 읽지 못했습니다. 암호화·손상 여부와 .xlsx 형식을 확인해 주세요.') from exc


@app.post('/api/inspect')
async def inspect(file: UploadFile = File(...)):
    return await import_operation(inspect_bom, await upload_bytes(file))


@app.post('/api/headers')
async def headers(file: UploadFile = File(...), sheet: str = Form(...), header_row: int = Form(...)):
    return await import_operation(read_headers, await upload_bytes(file), sheet, header_row)


@app.post('/api/preview')
async def preview(file: UploadFile = File(...), sheet: str = Form(...), header_row: int = Form(...), mapping: str = Form(...)):
    try:
        mapped = json.loads(mapping)
        if not isinstance(mapped, dict):
            raise ValueError()
    except ValueError:
        raise HTTPException(400, '열 매핑 형식이 올바르지 않습니다.')
    return await import_operation(parse_bom, await upload_bytes(file), sheet, header_row, mapped)


@app.post('/api/optimize')
def calculate(request: OptimizeRequest):
    if not SOLVE_LOCK.acquire(blocking=False):
        raise HTTPException(429, '다른 계산이 진행 중입니다. 잠시 후 다시 시도해 주세요.')
    try:
        result = optimize(request)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        SOLVE_LOCK.release()
    job_id = uuid4().hex
    now = time.monotonic()
    with RESULT_LOCK:
        for key in list(RESULTS):
            if now - RESULTS[key][0] > RESULT_TTL:
                del RESULTS[key]
        RESULTS[job_id] = (now, result)
        while len(RESULTS) > 20:
            RESULTS.popitem(last=False)
    return {'job_id': job_id, **result}


@app.get('/api/results/{job_id}.{format}')
def download(job_id: str, format: str):
    if format not in ('xlsx', 'pdf'):
        raise HTTPException(404, '지원하지 않는 출력 형식입니다.')
    with RESULT_LOCK:
        saved = RESULTS.get(job_id)
        if saved is None or time.monotonic() - saved[0] > RESULT_TTL:
            RESULTS.pop(job_id, None)
            raise HTTPException(404, '결과가 만료되었거나 서버가 재시작되었습니다. 다시 계산해 주세요.')
        plan = saved[1]
    content = export_xlsx(plan) if format == 'xlsx' else export_pdf(plan)
    mime = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' if format == 'xlsx' else 'application/pdf'
    return Response(content, media_type=mime, headers={'Content-Disposition': f'attachment; filename="steel_cutting_plan.{format}"',
                                                       'Cache-Control': 'no-store'})
