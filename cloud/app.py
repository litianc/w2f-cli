"""Cloud deployment entry point; never imports the single-machine runtime."""
from __future__ import annotations

from dataclasses import dataclass
import html
import os
from pathlib import Path
import secrets
import tempfile

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from starlette.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import psycopg
import json

from cloud.store import Store, StoreError
from w2f_protocol.ingest import MAX_BODY, ProtocolError


@dataclass(frozen=True)
class Settings:
    environment: str
    release: str
    data_dir: Path
    admin_user: str
    admin_password: str
    database_password: str
    database_host: str = "db"
    database_port: int = 5432

    @classmethod
    def from_env(cls) -> "Settings":
        environment = os.environ["W2F_CLOUD_ENVIRONMENT"]
        if environment not in {"v040-local", "v040-ci", "v040-prototype"}:
            raise ValueError("An explicit v0.4.0 environment is required")
        data_dir = Path(os.environ["W2F_CLOUD_DATA_DIR"])
        if not data_dir.is_absolute() or data_dir.resolve() in {Path.home(), Path("/")}:
            raise ValueError("A dedicated data directory is required")
        data_dir = data_dir.resolve()
        passwords = [Path(os.environ[name]).read_text().strip() for name in
                     ("W2F_ADMIN_PASSWORD_FILE", "W2F_DATABASE_PASSWORD_FILE")]
        if any(len(password) < 24 for password in passwords):
            raise ValueError("Dedicated passwords must contain at least 24 characters")
        return cls(environment, os.environ.get("W2F_RELEASE", "local"), data_dir,
                   os.environ.get("W2F_ADMIN_USER", "admin"), *passwords,
                   os.environ.get("W2F_DATABASE_HOST", "db"),
                   int(os.environ.get("W2F_DATABASE_PORT", "5432")))


def check_dependencies(settings: Settings) -> None:
    with psycopg.connect(host=settings.database_host, port=settings.database_port,
                        dbname="w2f", user="w2f", password=settings.database_password,
                        connect_timeout=3, options="-c statement_timeout=3000") as connection:
        connection.execute("SELECT version FROM w2f_schema WHERE version=1").fetchone()
    # Test the actual writable mount, not just the existence of its directory.
    with tempfile.TemporaryFile(dir=settings.data_dir) as probe:
        probe.write(b"w2f-ready")
        probe.flush()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="W2F cloud", docs_url=None, redoc_url=None, openapi_url=None)
    basic = HTTPBasic()
    store = Store(settings)

    def device_token(request: Request):
        authorization = request.headers.get('authorization', '')
        if not authorization.startswith('Bearer '):
            raise HTTPException(401, 'Device authentication required')
        return authorization[7:]

    @app.exception_handler(StoreError)
    async def store_error(request, error):
        return JSONResponse({'error': error.code}, status_code=error.status)

    @app.exception_handler(ProtocolError)
    async def protocol_error(request, error):
        return JSONResponse({'error': 'invalid_request'}, status_code=422)

    @app.exception_handler(psycopg.Error)
    async def database_error(request, error):
        return JSONResponse({'error': 'storage_unavailable'}, status_code=503)

    @app.get('/api/v1/session')
    def session(token=Depends(device_token)):
        return store.session(token)

    @app.post('/api/v1/events')
    async def ingest(request: Request, token=Depends(device_token)):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BODY:
                return JSONResponse({'error': 'request_too_large'}, status_code=413)
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise ProtocolError('Invalid JSON') from None
        return await run_in_threadpool(store.ingest, token, body)

    @app.get('/api/v1/messages/{event_id}')
    def message(event_id: str, token=Depends(device_token)):
        return jsonable_encoder(store.message(event_id, token))

    def authenticate(credentials: HTTPBasicCredentials = Depends(basic)) -> None:
        user_ok = secrets.compare_digest(credentials.username.encode(), settings.admin_user.encode())
        password_ok = secrets.compare_digest(credentials.password.encode(), settings.admin_password.encode())
        if not (user_ok and password_ok):
            raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Basic"})

    @app.middleware("http")
    async def response_headers(request, call_next):
        response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
        })
        return response

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        try:
            check_dependencies(settings)
        except (psycopg.Error, OSError):
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ok"}

    @app.get("/api/status", dependencies=[Depends(authenticate)])
    def status():
        check = ready()
        if isinstance(check, JSONResponse):
            return check
        return {"status": "ok", "environment": settings.environment, "release": settings.release}

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(authenticate)])
    def dashboard():
        check = ready()
        healthy = not isinstance(check, JSONResponse)
        status_text = "服务已就绪" if healthy else "服务暂未就绪，请稍后重试"
        return HTMLResponse(f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>W2F 云端管理</title>
<style>body{{font:16px/1.7 system-ui;background:#f4f6f8;color:#17252e;margin:0}}
main{{max-width:760px;margin:10vh auto;padding:32px}}section{{background:white;border-radius:16px;padding:28px;margin:24px 0}}
small{{color:#536571}}h1{{margin:0}}.badge{{color:#216449}}</style><main>
<small>W2F · v0.4.0 云端原型</small><h1>云端管理</h1><section>
<h2 class="badge">{status_text}</h2><p>环境：{html.escape(settings.environment)}</p>
<p>版本：{html.escape(settings.release[:12])}</p></section><section>
<h2>文本采集接入已开放</h2><p>设备可上传文本并查询云端保存状态。用户、设备和绑定通过受限管理命令配置；飞书投递尚未启用。</p>
</section></main></html>""", status_code=200 if healthy else 503)

    return app
