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


@diff_router.post("/repos/{owner}/{repo}/commits/{sha}")
async def analyze_commit_diff(
    owner: str, repo: str, sha: str, access_token: str = Query(None)
):
    if not access_token:
        raise HTTPException(status_code=401, detail="缺少 Access Token。")

    cache_key = f"diff_analysis:{owner}/{repo}/{sha}"
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
            commit_map, commits_data = await get_commit_number_and_list(
                owner, repo, access_token
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
                    target_commit_res = await client.get(
                        f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}",
                        headers={"Authorization": f"Bearer {access_token}"},
                    )
                    target_commit_res.raise_for_status()
                    logger.info(
                        f"目標 commit SHA {sha} 在 GitHub 上找到，但不在本地快取中。繼續分析，但可能缺少序號上下文。"
                    )
                except httpx.HTTPStatusError:
                    raise HTTPException(
                        status_code=404,
                        detail=f"目標 commit SHA {sha} 未在倉庫 {owner}/{repo} 中找到。",
                    )

            target_commit_number = commit_map.get(sha)
            if target_commit_number is None:
                logger.warning(f"無法為 SHA {sha} 計算序號 (analyze)，將不顯示序號。")

            current_diff_response = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github.v3.diff",
                },
            )
            current_diff_response.raise_for_status()
            current_diff_text = current_diff_response.text
            logger.info(
                f"已獲取目標 commit (序號: {target_commit_number or 'N/A'}, SHA: {sha}) 的 diff。長度: {len(current_diff_text)}"
            )

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
                        )
                        if prev_diff_response.status_code == 200:
                            previous_diff_text = prev_diff_response.text
                            logger.info(
                                f"已獲取前一個 commit (序號: {previous_commit_number or 'N/A'}, SHA: {previous_commit_sha}) 的 diff。長度: {len(previous_diff_text)}"
                            )
                        else:
                            logger.warning(
                                f"獲取前一個 commit {previous_commit_sha} 的 diff 失敗: 狀態碼 {prev_diff_response.status_code}"
                            )
            else:
                logger.info(
                    f"目標 commit SHA {sha} 不在快取中，無法自動確定前一個 commit。"
                )

            current_diff_for_prompt = current_diff_text
            if len(current_diff_for_prompt) > 60000:
                logger.warning(
                    f"當前 commit diff ({len(current_diff_for_prompt)} 字元) 過長，截斷至 60000。"
                )
                current_diff_for_prompt = (
                    current_diff_for_prompt[:60000] + "\n... [diff 因過長已被截斷]"
                )

            previous_diff_for_prompt = previous_diff_text
            if previous_diff_for_prompt and len(previous_diff_for_prompt) > 15000:
                logger.warning(
                    f"前一個 commit diff ({len(previous_diff_for_prompt)} 字元) 過長，截斷至 15000。"
                )
                previous_diff_for_prompt = (
                    previous_diff_for_prompt[:15000]
                    + "\n... [前一個 diff 因過長已被截斷]"
                )

            prompt = f"""
### **角色 (Role)**
你是一位專業的軟體架構師兼程式碼審查（Code Review）專家。你擅長從細微的程式碼變更中，洞察其對系統穩定性、可維護性和未來擴展性的深遠影響。

### **任務 (Task)**
針對「當前 Commit」的程式碼變更，生成一份結構化的審查報告。請使用「前一個 Commit」的內容作為基準，以評估變更的演進和合理性。

### **上下文 (Context)**
1.  **前一個 Commit (基準)** (序號: {previous_commit_number or 'N/A'}, SHA: {previous_commit_sha or 'N/A'}):
    ```diff
    {previous_diff_for_prompt if previous_diff_for_prompt else "無前一個 Commit 的 Diff 資訊，或這是首次提交。"}
    ```
2.  **當前 Commit (分析目標)** (序號: {target_commit_number or 'N/A'}, SHA: {sha}):
    ```diff
    {current_diff_for_prompt}
    ```

### **輸出格式 (Output Format)**
請以繁體中文，並嚴格遵循以下 Markdown 格式輸出你的分析報告，並確保每個部分都有具體、深入的內容：

#### 1. 變更摘要 (Summary)
* **目的**: 一句話總結此 Commit 的核心意圖。
* **類型**: 標示出變更類型（例如：新功能、錯誤修復、重構、效能優化、文件更新）。

#### 2. 關鍵變更分析 (Key Changes Analysis)
以條列方式，深入分析主要的程式碼變更點。對於每項變更，請說明：
* **變更內容**: 具體修改了什麼？（例如：引入了新的 `ApiService` 類別）
* **變更原因**: 為什麼需要這個變更？（例如：為了將 API 請求邏輯與業務邏輯解耦）

#### 3. 影響與價值 (Impact & Value)
* **正面影響**: 此變更對程式碼庫帶來了哪些具體好處？（例如：提升了可讀性、降低了未來修改的風險）。
* **解決的問題**: 是否解決了某個已知的問題或需求？

#### 4. 潛在風險與建議 (Potential Risks & Suggestions)
* **風險評估**: (可選) 是否引入了新的風險？（例如：是否有未處理的邊界情況？是否可能影響效能？）
* **改進建議**: (可選) 是否有更優雅或更穩健的實現方式？

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
            logger.error(
                f"分析 commit diff 時發生 GitHub API 錯誤: {str(e)}",
                extra={"url": str(e.request.url)},
            )
            detail = f"因 GitHub API 錯誤，無法分析 commit diff: {e.response.status_code} - {e.response.text}"
            if e.response.status_code == 401:
                detail = "GitHub token 可能無效或已過期 (分析 commit diff 時)。"
            elif e.response.status_code == 404:
                detail = f"Commit SHA {sha} 或相關數據未在倉庫 {owner}/{repo} 中找到以進行分析。"
            elif e.response.status_code == 422:
                detail = f"無法處理 commit SHA {sha} 的 diff (可能是無效的 SHA 或 diff 過大): {e.response.text}"
            raise HTTPException(status_code=e.response.status_code, detail=detail)
        except HTTPException as e:
            logger.error(f"分析 commit diff 時發生 HTTPException: {e.detail}")
            raise e
        except Exception as e:
            logger.error(f"分析 commit diff 時發生意外錯誤: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=500, detail=f"分析 commit diff 時發生意外錯誤: {str(e)}"
            )
