# capstone-be/AI/pr/analyze_pr.py
from fastapi import APIRouter, HTTPException, Query, Body
from typing import Dict
import httpx
from ..setting import (
    validate_github_token,
    generate_ai_content,
    MAX_CHARS_PR_DIFF,
    logger,
    redis_client,      # 確保導入
    CACHE_TTL_SECONDS  # 確保導入
)
import json # 確保導入

pr_router = APIRouter()

async def post_comment_to_github_pr(
    owner: str,
    repo: str,
    pull_number: int,
    access_token: str,
    comment_body: str
):
    """將評論發佈到指定的 Pull Request。"""
    comment_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pull_number}/comments"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(comment_url, json={"body": comment_body}, headers=headers)
            response.raise_for_status()
            logger.info(f"成功將評論發佈至 PR #{pull_number}")
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(
                f"發佈評論到 PR #{pull_number} 時發生 GitHub API 錯誤: {e.response.text}",
            )
            raise HTTPException(status_code=e.response.status_code, detail=f"GitHub API Error: {e.response.text}")
        except Exception as e:
            logger.error(f"發佈評論到 PR #{pull_number} 時發生意外錯誤: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="發佈評論時發生未知錯誤")


@pr_router.get("/repos/{owner}/{repo}/pulls/{pull_number}")
async def analyze_pr_diff(
    owner: str,
    repo: str,
    pull_number: int,
    access_token: str = Query(None),
):
    if not access_token:
        raise HTTPException(status_code=401, detail="缺少 Access Token。")

    logger.info(f"收到 PR 分析請求: {owner}/{repo}/pulls/{pull_number}")

    if not await validate_github_token(access_token):
        raise HTTPException(status_code=401, detail="無效或過期的 GitHub token。")

    async with httpx.AsyncClient() as client:
        try:
            pr_info_response = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            pr_info_response.raise_for_status()
            pr_data = pr_info_response.json()
            
            # ***** 主要修改點：獲取 head SHA 並建立快取鍵 *****
            pr_head_sha = pr_data.get("head", {}).get("sha")
            if not pr_head_sha:
                raise HTTPException(status_code=404, detail="無法獲取 PR 的 head SHA。")

            cache_key = f"pr_analysis:{owner}/{repo}:{pull_number}:{pr_head_sha}"
            if redis_client:
                try:
                    cached_result = redis_client.get(cache_key)
                    if cached_result:
                        logger.info(f"PR 分析快取命中: {cache_key}")
                        return json.loads(cached_result)
                except Exception as e:
                    logger.error(f"讀取 PR 分析快取失敗: {e}", extra={"cache_key": cache_key})
            # *************************************************

            pr_title = pr_data.get("title", "")
            pr_body = pr_data.get("body", "")
            
            diff_response = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github.v3.diff",
                },
            )
            diff_response.raise_for_status()
            pr_diff = diff_response.text

            logger.info(f"成功獲取 PR #{pull_number} 的 diff，長度: {len(pr_diff)} 字元。")

            if len(pr_diff) > MAX_CHARS_PR_DIFF:
                pr_diff = pr_diff[:MAX_CHARS_PR_DIFF] + "\n... [diff 因過長已被截斷]"

            prompt = f"""
### **角色 (Role)**
你是一位資深的軟體工程師，擅長進行程式碼審查 (Code Review)。你的分析應該客觀、具建設性且易於理解。

### **任務 (Task)**
根據提供的 Pull Request (PR) 資訊，包含標題、描述和程式碼變更 (diff)，撰寫一份專業的 Code Review 報告。

### **上下文 (Context)**
* **PR 標題**: {pr_title}
* **PR 描述**:
    ```
    {pr_body if pr_body else "此 PR 未提供描述。"}
    ```
* **程式碼變更 (Diff)**:
    ```diff
    {pr_diff}
    ```

### **輸出要求 (Output Requirements)**
請以繁體中文，並嚴格遵循以下 Markdown 格式輸出報告：

#### 1. **PR 目的總結**
* 根據 PR 的標題和描述，簡要總結這次變更的核心目的。

#### 2. **主要變更分析**
* 以條列方式，分析程式碼中最核心的幾項變更。
* 說明這些變更可能帶來的正面影響 (如：效能提升、程式碼可讀性增加、解決了某個 bug)。

#### 3. **潛在問題與建議**
* (可選) 指出程式碼中可能存在的潛在風險、未處理的邊界情況或可以改進的地方。
* (可選) 提出具體的修改建議。如果沒有，可以寫「從程式碼變更來看，目前沒有發現明顯的潛在問題。」
"""
            analysis_text = await generate_ai_content(prompt)
            
            result = {"pull_request_analysis": analysis_text}

            # ***** 主要修改點：將結果存入快取 *****
            if redis_client:
                try:
                    redis_client.set(cache_key, json.dumps(result), ex=CACHE_TTL_SECONDS)
                    logger.info(f"已快取 PR 分析結果: {cache_key}")
                except Exception as e:
                    logger.error(f"寫入 PR 分析快取失敗: {e}", extra={"cache_key": cache_key})
            # ***********************************

            return result

        except httpx.HTTPStatusError as e:
            detail = f"因 GitHub API 錯誤，無法分析 PR: {e.response.status_code} - {e.response.text}"
            raise HTTPException(status_code=e.response.status_code, detail=detail)
        except Exception as e:
            logger.error(f"分析 PR 時發生意外錯誤: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"分析 PR 時發生意外錯誤: {str(e)}")


@pr_router.post("/repos/{owner}/{repo}/pulls/{pull_number}/comments")
async def post_pr_comment(
    owner: str,
    repo: str,
    pull_number: int,
    payload: Dict = Body(...),
    access_token: str = Query(None),
):
    if not access_token:
        raise HTTPException(status_code=401, detail="缺少 Access Token。")

    comment = payload.get("comment")
    if not comment:
        raise HTTPException(status_code=400, detail="評論內容不得為空。")

    if not await validate_github_token(access_token):
        raise HTTPException(status_code=401, detail="無效或過期的 GitHub token。")
    
    comment_to_post = f"### 🤖 AI Code Review 報告\n\n" + comment
    
    result = await post_comment_to_github_pr(owner, repo, pull_number, access_token, comment_to_post)
    
    return {"message": "評論已成功發佈！", "comment_url": result.get("html_url")}