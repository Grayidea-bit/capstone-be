from fastapi import APIRouter, HTTPException, Query
import httpx
import logging
import google.generativeai as genai
import os

from ..setting import (
    validate_github_token,
    get_commit_number_and_list,
    get_available_model,
    generate_gemini_content,
)

overview_router = APIRouter()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)

# 常數調整
MAX_CHARS_OVERVIEW_DIFF = 70000
MAX_CHARS_OVERVIEW_README = 15000


@overview_router.get("/repos/{owner}/{repo}")
async def get_repo_overview(owner: str, repo: str, access_token: str = Query(None)):
    if not access_token:
        raise HTTPException(status_code=401, detail="Access token is missing.")
    logger.info(
        f"收到倉庫概覽請求: owner={owner}, repo={repo}, token (前5碼)={access_token[:5]}..."
    )
    if not await validate_github_token(access_token):
        raise HTTPException(status_code=401, detail="Invalid or expired GitHub token.")
    async with httpx.AsyncClient() as client:
        try:
            commit_map, commits_data = await get_commit_number_and_list(
                owner, repo, access_token
            )
            if not commits_data:
                logger.info(f"倉庫 {owner}/{repo} 無 commits，無法生成概覽。")
                raise HTTPException(
                    status_code=404, detail="倉庫中沒有 commits，無法生成概覽。"
                )
            first_commit_obj = commits_data[-1]
            first_commit_sha = first_commit_obj["sha"]
            first_commit_number = commit_map.get(first_commit_sha)
            if first_commit_number is None:
                logger.error(
                    f"無法為最早的 commit SHA {first_commit_sha} 找到序號 (overview)。"
                )
                raise HTTPException(
                    status_code=500, detail="無法確定第一次 commit 的序號以生成概覽。"
                )
            diff_response = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/commits/{first_commit_sha}",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github.v3.diff",
                },
            )
            diff_response.raise_for_status()
            diff_data = diff_response.text
            logger.info(
                f"第一次 commit (序號: {first_commit_number}, SHA: {first_commit_sha}) 的 diff 已獲取。長度: {len(diff_data)} 字元。"
            )
            if len(diff_data) > MAX_CHARS_OVERVIEW_DIFF:
                logger.warning(
                    f"第一次 commit diff 過大 ({len(diff_data)} 字元)，將截斷至 {MAX_CHARS_OVERVIEW_DIFF} 字元。"
                )
                diff_data = (
                    diff_data[:MAX_CHARS_OVERVIEW_DIFF]
                    + "\n... [diff 內容因過長已被截斷]"
                )
            readme_content = ""
            try:
                readme_response = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/readme",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/vnd.github.raw",
                    },
                )
                if readme_response.status_code == 200:
                    readme_content = readme_response.text
                    logger.info(
                        f"成功獲取 {owner}/{repo} 的 README。長度: {len(readme_content)} 字元。"
                    )
                    if len(readme_content) > MAX_CHARS_OVERVIEW_README:
                        logger.warning(
                            f"README 內容過大 ({len(readme_content)} 字元)，將截斷至 {MAX_CHARS_OVERVIEW_README} 字元。"
                        )
                        readme_content = (
                            readme_content[:MAX_CHARS_OVERVIEW_README]
                            + "\n... [README 內容因過長已被截斷]"
                        )
                elif readme_response.status_code == 404:
                    logger.info(f"倉庫 {owner}/{repo} 無 README 文件。")
                else:
                    readme_response.raise_for_status()
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 404:
                    logger.warning(f"獲取 README 時發生 HTTP 錯誤 (非 404): {str(e)}")
            selected_model_name = get_available_model()
            logger.info(f"為倉庫概覽選擇的模型: {selected_model_name}")
            model_instance = genai.GenerativeModel(selected_model_name)
            prompt = f"""
你是一位資深的程式碼分析專家。請根據以下 GitHub 倉庫的「第一次 commit 的 diff」(序號: {first_commit_number}, SHA: {first_commit_sha}) 和 README（如果有的話），提供一個簡潔（約 100-200 字）、對非技術人員友好的程式碼功能大綱。
說明這個倉庫的核心功能和主要目的。請明確提及這是基於「第一次 commit」的分析。
**倉庫上下文**:
- 第一次 commit diff (序號: {first_commit_number}, SHA: {first_commit_sha}):
```diff
{diff_data}
```
- README 內容 (若可用):
```
{readme_content if readme_content else "未提供 README。"}
```

**你的任務**:
生成程式碼功能大綱。
"""
            logger.info(
                f"送往 Gemini 的概覽提示詞 (模型: {selected_model_name}, 提示詞長度約: {len(prompt)} 字元): {prompt[:300]}..."
            )
            overview_text = await generate_gemini_content(model_instance, prompt)
            logger.info(
                f"Gemini 概覽結果 (模型: {selected_model_name}): {overview_text[:150]}..."
            )
            return {"overview": overview_text}
        except httpx.HTTPStatusError as e:
            logger.error(
                f"獲取倉庫概覽時發生 GitHub API 錯誤: {str(e)}, URL: {e.request.url}, Response: {e.response.text}"
            )
            detail = f"因 GitHub API 錯誤，無法生成倉庫概覽: {e.response.status_code} - {e.response.text}"
            if e.response.status_code == 401:
                detail = "GitHub token 可能無效或已過期 (生成概覽時)。"
            elif e.response.status_code == 404:
                detail = f"為 {owner}/{repo} 生成概覽所需的數據未找到 (例如，commit 未找到)。"
            raise HTTPException(status_code=e.response.status_code, detail=detail)
        except HTTPException as e:
            logger.error(f"獲取倉庫概覽時發生 HTTPException: {e.detail}")
            raise e
        except Exception as e:
            logger.error(f"獲取倉庫概覽時發生意外錯誤: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=500, detail=f"生成倉庫概覽時發生意外錯誤: {str(e)}"
            )
