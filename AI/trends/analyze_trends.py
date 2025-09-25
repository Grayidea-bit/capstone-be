# capstone-be/AI/trends/analyze_trends.py
from fastapi import APIRouter, HTTPException, Query
from typing import List, Dict
import httpx
from collections import Counter
import json # 導入 json

from ..setting import (
    validate_github_token,
    get_commit_number_and_list,
    generate_ai_content,
    logger,
    redis_client,       # 導入 redis_client
    CACHE_TTL_SECONDS   # 導入 CACHE_TTL_SECONDS
)

trends_router = APIRouter()


def classify_commit(message: str) -> str:
    """根據 commit message 關鍵字分類 commit 類型"""
    message = message.lower()
    if any(keyword in message for keyword in ["fix", "bug", "hotfix", "修復", "錯誤"]):
        return "錯誤修復"
    if any(keyword in message for keyword in ["feat", "feature", "新增", "功能"]):
        return "新功能"
    if any(keyword in message for keyword in ["perf", "performance", "optimize", "優化", "效能"]):
        return "效能優化"
    if any(keyword in message for keyword in ["refactor", "style", "format", "重構", "格式"]):
        return "程式碼重構"
    if any(keyword in message for keyword in ["docs", "doc", "文件", "註解"]):
        return "文件與註解"
    if any(keyword in message for keyword in ["test", "tests", "測試"]):
        return "測試相關"
    return "其他"


@trends_router.get("/repos/{owner}/{repo}/trends")
async def get_repository_trends(
    owner: str, repo: str, access_token: str = Query(None), limit: int = Query(50, ge=1, le=100)
):
    """分析最近 commit 的類型分佈"""
    if not access_token:
        raise HTTPException(status_code=401, detail="缺少 Access Token。")

    logger.info(
        f"收到倉庫趨勢分析請求: {owner}/{repo}",
        extra={"owner": owner, "repo": repo, "limit": limit},
    )

    if not await validate_github_token(access_token):
        raise HTTPException(status_code=401, detail="無效或過期的 GitHub token。")

    try:
        _, commits_data = await get_commit_number_and_list(owner, repo, access_token)
        if not commits_data:
            raise HTTPException(status_code=404, detail="倉庫中沒有 commits，無法進行分析。")

        recent_commits = commits_data[:limit]
        commit_summary = []
        category_counts = {}

        for commit in recent_commits:
            message = commit.get("commit", {}).get("message", "")
            category = classify_commit(message)
            commit_summary.append(f"- {message.splitlines()[0]} (分類: {category})")
            category_counts[category] = category_counts.get(category, 0) + 1

        summary_text = "\n".join(commit_summary)
        stats_text = "\n".join(
            [f"- {cat}: {count} 次" for cat, count in category_counts.items()]
        )

        prompt = f"""
### **角色 (Role)**
你是一位數據分析師，專門分析軟體開發專案的活躍度與趨勢。

### **任務 (Task)**
根據提供的最近 {len(recent_commits)} 筆 commit 紀錄和分類統計，撰寫一份簡潔的程式碼庫歷史趨勢分析報告。

### **上下文 (Context)**
* **Commit 分類統計**:
    ```
    {stats_text}
    ```
* **最近 Commit 列表**:
    ```
    {summary_text}
    ```

### **輸出要求 (Output Requirements)**
請以繁體中文，並嚴格遵循以下 Markdown 格式輸出報告：

#### 1. **近期開發重點總結**
* 根據 commit 的分類統計，總結近期的開發重心是什麼？ (例如：是著重在新功能開發，還是 bug 修復？)

#### 2. **趨勢觀察**
* 從 commit 的訊息中，有沒有觀察到特別的模式或趨勢？ (例如：最近是否都在處理同一個模組的功能？或是正在進行大規模的重構？)

#### 3. **開發活躍度評估**
* 綜合來看，這個專案的開發活躍度如何？

請開始生成趨勢分析報告：
"""
        analysis_text = await generate_ai_content(prompt)
        
        activity_analysis_data = await analyze_file_activity(owner, repo, access_token, commits_data)

        return {
            "trends_analysis": analysis_text,
            "statistics": category_counts,
            "commit_count": len(recent_commits),
            "activity_analysis": activity_analysis_data 
        }

    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"分析倉庫趨勢時發生意外錯誤: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"分析倉庫趨勢時發生意外錯誤: {str(e)}")


async def analyze_file_activity(owner: str, repo: str, access_token: str, commits_data: list, limit: int = 200):
    """分析最近 N 個 commit 的檔案和模組修改頻率，並加入快取機制"""
    
    # --- 新增快取邏輯 ---
    # 使用最新的 commit SHA 作為快取鍵的一部分，確保有新 commit 時快取會失效
    latest_commit_sha = commits_data[0]['sha']
    cache_key = f"activity_analysis:{owner}/{repo}:{latest_commit_sha}"

    if redis_client:
        try:
            cached_result = redis_client.get(cache_key)
            if cached_result:
                logger.info(f"檔案活躍度分析快取命中: {cache_key}")
                return json.loads(cached_result)
        except Exception as e:
            logger.error(f"讀取活躍度分析快取失敗: {e}", extra={"cache_key": cache_key})
    # --- 快取邏輯結束 ---

    logger.info(f"開始對 {owner}/{repo} 進行檔案活躍度分析，分析最近 {limit} 筆 commits。")
    
    commits_to_analyze = commits_data[:limit]
    all_changed_files = []

    async with httpx.AsyncClient() as client:
        for i, commit in enumerate(commits_to_analyze):
            sha = commit["sha"]
            logger.debug(f"正在獲取 commit #{i+1} ({sha[:7]}) 的檔案變更...")
            try:
                commit_details_res = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}",
                    headers={"Authorization": f"Bearer {access_token}"}
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
        module_name = file_path.split('/')[0]
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

    # --- 新增寫入快取邏輯 ---
    if redis_client:
        try:
            redis_client.set(cache_key, json.dumps(result), ex=CACHE_TTL_SECONDS)
            logger.info(f"已快取檔案活躍度分析結果: {cache_key}")
        except Exception as e:
            logger.error(f"寫入活躍度分析快取失敗: {e}", extra={"cache_key": cache_key})
    # --- 快取邏輯結束 ---
    
    return result