from fastapi import APIRouter, HTTPException, Query
from typing import List, Dict
from ..setting import (
    validate_github_token,
    get_commit_number_and_list,
    generate_ai_content,
    logger,
)

trends_router = APIRouter()


def classify_commit(message: str) -> str:
    """根據 commit message 關鍵字分類 commit 類型"""
    message = message.lower()
    if any(
        keyword in message for keyword in ["fix", "bug", "hotfix", "修復", "錯誤"]
    ):
        return "錯誤修復"
    if any(
        keyword in message
        for keyword in ["feat", "feature", "新增", "功能"]
    ):
        return "新功能"
        
    if any(
        keyword in message for keyword in ["perf", "performance", "optimize", "優化", "效能"]
    ):
        return "效能優化"

    if any(
        keyword in message
        for keyword in ["refactor", "style", "format", "重構", "格式"]
    ):
        return "程式碼重構"
    if any(
        keyword in message for keyword in ["docs", "doc", "文件", "註解"]
    ):
        return "文件與註解"
    if any(
        keyword in message for keyword in ["test", "tests", "測試"]
    ):
        return "測試相關"
    return "其他"


@trends_router.get("/repos/{owner}/{repo}/trends")
async def get_repository_trends(
    owner: str, repo: str, access_token: str = Query(None), limit: int = Query(50, ge=1, le=100)
):
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
        return {
            "trends_analysis": analysis_text,
            "statistics": category_counts,
            "commit_count": len(recent_commits),
        }

    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"分析倉庫趨勢時發生意外錯誤: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"分析倉庫趨勢時發生意外錯誤: {str(e)}")

