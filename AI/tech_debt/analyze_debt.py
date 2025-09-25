# capstone-be/AI/tech_debt/analyze_debt.py
from fastapi import APIRouter, HTTPException, Query
from ..setting import (
    validate_github_token,
    get_commit_number_and_list,
    generate_ai_content,
    logger,
    redis_client,      # 確保 redis_client 已導入
    CACHE_TTL_SECONDS  # 確保 CACHE_TTL_SECONDS 已導入
)
from ..trends.analyze_trends import analyze_file_activity
from ..code_analyzer import CodeAnalyzer
import httpx
from radon.complexity import cc_visit
from radon.metrics import mi_visit
import json

tech_debt_router = APIRouter()

def get_code_metrics(code: str):
    """使用 radon 分析程式碼的圈複雜度和可維護性指數"""
    try:
        if '\x00' in code:
            logger.warning("程式碼包含 null bytes，跳過 radon 分析。")
            return None
            
        complexity_results = cc_visit(code)
        maintainability_index = mi_visit(code, multi=True)
        
        high_complexity_functions = sorted(
            [f for f in complexity_results if f.complexity > 10], 
            key=lambda x: x.complexity, 
            reverse=True
        )[:3]

        return {
            "maintainability_index": maintainability_index,
            "high_complexity_functions": [
                f"{f.name} (Complexity: {f.complexity})" for f in high_complexity_functions
            ]
        }
    except Exception as e:
        logger.error(f"使用 radon 分析程式碼時發生錯誤: {e}", exc_info=True)
        return None


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

            # ***** 主要修改點：新增頂層快取 *****
            latest_commit_sha = commits_data[0]['sha']
            cache_key = f"tech_debt_analysis:{owner}/{repo}:{latest_commit_sha}"

            if redis_client:
                try:
                    cached_result = redis_client.get(cache_key)
                    if cached_result:
                        logger.info(f"技術債分析快取命中: {cache_key}")
                        return json.loads(cached_result)
                except Exception as e:
                    logger.error(f"讀取技術債分析快取失敗: {e}", extra={"cache_key": cache_key})
            # ***********************************

            activity_analysis = await analyze_file_activity(owner, repo, access_token, commits_data)
            hotspot_files = [file_info[0] for file_info in activity_analysis.get("top_files", [])]

            analyzer = CodeAnalyzer(owner, repo, access_token, client)
            hotspot_files_content = await analyzer.get_files_content(hotspot_files)

            code_smell_context = ""
            quantitative_analysis_text = ""
            for path, content in hotspot_files_content.items():
                truncated_content = content[:5000]
                code_smell_context += f"--- 檔案: `{path}` ---\n```\n{truncated_content}\n```\n\n"
                
                if path.endswith('.py'):
                    metrics = get_code_metrics(content)
                    if metrics:
                        quantitative_analysis_text += f"#### **檔案: `{path}`**\n"
                        quantitative_analysis_text += f"- **可維護性指數 (MI)**: {metrics['maintainability_index']:.2f} (越高越好，0-100)\n"
                        if metrics['high_complexity_functions']:
                            quantitative_analysis_text += "- **高圈複雜度函式**: " + ", ".join(metrics['high_complexity_functions']) + "\n"
                        else:
                            quantitative_analysis_text += "- **圈複雜度**: 良好，未發現高複雜度函式。\n"

            prompt = f"""
### **角色 (Role)**
你是一位對程式碼品質有極高要求的資深軟體架構師，擅長結合**量化指標**與**靜態程式碼分析**來識別 "Code Smells"。

### **任務 (Task)**
分析以下從專案中最頻繁變更的檔案（程式碼熱點）中所提取的程式碼和量化指標，深入識別潛在的技術債，並提出具體的重構建議。

### **1. 量化分析指標 (Quantitative Metrics)**
{quantitative_analysis_text if quantitative_analysis_text else "未能對熱點檔案生成量化分析指標 (可能熱點檔案非 Python 程式碼)。"}

### **2. 核心分析程式碼 (Code Hotspots)**
{code_smell_context if code_smell_context else "未能獲取熱點檔案的程式碼。"}

### **輸出要求 (Output Requirements)**
請以繁體中文，並嚴格遵循以下 Markdown 格式輸出報告，為每個部分提供具體的洞察和建議。

---

#### 1. **總體健康度評估**
* 綜合**量化指標**和**程式碼內容**，給出一個關於這些核心檔案程式碼品質的總體評價（例如：結構清晰、可維護性良好；或指標顯示 MI 偏低，且存在高複雜度函式，建議立即重構）。

#### 2. **Code Smell 分析與重構建議**
* **複雜度 (Complexity)**: 結合量化指標中的「高圈複雜度函式」列表，找出是否有過於冗長、邏輯過於複雜或巢狀過深的函式？請**引用程式碼範例**並說明如何簡化。
* **重複性 (Duplication)**: 找出是否有可以被抽象化或重用的重複程式碼片段？請**提供重構後的範例**。
* **職責不清 (Unclear Responsibilities)**: 是否有違反單一職責原則的類別或函式？它們是否做了太多事情？建議如何拆分？
* **潛在的強耦合 (Tight Coupling)**: 是否有模組不必要地依賴了另一個模組的內部實現細節？

#### 3. **建議的優先行動方案 (Action Plan)**
* 以條列方式，提出 2-3 個最值得優先處理的技術債項目（**優先處理量化指標最差的部分**），並簡要說明為什麼它們最重要。
"""
            analysis_text = await generate_ai_content(prompt)

            result = {
                "analysis": analysis_text,
                "activity_analysis": activity_analysis
            }
            
            # ***** 主要修改點：將結果存入快取 *****
            if redis_client:
                try:
                    redis_client.set(cache_key, json.dumps(result), ex=CACHE_TTL_SECONDS)
                    logger.info(f"已快取技術債分析結果: {cache_key}")
                except Exception as e:
                    logger.error(f"寫入技術債分析快取失敗: {e}", extra={"cache_key": cache_key})
            # ***********************************

            return result

        except HTTPException as e:
            raise e
        except Exception as e:
            logger.error(f"生成技術債報告時發生意外錯誤: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"生成技術債報告時發生意外錯誤: {str(e)}")