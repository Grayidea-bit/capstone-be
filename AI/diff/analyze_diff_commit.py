# capstone-be/AI/diff/analyze_diff_commit.py
from fastapi import APIRouter, HTTPException, Query
from ..setting import (
    validate_github_token,
    get_commit_number_and_list,
    generate_ai_content,
    logger,
    redis_client,
    CACHE_TTL_SECONDS
)
import httpx
import json

diff_router = APIRouter()


@diff_router.post("/repos/{owner}/{repo}/{branch}/commits/{sha}")
async def analyze_commit_diff(
    owner: str, repo: str,branch:str, sha: str, access_token: str = Query(None)
):
    print("===================be calling=====================")
    if not access_token:
        raise HTTPException(status_code=401, detail="缺少 Access Token。")

    cache_key = f"diff_analysis:{owner}/{repo}/{branch}/{sha}"
    if redis_client:
        try:
            cached_result = redis_client.get(cache_key)
            if cached_result:
                logger.info(f"Commit 分析快取命中: {cache_key}")
                return json.loads(cached_result)
        except Exception as e:
            logger.error(f"讀取 Redis 快取時發生錯誤: {e}", extra={"cache_key": cache_key})


    logger.info(
        f"收到 commit 分析請求: {owner}/{repo}/{sha}",
        extra={"owner": owner, "repo": repo, "sha": sha},
    )

    if not await validate_github_token(access_token):
        raise HTTPException(status_code=401, detail="無效或過期的 GitHub token。")

    async with httpx.AsyncClient() as client:
        try:
            commits_data = await get_commit_number_and_list(
                owner, repo,branch, access_token
            )
            if not commits_data:
                raise HTTPException(
                    status_code=404, detail="倉庫中沒有 commits，無法進行分析。"
                )

            target_commit_obj = next((c for c in commits_data if c["sha"] == sha), None)
            if not target_commit_obj:
                logger.warning(
                    f"目標 commit SHA {sha} 未在快取的 commit 列表中找到。將嘗試直接從 GitHub API 獲取。"
                )
                try:
                    headers = {
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/vnd.github.v3+json",
                    }
                    branch_info_url = f"https://api.github.com/repos/{owner}/{repo}/branches/{branch}"
                    branch_info_res = await client.get(
                        branch_info_url, headers=headers
                    )
                    branch_info_res.raise_for_status()
                    commit_sha = branch_info_res.json()["commit"]["sha"]
                    
                    target_commit_res = await client.get(
                        f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}",
                        headers=headers,
                        params={"sha": commit_sha},
                    )
                    target_commit_res.raise_for_status()
                    target_commit_obj=target_commit_res.json()
                except httpx.HTTPStatusError:
                    raise HTTPException(
                        status_code=404,
                        detail=f"目標 commit SHA {sha} 未在倉庫 {owner}/{repo} 中找到。",
                    )
                    
            commit_map= {commit["sha"]: i for i, commit in enumerate(reversed(commits_data), 1)}
            target_commit_number = commit_map.get(sha)   
            
            current_diff_response = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github.v3.diff",
                },
                params={"sha":branch},
            )
            current_diff_response.raise_for_status()
            current_diff_text = current_diff_response.text
            
            previous_diff_text = None
            previous_commit_sha = None
            previous_commit_number = None

            if target_commit_obj:
                target_index = commits_data.index(target_commit_obj)
                if target_index + 1 < len(commits_data):
                    previous_commit_obj = commits_data[target_index + 1]
                    previous_commit_sha = previous_commit_obj["sha"]
                    previous_commit_number = commit_map.get(previous_commit_sha)
                    if previous_commit_sha:
                        prev_diff_response = await client.get(
                            f"https://api.github.com/repos/{owner}/{repo}/commits/{previous_commit_sha}",
                            headers={
                                "Authorization": f"Bearer {access_token}",
                                "Accept": "application/vnd.github.v3.diff",
                            },
                            params={"sha":branch}
                        )
                        if prev_diff_response.status_code == 200:
                            previous_diff_text = prev_diff_response.text

            current_diff_for_prompt = current_diff_text
            if len(current_diff_for_prompt) > 60000:
                current_diff_for_prompt = current_diff_for_prompt[:60000] + "\n... [diff 因過長已被截斷]"

            previous_diff_for_prompt = previous_diff_text
            if previous_diff_for_prompt and len(previous_diff_for_prompt) > 15000:
                previous_diff_for_prompt = previous_diff_for_prompt[:15000] + "\n... [前一個 diff 因過長已被截斷]"

            prompt = f"""
### **角色 (Role)**
你是一位頂級的軟體架構師和程式碼品質專家。你的任務是進行一次深度 Code Review，不僅要理解變更的意圖，更要評估其品質和潛在風險。

### **任務 (Task)**
針對「當前 Commit」的程式碼變更，生成一份包含**品質評估**和**重構建議**的結構化審查報告。請使用「前一個 Commit」的內容作為比較的基準。

### **上下文 (Context)**
1.  **前一個 Commit (基準)** (序號: {previous_commit_number or 'N/A'}, SHA: {previous_commit_sha or 'N/A'}):
    ```diff
    {previous_diff_for_prompt if previous_diff_for_prompt else "無前一個 Commit 的 Diff 資訊。"}
    ```
2.  **當前 Commit (分析目標)** (序號: {target_commit_number or 'N/A'}, SHA: {sha}):
    ```diff
    {current_diff_for_prompt}
    ```

### **輸出格式 (Output Format)**
請以繁體中文，並嚴格遵循以下 Markdown 格式輸出報告。**每個部分都必須有具體、深入的內容**。

---

#### 1. 變更摘要 (Summary)
* **目的**: 一句話總結此 Commit 的核心意圖。
* **類型**: 標示出變更類型（例如：新功能、錯誤修復、重構、效能優化、文件更新）。

#### 2. 關鍵變更分析 (Key Changes Analysis)
* 以條列方式，深入分析主要的程式碼變更點，說明其**變更內容**與**變更原因**。

#### 3. 程式碼品質評估與重構建議 (Code Quality & Refactoring Suggestions)
* **品質評估**: 像靜態分析工具一樣，從以下幾點評估程式碼品質。對於發現的每個問題，請**引用程式碼中的具體範例**：
    * **可讀性**: 變數和函式命名是否清晰？程式碼結構是否易於理解？
    * **複雜度**: 是否存在過於複雜的邏輯、過深的巢狀迴圈或條件判斷？
    * **潛在 Bug**: 是否有明顯的邊界條件未處理？是否存在空指標風險？
    * **硬編碼 (Hardcoding)**: 是否有應被定義為常數的「魔法數字」或字串？
* **重構建議**: 針對上述評估出的問題，提出**具體可行**的重構或優化建議。如果沒有發現問題，請明確指出「**程式碼品質良好，暫無重構建議**」。

#### 4. 影響與價值 (Impact & Value)
* **正面影響**: 此變更對程式碼庫帶來了哪些具體好處？
* **解決的問題**: 是否解決了某個已知的問題或需求？

---
請開始生成報告：
"""
            analysis_text = await generate_ai_content(prompt)
            result = {
                "sha": sha,
                "diff": current_diff_text,
                "previous_diff": previous_diff_text,
                "analysis": analysis_text,
                "commit_number": target_commit_number,
                "previous_commit_number": previous_commit_number,
            }
            
            if redis_client:
                try:
                    redis_client.set(cache_key, json.dumps(result), ex=CACHE_TTL_SECONDS)
                    logger.info(f"已快取 Commit 分析結果: {cache_key}")
                except Exception as e:
                     logger.error(f"寫入 Redis 快取失敗: {e}", extra={"cache_key": cache_key})
                
            return result
            
        except httpx.HTTPStatusError as e:
            detail = f"因 GitHub API 錯誤，無法分析 commit diff: {e.response.status_code} - {e.response.text}"
            raise HTTPException(status_code=e.response.status_code, detail=detail)
        except HTTPException as e:
            logger.error(f"分析 commit diff 時發生 HTTPException: {e.detail}")
            raise e
        except Exception as e:
            logger.error(f"分析 commit diff 時發生意外錯誤: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=500, detail=f"分析 commit diff 時發生意外錯誤: {str(e)}"
            )
