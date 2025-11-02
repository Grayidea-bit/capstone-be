from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from .async_request import async_multiple_request
import logging
import httpx

repo_commit_router = APIRouter()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@repo_commit_router.get("/repos/{owner}/{repo}/{branch}")
async def get_commits(owner: str, repo: str,branch:str, access_token: str = Query(None)):
    if not access_token:
        logger.error("在獲取倉庫提交記錄請求中未提供 Access token。")
        raise HTTPException(status_code=401, detail="Access token is missing.")

    headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github.v3+json",
    }
    url = f"https://api.github.com/repos/{owner}/{repo}/commits"
        
    try:
        response = await async_multiple_request(url,headers,branch)

        sorted_response=list()
        for page in range(1, len(response) + 1):
            for context in response[page]:
                sorted_response.append(context)
            
        commit_info = [
            {
                "name": commit.get("commit", {}).get("message"),
                "sha": commit.get("sha"),
            }
            for commit in sorted_response
            if commit.get("sha") and commit.get("commit", {}).get("message")
        ]

        return JSONResponse({"commits": commit_info})

    except httpx.HTTPStatusError as e:
            logger.error(
                f"GitHub API 返回錯誤: {e.response.status_code} - {e.response.text}"
            )
            raise HTTPException(
                status_code=e.response.status_code,
                detail=f"GitHub API Error: {e.response.text}",
            )
    except httpx.RequestError as e:
            logger.error(f"發送請求時發生錯誤: {e}")
            raise HTTPException(status_code=500, detail="請求 GitHub API 時發生錯誤。")
    except Exception as e:
            logger.error(f"發生意外錯誤: {e}")
            raise HTTPException(status_code=500, detail="內部伺服器錯誤")
