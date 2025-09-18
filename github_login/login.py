from fastapi import APIRouter, HTTPException, Query
import os
import httpx
import logging

from dotenv import load_dotenv
load_dotenv()

login_router = APIRouter()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")


@login_router.get("/")
async def github_callback(code: str = Query(...)):
    logger.info(f"收到 GitHub OAuth code: {code[:10]}...")
    logger.info(f"GITHUB_CLIENT_ID={GITHUB_CLIENT_ID}, GITHUB_CLIENT_SECRET={'set' if GITHUB_CLIENT_SECRET else 'unset'}")
    logger.info(f"code={code}")
    async with httpx.AsyncClient() as client:
        try:
            token_response = await client.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": GITHUB_CLIENT_ID,
                    "client_secret": GITHUB_CLIENT_SECRET,
                    "code": code,
                },
                headers={"Accept": "application/json"},
            )
            token_response.raise_for_status()
            token_data = token_response.json()
            logger.info(
                f"GitHub token 響應 (部分): { {k: (v[:5]+'...' if isinstance(v, str) and len(v)>5 else v) for k,v in token_data.items()} }"
            )
            access_token = token_data.get("access_token")
            if not access_token:
                error = token_data.get("error", "未知錯誤")
                error_description = token_data.get("error_description", "未提供描述")
                logger.error(
                    f"從 GitHub 獲取 access_token 失敗: {error} - {error_description}"
                )
                raise HTTPException(
                    status_code=400,
                    detail=f"無法獲取 GitHub access token: {error_description}",
                )
            user_response = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            user_response.raise_for_status()
            user_data = user_response.json()
            logger.info(f"成功獲取 GitHub 用戶數據: login='{user_data.get('login')}'")
            return {
                "access_token": access_token,
                "user": {
                    "login": user_data.get("login"),
                    "avatar_url": user_data.get("avatar_url"),
                    "html_url": user_data.get("html_url"),
                },
            }
        except httpx.HTTPStatusError as e:
            logger.error(
                f"GitHub OAuth 回呼期間發生 HTTP 錯誤: {str(e)}, URL: {e.request.url}, Response: {e.response.text}"
            )
            raise HTTPException(
                status_code=e.response.status_code,
                detail=f"GitHub OAuth 回呼失敗: {e.response.text}",
            )
        except Exception as e:
            logger.error(f"GitHub OAuth 回呼期間發生意外錯誤: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=500, detail=f"GitHub OAuth 回呼期間發生意外錯誤: {str(e)}"
            )
