# capstone-be/AI/tech_debt/analyze_debt.py
from fastapi import APIRouter, HTTPException, Query
from ..setting import (
    validate_github_token,
    generate_ai_content,
    logger,
    redis_client,      # 確保 redis_client 已導入
    CACHE_TTL_SECONDS  # 確保 CACHE_TTL_SECONDS 已導入
)
from collections import Counter
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


@tech_debt_router.get("/repos/{owner}/{repo}/{branch}/tech-debt")
async def get_tech_debt_report(owner: str, repo: str,branch:str, access_token: str = Query(None)):
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
            commits_response = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/commits",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"per_page": 100,"page":1,"sha":branch} 
            )
            commits_response.raise_for_status()
            commits_data = commits_response.json()
            
            if not commits_data:
                raise HTTPException(status_code=404, detail="倉庫中沒有 commits，無法進行分析。")

            # ***** 主要修改點：新增頂層快取 *****
            latest_commit_sha = commits_data[0]['sha']
            cache_key = f"tech_debt_analysis:{owner}/{repo}/{branch}:{latest_commit_sha}"

            if redis_client:
                try:
                    cached_result = redis_client.get(cache_key)
                    if cached_result:
                        logger.info(f"技術債分析快取命中: {cache_key}")
                        return json.loads(cached_result)
                except Exception as e:
                    logger.error(f"讀取技術債分析快取失敗: {e}", extra={"cache_key": cache_key})
            # ***********************************

            activity_analysis = await analyze_file_activity(owner, repo,branch, access_token, commits_data)
            hotspot_files = [file_info[0] for file_info in activity_analysis.get("top_files", [])]

            analyzer = CodeAnalyzer(owner, repo,branch, access_token, client)
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
        
        
async def analyze_file_activity(owner: str, repo: str, branch:str, access_token: str, commits_data: list, limit: int = 200):
    """分析最近 N 個 commit 的檔案和模組修改頻率，並加入快取機制"""
    
    latest_commit_sha = commits_data[0]['sha']
    cache_key = f"activity_analysis:{owner}/{repo}/{branch}:{latest_commit_sha}:{limit}"

    if redis_client:
        try:
            cached_result = redis_client.get(cache_key)
            if cached_result:
                logger.info(f"檔案活躍度分析快取命中: {cache_key}")
                return json.loads(cached_result)
        except Exception as e:
            logger.error(f"讀取活躍度分析快取失敗: {e}", extra={"cache_key": cache_key})

    logger.info(f"開始對 {owner}/{repo} 進行檔案活躍度分析，分析最近 {limit} 筆 commits。")
    
    commits_to_analyze = commits_data[:limit]
    all_changed_files = []

    async with httpx.AsyncClient() as client:
        # (此處的程式碼維持不變)
        for i, commit in enumerate(commits_to_analyze):
            sha = commit["sha"]
            logger.debug(f"正在獲取 commit #{i+1} ({sha[:7]}) 的檔案變更...")
            try:
                commit_details_res = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params={"sha":branch}
                )
                commit_details_res.raise_for_status()
                commit_details = commit_details_res.json()
                
                if 'files' in commit_details:
                    for file in commit_details['files']:
                        all_changed_files.append(file['filename'])
            except httpx.HTTPStatusError as e:
                logger.warning(f"無法獲取 commit {sha} 的詳細資訊: {e}")
                continue

    file_counts = Counter(all_changed_files)
    
    module_counts = Counter()
    for file_path, count in file_counts.items():
        # 修正了潛在的 bug，確保即使檔案在根目錄也能正確處理
        module_name = file_path.split('/')[0] if '/' in file_path else file_path
        if module_name:
            module_counts[module_name] += count

    top_files_text = "\n".join([f"- `{path}`: {count} 次" for path, count in file_counts.most_common(10)])
    top_modules_text = "\n".join([f"- `{module}`: {count} 次" for module, count in module_counts.most_common(10)])

    prompt = f"""
### **角色 (Role)**
你是一位經驗豐富的首席工程師 (Principal Engineer)，擅長從程式碼庫的演進歷史中洞察架構的優劣和團隊的開發模式。

### **任務 (Task)**
根據提供的最近 {len(commits_to_analyze)} 筆 commit 中，檔案和模組的修改頻率統計，撰寫一份深入的**程式碼庫健康度與演進趨勢分析**。

### **核心分析數據 (Primary Data Sources)**
* **最常變更的檔案 (Top 10 Files Changed)**:
    ```
    {top_files_text}
    ```
* **最活躍的模組 (Top 10 Modules Changed)**:
    ```
    {top_modules_text}
    ```

### **輸出要求 (Output Requirements)**
請以繁體中文，並嚴格遵循以下 Markdown 格式輸出報告：

#### 1. **開發重心與核心模組識別**
* 根據「最活躍的模組」統計，分析近期專案的開發重心在哪裡？哪些模組是這個系統的核心？

#### 2. **潛在技術債分析 (程式碼熱點)**
* 根據「最常變更的檔案」列表，是否存在某些檔案被修改的頻率遠高於其他檔案？
* 如果存在這樣的「熱點」檔案，分析可能的原因（例如：該檔案職責過於龐大、設定檔經常變動、或是核心邏輯的集中點）。這是否暗示了潛在的技術債或需要進行重構？

#### 3. **架構健康度與耦合性評估**
* 綜合來看，這些變更分佈是否健康？是集中在少數幾個檔案，還是廣泛分佈在不同模組？一個健康的系統通常變更是分散的，而脆弱的系統則常常牽一髮而動全身，暗示著模組間的高度耦合。

請開始生成分析報告：
"""

    ai_analysis_text = await generate_ai_content(prompt)

    result = {
        "analysis_text": ai_analysis_text,
        "top_files": file_counts.most_common(10),
        "top_modules": module_counts.most_common(10)
    }

    if redis_client:
        try:
            redis_client.set(cache_key, json.dumps(result), ex=CACHE_TTL_SECONDS)
            logger.info(f"已快取檔案活躍度分析結果: {cache_key}")
        except Exception as e:
            logger.error(f"寫入活躍度分析快取失敗: {e}", extra={"cache_key": cache_key})
    
    return result