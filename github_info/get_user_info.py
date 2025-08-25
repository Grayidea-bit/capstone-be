import httpx
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

user_info_router = FastAPI()


@user_info_router.get("/")
async def get_user_info(access_token: Optional[str] = Query(None)):
    if not access_token:
        return JSONResponse(
            content={"error": "No access token provided"}, status_code=400
        )

    github_api_url = "https://api.github.com/user"
    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(github_api_url, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return JSONResponse(
                content={
                    "error": "Failed to fetch user from GitHub",
                    "details": exc.response.json(),
                },
                status_code=exc.response.status_code,
            )
        except httpx.RequestError as exc:
            return JSONResponse(
                content={
                    "error": "An error occurred while requesting GitHub API",
                    "details": str(exc),
                },
                status_code=503,
            )

    user_data = response.json()

    return JSONResponse(
        {"username": user_data.get("login"), "avatar_url": user_data.get("avatar_url")}
    )
