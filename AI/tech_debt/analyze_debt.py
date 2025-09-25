# capstone-be/AI/tech_debt/analyze_debt.py
from fastapi import APIRouter, HTTPException, Query
from ..setting import (
    validate_github_token,
    get_commit_number_and_list,
    generate_ai_content,
    logger,
)
from ..trends.analyze_trends import analyze_file_activity
from ..code_analyzer import CodeAnalyzer # 導入新的分析器
import httpx

tech_debt_router = APIRouter()

@tech_debt_router.get("/repos/{owner}/{repo}/tech-debt")
async def get_tech_debt_report(owner: str, repo: str, access_token: str = Query(None)):
    if not access_token:
        raise HTTPException(status_code=401, detail="缺少 Access Token。")

    logger.info(
        f"收到技術債報告請求: {owner}/{repo}",
        extra={"owner": owner, "repo": repo},
    )

    if not await validate_github_token(access_token):
        raise HTTPException(status_code=401, detail="無效或過期的 GitHub token。")

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            _, commits_data = await get_commit_number_and_list(owner, repo, access_token)
            if not commits_data:
                raise HTTPException(status_code=404, detail="倉庫中沒有 commits，無法進行分析。")

            # 步驟 1: 執行熱點分析，找出最常變更的檔案 (維持不變)
            activity_analysis = await analyze_file_activity(owner, repo, access_token, commits_data)
            hotspot_files = [file_info[0] for file_info in activity_analysis["top_files"]]

            # 步驟 2: 使用 CodeAnalyzer 獲取熱點檔案的完整程式碼
            analyzer = CodeAnalyzer(owner, repo, access_token, client)
            hotspot_files_content = await analyzer.get_files_content(hotspot_files)

            # 步驟 3: 建立用於 Code Smell 分析的上下文
            code_smell_context = ""
            for path, content in hotspot_files_content.items():
                # 限制單一檔案長度，避免 prompt 過大
                code_smell_context += f"--- 檔案: `{path}` ---\n```python\n{content[:5000]}\n```\n\n" 

            # 步驟 4: 設計新的 AI Prompt，專注於 Code Smell
            prompt = f"""
### **角色 (Role)**
你是一位對程式碼品質有極高要求的資深軟體架構師，擅長靜態程式碼分析與識別 "Code Smells"。

### **任務 (Task)**
分析以下從專案中最頻繁變更的檔案（程式碼熱點）中所提取的程式碼，深入識別潛在的技術債，並提出具體的重構建議。

### **核心分析程式碼 (Code Hotspots)**
{code_smell_context if code_smell_context else "未能獲取熱點檔案的程式碼。"}

### **輸出要求 (Output Requirements)**
請以繁體中文，並嚴格遵循以下 Markdown 格式輸出報告，為每個部分提供具體的洞察和建議。

---

#### 1. **總體健康度評估**
* 綜合分析，給出一個關於這些核心檔案程式碼品質的總體評價（例如：結構清晰、輕微混亂、需要立即重構）。

#### 2. **Code Smell 分析與重構建議**
* **複雜度 (Complexity)**: 找出是否有過於冗長、邏輯過於複雜或巢狀過深的函式？請**引用程式碼範例**並說明如何簡化。
* **重複性 (Duplication)**: 找出是否有可以被抽象化或重用的重複程式碼片段？請**提供重構後的範例**。
* **職責不清 (Unclear Responsibilities)**: 是否有違反單一職責原則的類別或函式？它們是否做了太多事情？建議如何拆分？
* **潛在的強耦合 (Tight Coupling)**: 是否有模組不必要地依賴了另一個模組的內部實現細節？

#### 3. **建議的優先行動方案 (Action Plan)**
* 以條列方式，提出 2-3 個最值得優先處理的技術債項目，並簡要說明為什麼它們最重要。
"""
            analysis_text = await generate_ai_content(prompt)

            return {
                "analysis": analysis_text,
                "activity_analysis": activity_analysis # 仍然返回熱點分析的統計數據
            }

        except HTTPException as e:
            raise e
        except Exception as e:
            logger.error(f"生成技術債報告時發生意外錯誤: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"生成技術債報告時發生意外錯誤: {str(e)}")