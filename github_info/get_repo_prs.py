# capstone-be/github_info/get_repo_prs.py
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
import logging
import httpx

pr_list_router = APIRouter()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@pr_list_router.get("/repos/{owner}/{repo}/pulls")
async def get_pull_requests(owner: str, repo: str, access_token: str = Query(None)):
    if not access_token:
        logger.error("在獲取 PR 列表請求中未提供 Access token。")
        raise HTTPException(status_code=401, detail="Access token is missing.")

    async with httpx.AsyncClient() as client:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        url = f"https://api.github.com/repos/{owner}/{repo}/pulls"

        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()

            prs_data = response.json()
            
            pr_info = [
                {
                    "number": pr.get("number"),
                    "title": pr.get("title"),
                    "sha": pr.get("head", {}).get("sha")
                }
                for pr in prs_data
            ]

            return JSONResponse(pr_info)

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