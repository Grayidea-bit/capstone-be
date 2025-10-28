from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from AI.setting import validate_github_token
from .async_request import async_multiple_request
import httpx
import logging

repo_branch_router = APIRouter()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@repo_branch_router.get("/{owner}/{repo}")
async def get_branch(owner: str, repo: str, access_token: str = Query(None)):
    if not access_token:
        logger.error("NO Access token。")
        raise HTTPException(status_code=401, detail="Access token is missing.")

    logger.info(f"access_token (前5碼): {access_token[:5]}...")
    if not await validate_github_token(access_token):
        logger.error("Invalid or expired GitHub token (get_branches)。")
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired GitHub token. Please login again.",
            headers={"WWW-Authenticate": "Bearer realm='GitHub OAuth'"},
        )

    url = f"https://api.github.com/repos/{owner}/{repo}/branches"
    headers={"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient() as client:
        try:
            response = await async_multiple_request(url,headers)
            sorted_response=list()
            for page in range(1, len(response) + 1):
                for context in response[page]:
                    sorted_response.append(context)
            
            branch_names = [b["name"] for b in sorted_response]
            logger.info(f"成功獲取 {len(branch_names)} 個 branch。")
            return JSONResponse({"branches": branch_names})

        except httpx.HTTPStatusError as e:
            logger.error(
                f"獲取分支列表時發生 HTTP 錯誤: {str(e)}, URL: {e.request.url}, Response: {e.response.text}"
            )
            detail = (
                f"無法獲取 branch list: {e.response.status_code} - {e.response.text}"
            )
            if e.response.status_code == 401:
                detail = "GitHub token 可能已在此期間失效。請重新登入。"
            raise HTTPException(
                status_code=e.response.status_code,
                detail=detail,
                headers=(
                    {"WWW-Authenticate": "Bearer realm='GitHub Branches'"}
                    if e.response.status_code == 401
                    else None
                ),
            )

        except Exception as e:
            logger.error(f"獲取 branch list 時發生意外錯誤: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=500, detail=f"獲取 branch list 時發生意外錯誤: {str(e)}"
            )
