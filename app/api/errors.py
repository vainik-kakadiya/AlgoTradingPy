"""Consistent JSON error responses: {"error": {"code", "message", "details"}}."""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.safety import SafetyViolation
from app.market_data.kite_client import KiteNotAuthenticated, KiteNotConfigured

logger = logging.getLogger(__name__)


def error_body(code: str, message: str, details: object = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details}}


class ApiError(HTTPException):
    def __init__(self, status_code: int, code: str, message: str, details: object = None) -> None:
        super().__init__(status_code=status_code, detail=message)
        self.code = code
        self.details = details


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(error_body(exc.code, str(exc.detail), exc.details), status_code=exc.status_code)

    @app.exception_handler(HTTPException)
    async def _http(_: Request, exc: HTTPException) -> JSONResponse:
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED", 409: "CONFLICT"}.get(exc.status_code, "HTTP_ERROR")
        return JSONResponse(error_body(code, str(exc.detail)), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"loc": ".".join(str(p) for p in e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        return JSONResponse(error_body("VALIDATION_ERROR", "Request validation failed", details), status_code=422)

    @app.exception_handler(SafetyViolation)
    async def _safety(_: Request, exc: SafetyViolation) -> JSONResponse:
        return JSONResponse(error_body("PAPER_ONLY", str(exc)), status_code=403)

    @app.exception_handler(LookupError)
    async def _lookup(_: Request, exc: LookupError) -> JSONResponse:
        return JSONResponse(error_body("NOT_FOUND", str(exc).strip("'\"")), status_code=404)

    @app.exception_handler(KiteNotConfigured)
    async def _kite_cfg(_: Request, exc: KiteNotConfigured) -> JSONResponse:
        return JSONResponse(error_body("KITE_NOT_CONFIGURED", str(exc)), status_code=400)

    @app.exception_handler(KiteNotAuthenticated)
    async def _kite_auth(_: Request, exc: KiteNotAuthenticated) -> JSONResponse:
        return JSONResponse(error_body("KITE_NOT_AUTHENTICATED", str(exc)), status_code=401)

    @app.exception_handler(ValueError)
    async def _value(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(error_body("BAD_REQUEST", str(exc)), status_code=400)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled API error")
        return JSONResponse(error_body("INTERNAL_ERROR", "Internal server error"), status_code=500)
