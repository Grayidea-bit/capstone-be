from fastapi import APIRouter, HTTPException, Query
import httpx
import unicodedata
from ..setting import (
    validate_github_token,
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

    logger.info(
        f"收到倉庫概覽請求: {owner}/{repo}",
        extra={"owner": owner, "repo": repo},
    )
    if not await validate_github_token(access_token):
        raise HTTPException(status_code=401, detail="無效或過期的 GitHub token。")
        
    async with httpx.AsyncClient() as client:
        try:
            commits_response = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/commits",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"per_page": 100,"page":1},
            )
            commits_response.raise_for_status()
            commits_data = commits_response.json()
            
            if not commits_data:
                raise HTTPException(
                    status_code=404, detail="倉庫中沒有 commits，無法生成概覽。"
                )

            latest_commit_sha = commits_data[0]["sha"]
            cache_key = f"overview:{owner}/{repo}:{latest_commit_sha}"
            if redis_client:
                try:
                    cached_result = redis_client.get(cache_key)
                    if cached_result:
                        logger.info(f"專案概覽快取命中: {cache_key}")
                        return json.loads(cached_result)
                except Exception as e:
                    logger.error(f"讀取專案概覽快取失敗: {e}", extra={"cache_key": cache_key})
            # ***********************************

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
                for c in commits_data[:100]
            ]
            commit_messages_text = "\n".join(recent_commit_messages)
            print("=================finish get file list===============")

            # --- (步驟 1：執行第一個 AI 任務 - 產生概覽) ---
            overview_prompt = f"""
### **角色 (Role)**
你是一位頂尖的技術策略顧問與軟體架構師。你的專長是快速理解一個軟體專案的核心價值、主要功能與技術架構。

### **任務 (Task)**
根據提供的 GitHub 倉庫的綜合資訊，撰寫一份**單一段落**、約 150 字的專案目的與現況摘要。你的分析應**宏觀且全面**，不要過度聚焦於單一的細節。

### **核心分析資料 (Primary Information Sources)**
1.  **README 文件 (最重要)**: 
    ```markdown
    {readme_content if readme_content else "這個專案尚未提供 README 文件。"}
    ```
2.  **專案檔案結構**:
    ```
    {file_structure_text[:1000] if file_structure_text else "無法獲取檔案結構。"}
    ```
3.  **近期開發動態 (Commit 訊息)**:
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
            try:
                overview_text = await generate_ai_content(overview_prompt)
                logger.info("成功生成 AI 概覽。")
            except Exception as e:
                logger.error(f"AI 概覽生成失敗: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail="AI 概覽生成失敗。")


            # --- (步驟 2：根據概覽，執行第二個 AI 任務 - 產生流程圖) ---
            flowchart_prompt = f"""
### **角色 (Role)**
你是一位專精於業務流程分析的系統分析師。

### **任務 (Task)**
根據以下提供的專案「AI 專案概覽」文字，將其核心功能和工作流程，轉換成一份能反映**條件分支**和**主要流程**的 PlantUML **活動圖 (Activity Diagram)**。

### **分析資料 (Project Overview Text)**
```
{overview_text}
```

### **輸出要求 (Output Requirements)**
1.  **重點**: 專注於概覽中提到的**核心功能**和**主要步驟**。
2.  **簡潔**: 忽略次要細節，保持圖表高層次且易於理解。
3.  **格式**: 輸出**必須**以 `@startuml` 開頭，並以 `@enduml` 結尾。
4.  **節點語言 (Node Language)**: 流程節點（即引號 "..." 內的文字）必須使用**繁體中文**。
5.  **語法**:
    * 遵循標準的 PlantUML 活動圖語法。
    * **以 `start` 關鍵字作為起點。**
    * **以 `(*)` 關鍵字作為終點。**
    * 使用 `-->` 串聯流程。
6.  **關鍵語法 - 條件分支 (If/Else):**
    * 如果流程中有效能會導致不同結果的「判斷點」，請務必使用 `if` 語法。
    * **範例 (僅供參考):**
        if (使用者是否登入？) then (是)
            --> "顯示使用者資料"
        else (否)
            --> "導向登入頁面"
        endif
        --> "繼續後續流程"
7.  **關鍵語法 - 平行處理 (Fork/Join):**
    * 如果多個任務可以同時進行，請使用 `fork`。
    * **範例 (僅供參考):**
        fork
            --> "任務 A"
        fork again
            --> "任務 B"
        endfork
        --> "匯總 A 和 B 的結果"
8.  **重要規則**:
    * 範例**僅用於說明語法**，你**絕對不能**在最終輸出中照抄範例中的文字。
    * 你的輸出**必須**基於「分析資料」({overview_text}) 的內容來生成。
9.  **語法關鍵**:
    * `if (...)` 括號中的條件文字，**絕對不能** 加上引號 (" ")。
    * **(錯誤範例):** `if ("是否通過驗證？") then (是)`
    * **(正確範例):** `if (是否通過驗證？) then (是)`
    * 只有活動節點（例如 `--> "..."`）才需要引號。
10. **(強化) 嚴格輸出 (Strict Output)**:
    * 你的回應**必須**直接是 PlantUML 代碼本身。
    * **絕對不要**在 `@startuml` ... `@enduml` 區塊之外包含任何解釋性文字、註解、開頭問候語（例如 "好的，這裏是..."）或結尾總結。
    * 你的唯一輸出就是代碼。
11. **(新增) 符號規範 (Symbol Rules)**:
    * 所有 PlantUML **語法**字元（例如 `-->`, `if`, `then`, `else`, `endif`, `fork`, `endfork`, `(*)`, `(`, `)`, `:`）**必須**使用**半形 (half-width)** 符號。
    * 在流程節點（引號內的文字）之外使用任何全形符號（例如 `：`、`（`、`）`、`－`、`＞`）都將導致語法錯誤，必須禁止。

請開始生成 PlantUML：
"""
            
            try:
                plantuml_code = await generate_ai_content(flowchart_prompt)
                print(plantuml_code)
                logger.info("成功生成 PlantUML 流程圖。")
            except Exception as e:
                logger.error(f"AI PlantUML 流程圖生成失敗: {e}", exc_info=True)
                # 即使流程圖失敗，我們還是可以回傳概覽，只是 PlantUML 會是空的
                plantuml_code = "@startuml\n' 流程圖生成失敗: {e}\n@enduml"

            
            result = {
                "overview": overview_text,
                "file_structure": file_structure_text,
                "plantuml_code": plantuml_code
            }

            # ***** 將結果存入快取 *****
            if redis_client:
                try:
                    redis_client.set(cache_key, json.dumps(result), ex=CACHE_TTL_SECONDS)
                    logger.info(f"已快取專案概覽 (含流程圖): {cache_key}")
                except Exception as e:
                    logger.error(f"寫入專案概覽快取失敗: {e}", extra={"cache_key": cache_key})
            # ***********************************
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
