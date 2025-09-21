# capstone-be/AI/overview/analyze_overview.py
from fastapi import APIRouter, HTTPException, Query
import httpx
from ..setting import (
    validate_github_token,
    get_commit_number_and_list,
    generate_ai_content,
    MAX_CHARS_README,
    logger,
    redis_client,
    CACHE_TTL_SECONDS
)
import json

overview_router = APIRouter()

@overview_router.get("/repos/{owner}/{repo}")
async def get_repo_overview(owner: str, repo: str, access_token: str = Query(None)):
    if not access_token:
        raise HTTPException(status_code=401, detail="缺少 Access Token。")

    cache_key = f"overview:{owner}/{repo}"
    if redis_client:
        try:
            cached_result = redis_client.get(cache_key)
            if cached_result:
                logger.info(f"專案概覽快取命中: {cache_key}")
                return json.loads(cached_result)
        except Exception as e:
            logger.error(f"讀取 Redis 快取時發生錯誤: {e}", extra={"cache_key": cache_key})

    logger.info(
        f"收到倉庫概覽請求: {owner}/{repo}",
        extra={"owner": owner, "repo": repo},
    )
    if not await validate_github_token(access_token):
        raise HTTPException(status_code=401, detail="無效或過期的 GitHub token。")
        
    async with httpx.AsyncClient() as client:
        try:
            _, commits_data = await get_commit_number_and_list(
                owner, repo, access_token
            )
            if not commits_data:
                raise HTTPException(
                    status_code=404, detail="倉庫中沒有 commits，無法生成概覽。"
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
                    logger.info(f"成功獲取 README。長度: {len(readme_content)} 字元。")
                    if len(readme_content) > MAX_CHARS_README:
                        readme_content = readme_content[:MAX_CHARS_README] + "\n... [README 內容因過長已被截斷]"
                elif readme_response.status_code == 404:
                    logger.info(f"倉庫 {owner}/{repo} 無 README 文件。")
            except httpx.HTTPStatusError as e:
                 if e.response.status_code != 404:
                    logger.warning(f"獲取 README 時發生 HTTP 錯誤 (非 404): {str(e)}")
            
            latest_commit_sha = commits_data[0]["sha"]
            tree_response = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/git/trees/{latest_commit_sha}?recursive=1",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            file_structure_text = ""
            if tree_response.status_code == 200:
                tree_data = tree_response.json()
                file_paths = [item['path'] for item in tree_data.get('tree', []) if item.get('type') == 'blob']
                file_structure_text = "\n".join(file_paths)
            
            recent_commit_messages = [
                f"- {c.get('commit', {}).get('message', '').splitlines()[0]}"
                for c in commits_data[:15]
            ]
            commit_messages_text = "\n".join(recent_commit_messages)

            prompt = f"""
### **角色 (Role)**
你是一位頂尖的技術策略顧問與軟體架構師。你的專長是快速理解一個軟體專案的核心價值、主要功能與技術架構。

### **任務 (Task)**
根據提供的 GitHub 倉庫的綜合資訊，撰寫一份**單一段落**、約 150 字的專案目的與現況摘要。你的分析應**宏觀且全面**，不要過度聚焦於單一的細節。

### **核心分析資料 (Primary Information Sources)**
1.  **README 文件 (最重要)**: 這是理解專案目的和官方說明的首要依據。
    ```markdown
    {readme_content if readme_content else "這個專案尚未提供 README 文件。"}
    ```
2.  **專案檔案結構**: 這些檔案和目錄路徑揭示了專案的技術棧和架構。
    ```
    {file_structure_text[:1000] if file_structure_text else "無法獲取檔案結構。"}
    ```
3.  **近期開發動態 (Commit 訊息)**: 這些訊息標題反映了最近的開發重點。
    ```
    {commit_messages_text if commit_messages_text else "無法獲取 commit 訊息。"}
    ```

### **輸出要求 (Output Requirements)**
- **核心重點**: 綜合所有資訊，聚焦於專案「解決什麼問題」、「目前的核心功能是什麼」，以及「它的技術架構大概是怎樣的」。
- **語氣風格**: 專業、簡潔、高度概括。
- **格式**: 盡量以條列式列出功能要點，並嚴格遵守"Markdown"格式。
- **開頭**: 請以「這是一個...專案，旨在...」的形式作為開頭。

請開始生成摘要：
"""
            overview_text = await generate_ai_content(prompt)
            
            result = {
                "overview": overview_text,
                "file_structure": file_structure_text 
            }

            if redis_client:
                try:
                    redis_client.set(cache_key, json.dumps(result), ex=CACHE_TTL_SECONDS)
                    logger.info(f"已快取專案概覽: {cache_key}")
                except Exception as e:
                    logger.error(f"寫入 Redis 快取失敗: {e}", extra={"cache_key": cache_key})

            return result
            
        except httpx.HTTPStatusError as e:
            logger.error(
                f"獲取倉庫概覽時發生 GitHub API 錯誤: {str(e)}",
                extra={"url": str(e.request.url)},
            )
            detail = f"因 GitHub API 錯誤，無法生成倉庫概覽: {e.response.status_code} - {e.response.text}"
            raise HTTPException(status_code=e.response.status_code, detail=detail)
        except HTTPException as e:
            logger.error(f"獲取倉庫概覽時發生 HTTPException: {e.detail}")
            raise e
        except Exception as e:
            logger.error(f"獲取倉庫概覽時發生意外錯誤: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=500, detail=f"生成倉庫概覽時發生意外錯誤: {str(e)}"
            )