from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from google.api_core.exceptions import ResourceExhausted
from .AI.chat import chat_router
from .AI.diff import diff_router
from .AI.overview import overview_router
from .AI.setting import format_rate_limit_error
from .github_login.login import login_router
from .github_info.get_repo_commit import repo_commit_router
from .github_info.get_repo_list import repo_list_router
from .github_info.get_user_info import user_info_router
import logging


app = FastAPI()

# @chat_router.post("/repos/{owner}/{repo}")
app.include_router(chat_router, prefix="/chat", tags=["chat"])
# @diff_router.post("/repos/{owner}/{repo}/commits/{sha}")
app.include_router(diff_router, prefix="/diff", tags=["diff"])
# @overview_router.get("/repos/{owner}/{repo}")
app.include_router(overview_router, prefix="/overview", tags=["overview"])
app.include_router(login_router, prefix="/login", tags=["login"])
# @repo_commit_router.get("/repos/{owner}/{repo}"
app.include_router(repo_commit_router, prefix="/repo_commit", tags=["repo_commit"])
app.include_router(repo_list_router, prefix="/repo_list", tags=["repo_list"])
app.include_router(user_info_router, prefix="/user_info", tags=["user_info"])


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error(f"全域異常處理器捕獲到未處理的異常: {str(exc)}", exc_info=True)
    status_code = 500
    detail = f"伺服器內部錯誤: {str(exc)}"
    headers = {"Access-Control-Allow-Origin": "http://localhost:3000"}
    if isinstance(exc, HTTPException):
        status_code = exc.status_code
        detail = exc.detail
        if exc.headers:
            headers.update(exc.headers)
    elif isinstance(exc, ResourceExhausted):
        status_code = 429
        error_message, retry_delay = format_rate_limit_error(exc)
        detail = error_message
        headers["Retry-After"] = str(retry_delay)
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail},
        headers=headers,
    )
