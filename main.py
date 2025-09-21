# capstone-be/main.py
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from AI.chat.chatting_repo import chat_router
from AI.diff.analyze_diff_commit import diff_router
from AI.overview.analyze_overview import overview_router
from AI.pr.analyze_pr import pr_router
from AI.trends.analyze_trends import trends_router
from github_login.login import login_router
from github_info.get_repo_commit import repo_commit_router
from github_info.get_repo_list import repo_list_router
from github_info.get_user_info import user_info_router
from github_info.get_repo_prs import pr_list_router
import logging
from AI.setting import logger

from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()


app.include_router(chat_router, prefix="/chat", tags=["對話 (Chat)"])
app.include_router(diff_router, prefix="/diff", tags=["Commit 分析"])
app.include_router(overview_router, prefix="/overview", tags=["專案概覽 (Overview)"])
app.include_router(pr_router, prefix="/pr", tags=["Pull Request 分析"])
app.include_router(pr_list_router, prefix="/pr", tags=["獲取 PRs"])
app.include_router(trends_router, prefix="/trends", tags=["倉庫趨勢分析 (Trends)"])
app.include_router(login_router, prefix="/login", tags=["GitHub 登入"])
app.include_router(repo_commit_router, prefix="/repo_commit", tags=["獲取 Commits"])
app.include_router(repo_list_router, prefix="/repo_list", tags=["獲取 Repos"])
app.include_router(user_info_router, prefix="/user_info", tags=["獲取使用者資訊"])

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全域異常處理器 (保持不變)
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    status_code = 500
    detail = f"伺服器內部錯誤: {str(exc)}"
    
    extra_info = {
        "url": str(request.url),
        "method": request.method,
    }

    if isinstance(exc, HTTPException):
        status_code = exc.status_code
        detail = exc.detail
        logger.warning(
            f"HTTPException 被捕獲: {detail}",
            extra={**extra_info, "status_code": status_code},
        )
    else:
        logger.error(
            f"未處理的異常: {str(exc)}",
            exc_info=True,
            extra=extra_info,
        )

    return JSONResponse(
        status_code=status_code,
        content={"detail": detail},
    )