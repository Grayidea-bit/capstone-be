from fastapi import APIRouter, HTTPException, Query
from ..AI.setting import validate_github_token
import httpx
import logging

repo_list_router = APIRouter()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@repo_list_router.get("/")
async def get_repos(access_token: str = Query(None)):
    if not access_token:
        logger.error("獲取倉庫列表請求中未提供 Access token。")
        raise HTTPException(status_code=401, detail="Access token is missing.")
    logger.info(f"收到獲取倉庫列表請求，access_token (前5碼): {access_token[:5]}...")
    if not await validate_github_token(access_token):
        logger.error("無效或過期的 GitHub token (get_repos)。")
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired GitHub token. Please login again.",
            headers={"WWW-Authenticate": "Bearer realm='GitHub OAuth'"},
        )
    async with httpx.AsyncClient() as client:
        try:
            repos_response = await client.get(
                "https://api.github.com/user/repos",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"type": "owner", "sort": "updated", "per_page": 100},
            )
            repos_response.raise_for_status()
            repos_response.raise_for_status()
            repos_data = repos_response.json()
            logger.info(f"成功獲取 {len(repos_data)} 個倉庫。")
            return repos_data
        except httpx.HTTPStatusError as e:
            logger.error(
                f"獲取倉庫列表時發生 HTTP 錯誤: {str(e)}, URL: {e.request.url}, Response: {e.response.text}"
            )
            detail = f"無法獲取倉庫列表: {e.response.status_code} - {e.response.text}"
            if e.response.status_code == 401:
                detail = "GitHub token 可能已在此期間失效。請重新登入。"
            raise HTTPException(
                status_code=e.response.status_code,
                detail=detail,
                headers=(
                    {"WWW-Authenticate": "Bearer realm='GitHub Repos'"}
                    if e.response.status_code == 401
                    else None
                ),
            )
        except Exception as e:
            logger.error(f"獲取倉庫列表時發生意外錯誤: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=500, detail=f"獲取倉庫列表時發生意外錯誤: {str(e)}"
            )
