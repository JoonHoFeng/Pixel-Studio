from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from typing import Literal
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.datastructures import UploadFile

from app.engine import ImageError, MAX_BYTES, MAX_PIXELS

BASE = Path(__file__).resolve().parent
STORE = Path(tempfile.gettempdir()) / f'pixel-local-{getattr(os, "getuid", lambda: 0)()}'
TTL = 30 * 60
WORKER_TIMEOUT = 30
ACTIVE: set[str] = set()
SLOTS = asyncio.Semaphore(2)


def error(code, message, status=400):
    return JSONResponse({'error': {'code': code, 'message': message}}, status_code=status)


def cleanup(now=None):
    now = time.time() if now is None else now
    if not STORE.exists():
        return
    for folder in STORE.iterdir():
        if folder.is_dir() and folder.name not in ACTIVE:
            try:
                if now - folder.stat().st_mtime >= TTL:
                    shutil.rmtree(folder)
            except FileNotFoundError:
                pass


async def janitor():
    while True:
        await asyncio.sleep(60)
        cleanup()


@asynccontextmanager
async def lifespan(app):
    STORE.mkdir(mode=0o700, parents=True, exist_ok=True)
    cleanup()
    task = asyncio.create_task(janitor())
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


app = FastAPI(title='像素工坊 · 本地图片转换', lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', '[::1]', 'testserver'])


class RequestTooLarge(Exception):
    pass


class BodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        limit = MAX_BYTES + 1024 * 1024 if scope['path'] == '/api/inspect' else 64 * 1024
        headers = dict(scope.get('headers', []))
        try:
            declared_size = int(headers.get(b'content-length', b'0'))
        except ValueError:
            return await error('INVALID_REQUEST', '请求长度无效。')(scope, receive, send)
        if declared_size > limit:
            return await error('FILE_TOO_LARGE', '请求体超过允许大小。图片上传上限为 30 MB。', 413)(scope, receive, send)
        total = 0
        async def bounded_receive():
            nonlocal total
            message = await receive()
            total += len(message.get('body', b''))
            if total > limit:
                raise RequestTooLarge()
            return message
        try:
            await self.app(scope, bounded_receive, send)
        except RequestTooLarge:
            await error('FILE_TOO_LARGE', '文件超过 30 MB 上传限制。', 413)(scope, receive, send)


app.add_middleware(BodyLimitMiddleware)


@app.middleware('http')
async def local_only(request: Request, call_next):
    origin = request.headers.get('origin')
    if request.method == 'POST' and origin and origin != str(request.base_url).rstrip('/'):
        return error('ORIGIN_REJECTED', '仅允许从本地工具页面发起请求。', 403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response


@app.exception_handler(ImageError)
async def image_error(request, exc):
    return error(exc.code, exc.message, 404 if exc.code == 'EXPIRED' else 400)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    return error('INVALID_OPTIONS', '参数无效：请检查整数宽高、格式、质量和背景颜色。', 422)


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    return error('INVALID_REQUEST', '请求无效或资源不存在。', exc.status_code)


class ConvertOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    image_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    width: int = Field(gt=0, le=MAX_PIXELS, strict=True)
    height: int = Field(gt=0, le=MAX_PIXELS, strict=True)
    format: Literal['jpeg', 'jpg', 'png', 'svg', 'gif', 'webp', 'raw', 'tiff', 'tif', 'ico']
    fit: Literal['contain', 'cover', 'stretch'] = 'contain'
    background: str = 'transparent'
    quality: int = Field(default=90, ge=1, le=100, strict=True)
    lossless: bool = False

    @field_validator('background')
    @classmethod
    def background_color(cls, value):
        if value != 'transparent' and not re.fullmatch(r'#[0-9a-fA-F]{6}', value):
            raise ValueError('Invalid color')
        return value


def existing(identifier):
    if not re.fullmatch(r'[a-f0-9]{32}', identifier):
        raise ImageError('EXPIRED', '图片不存在或已过期，请重新上传。')
    folder = STORE / identifier
    if not folder.is_dir() or time.time() - folder.stat().st_mtime >= TTL:
        raise ImageError('EXPIRED', '图片不存在或已过期，请重新上传。')
    return folder


def new_folder():
    STORE.mkdir(mode=0o700, parents=True, exist_ok=True)
    identifier = uuid.uuid4().hex
    folder = STORE / identifier
    folder.mkdir(mode=0o700)
    ACTIVE.add(identifier)
    return identifier, folder


async def run_job(folder: Path, job: dict):
    (folder / 'job.json').write_text(json.dumps(job, ensure_ascii=False))
    process = None
    async with SLOTS:
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, '-m', 'app.worker', str(folder / 'job.json'),
                cwd=BASE.parent, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(process.wait(), timeout=WORKER_TIMEOUT)
            except asyncio.TimeoutError as exc:
                raise ImageError('PROCESSING_TIMEOUT', '处理超过 30 秒，已终止任务。请减小图片或尺寸后重试。') from exc
            result_path = folder / 'worker-result.json'
            if process.returncode != 0 or not result_path.exists():
                raise ImageError('PROCESSING_FAILED', '图片处理进程异常结束，可能超过资源限制。')
            result = json.loads(result_path.read_text())
            if not result['ok']:
                raise ImageError(result['code'], result['message'])
            return result['data']
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            for name in ('job.json', 'worker-result.json'):
                (folder / name).unlink(missing_ok=True)


@app.post('/api/inspect')
async def inspect(request: Request):
    identifier, folder = new_folder()
    try:
        async with request.form(max_files=1, max_fields=0) as form:
            upload = form.get('file')
            if not isinstance(upload, UploadFile):
                raise ImageError('MISSING_FILE', '请选择一张图片。')
            size = 0
            with (folder / 'source').open('wb') as destination:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ImageError('FILE_TOO_LARGE', '文件超过 30 MB 上传限制。')
                    destination.write(chunk)
            if not size:
                raise ImageError('EMPTY_FILE', '上传文件为空。')
            filename = Path((upload.filename or 'image').replace('\\', '/')).name[:200]
        metadata = await run_job(folder, {'action': 'inspect', 'source': str(folder / 'source'), 'filename': filename})
        metadata.update(image_id=identifier, filename=filename, preview_url=f'/api/files/{identifier}/preview', expires_in=TTL)
        (folder / 'meta.json').write_text(json.dumps(metadata, ensure_ascii=False))
        os.utime(folder, None)
        return metadata
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    finally:
        ACTIVE.discard(identifier)


@app.post('/api/convert')
async def convert(options: ConvertOptions):
    source = existing(options.image_id)
    if not (source / 'source').is_file():
        raise ImageError('EXPIRED', '源图片不存在，请重新上传。')
    # Multiple conversions for the same source may overlap; mark each reference separately.
    os.utime(source, None)
    identifier, folder = new_folder()
    try:
        # Hard link isolates the input lifetime from cleanup and concurrent requests.
        os.link(source / 'source', folder / 'source')
        shutil.copyfile(source / 'meta.json', folder / 'meta.json')
        metadata = await run_job(folder, {'action': 'convert', 'source': str(folder / 'source'), 'options': options.model_dump()})
        (folder / 'source').unlink()
        ext = metadata['extension']
        metadata.update(result_id=identifier, preview_url=f'/api/files/{identifier}/preview',
                        download_url=f'/api/files/{identifier}/download',
                        filename=f'image-{options.width}x{options.height}.{ext}', expires_in=TTL)
        if metadata['animated']:
            metadata['preview_url'] = f'/api/files/{identifier}/animation'
        (folder / 'meta.json').write_text(json.dumps(metadata, ensure_ascii=False))
        os.utime(folder, None)
        return metadata
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    finally:
        ACTIVE.discard(identifier)


@app.get('/api/files/{identifier}/{kind}')
async def file_response(identifier: str, kind: Literal['preview', 'download', 'animation']):
    folder = existing(identifier)
    if not (folder / 'meta.json').exists():
        raise ImageError('EXPIRED', '图片尚未生成或已过期。')
    metadata = json.loads((folder / 'meta.json').read_text())
    if kind == 'preview':
        return FileResponse(folder / 'preview.png', media_type='image/png')
    ext = metadata.get('extension')
    if not ext:
        raise ImageError('EXPIRED', '生成结果不存在，请重新生成。')
    media = {'jpg': 'image/jpeg', 'png': 'image/png', 'svg': 'image/svg+xml', 'gif': 'image/gif',
             'webp': 'image/webp', 'tif': 'image/tiff', 'ico': 'image/x-icon', 'zip': 'application/zip'}[ext]
    if kind == 'animation' and ext not in ('gif', 'webp'):
        raise ImageError('INVALID_REQUEST', '此结果不是动画。')
    return FileResponse(folder / f'result.{ext}', media_type=media,
                        filename=metadata['filename'] if kind == 'download' else None)


app.mount('/', StaticFiles(directory=BASE / 'static', html=True), name='static')
