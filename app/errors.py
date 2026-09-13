"""统一错误处理：所有业务错误返回一致的 4xx JSON 结构。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """业务校验错误，映射为明确的 4xx 响应。"""

    def __init__(self, status_code: int, code: str, message: str, details: dict | None = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def _payload(code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic 校验失败（缺字段、类型错误等）→ 422，并给出可读明细
        details = [
            {"field": ".".join(str(p) for p in e.get("loc", [])), "reason": e.get("msg", "")}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_payload("VALIDATION_ERROR", "请求字段缺失或格式不合法", {"fields": details}),
        )
