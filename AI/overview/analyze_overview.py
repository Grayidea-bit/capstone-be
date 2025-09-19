from fastapi import APIRouter, HTTPException, Query
import httpx
import logging
import os

from ..setting import (
    validate_github_token,
    get_commit_number_and_list,
    generate_ai_content,
)

overview_router = APIRouter()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

            prompt = f"""
### **角色 (Role)**
你是一位頂尖的技術策略顧問。你的專長是將複雜的程式碼專案，轉化為非技術背景的決策者（如專案經理、市場團隊）也能理解的核心價值主張。

### **任務 (Task)**
根據提供的 GitHub 倉庫「首次提交的程式碼變更」與「README 文件」，撰寫一份**單一段落**、約 150 字的專案目的摘要。

### **上下文 (Context)**
1.  **分析基礎**: 你的分析**僅限於**首次提交 (序號: {first_commit_number}, SHA: {first_commit_sha}) 的內容，這代表了專案的初始架構和核心理念。
2.  **首次提交 Diff**:
    ```diff
    {diff_data}
    ```
3.  **README 文件**:
    ```markdown
    {readme_content if readme_content else "尚未提供 README 文件。"}
    ```

### **輸出要求 (Output Requirements)**
- **核心重點**: 聚焦於專案「解決什麼問題」和「預期目標是什麼」，而不是「如何實現」。
- **語氣風格**: 專業、簡潔、高度概括。
- **格式**: 盡量以條列式列出功能要點，並嚴格遵守"Markdown"格式。
- **開頭**: 請以「根據專案的初始版本分析...」作為開頭。

請開始生成摘要：
"""
            logger.info(
                f"送往 AI 服務的概覽提示詞 (模型: sonar-pro, 提示詞長度約: {len(prompt)} 字元): {prompt[:300]}..."
            )
            overview_text = await generate_ai_content(prompt)
            logger.info(
                f"AI 服務概覽結果 (模型: sonar-pro): {overview_text[:150]}..."
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