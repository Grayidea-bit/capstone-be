 **使用 Docker 啟動 (需先下載Docker)**：
    在終端機執行以下指令，即可在背景啟動一個 Redis 容器
    ```
    docker run -d --name my-redis -p 6379:6379 redis
    ```
## API 端點

**基礎 URL**: `http://127.0.0.1:8000`

| 功能 | HTTP 方法 | 路徑 & 查詢參數 | Body | 成功回應 (JSON) | 前端備註 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **GitHub 登入** | `GET` | `/login/?code=<auth_code>` | 無 | `{"access_token": "...", "user": {"login": "...", "avatar_url": "..."}}` | 處理 OAuth 回調 |
| **獲取使用者** | `GET` | `/user_info/?access_token=<token>` | 無 | `{"username": "...", "avatar_url": "..."}` | 用於驗證 token 和顯示使用者資訊 |
| **獲取倉庫列表** | `GET` | `/repo_list/?access_token=<token>` | 無 | `[{"full_name": "owner/repo", ...}]` | `full_name` 欄位可以直接用於下拉選單 |
| **獲取 Commit 列表** | `GET` | `/repo_commit/repos/{owner}/{repo}?access_token=<token>` | 無 | `{"commits": [{"name": "...", "sha": "..."}]}` | 用於 PRs/Commits 頁面和聊天上下文 |
| **獲取 PR 列表** | `GET` | `/pr/repos/{owner}/{repo}/pulls?access_token=<token>` | 無 | `[{"number": 123, "title": "...", "sha": "..."}]` | 用於 PRs/Commits 頁面 |
| **專案概覽** | `GET` | `/overview/repos/{owner}/{repo}?access_token=<token>` | 無 | `{"overview": "...", "file_structure": "..."}` | `overview` 是 Markdown，需前端解析 |
| **倉庫趨勢分析** | `GET` | `/trends/repos/{owner}/{repo}/trends?access_token=<token>` | 無 | `{"trends_analysis": "...", "statistics": {...}, "commit_count": 50, "activity_analysis": {...}}` | 包含兩份 AI 分析和兩份圖表數據 |
| **分析 Commit** | `POST` | `/diff/repos/{owner}/{repo}/commits/{sha}?access_token=<token>` | 無 | `{"analysis": "...", ...}` | `analysis` 是 Markdown，需前端解析 |
| **分析 PR** | `GET` | `/pr/repos/{owner}/{repo}/pulls/{pull_number}?access_token=<token>` | 無 | `{"pull_request_analysis": "..."}` | `pull_request_analysis` 是 Markdown |
| **發佈 PR 評論** | `POST` | `/pr/repos/{owner}/{repo}/pulls/{pull_number}/comments?access_token=<token>` | `{"comment": "markdown_string"}` | `{"message": "...", "comment_url": "..."}` | Body 中傳入之前獲取的分析結果 |
| **智能問答** | `POST` | `/chat/repos/{owner}/{repo}?access_token=<token>&question=...&mode=...&target_sha=...` | 無 | `{"answer": "...", "history": [...]}` | `mode` 可為 `commit`, `repository`, `what-if` |

## 所需圖表

#### 1. Commit 類型分佈圖 (Doughnut Chart)

- **API 端點**: `/trends/...`
- **數據來源**: `response.statistics`
- **範例數據結構**:
  ```json
  {
    "statistics": {
      "新功能": 25,
      "錯誤修復": 15,
      "程式碼重構": 8,
      "文件與註解": 2
    }
  }
  ```

#### 2. 模組活躍度圖 (Horizontal Bar Chart)
- **API 端點**: `/trends/...`

 **數據來源**: `response.activity_analysis.top_modules`

- **範例數據結構:**
```json
{
  "activity_analysis": {
    "top_modules": [
      ["AI", 55],
      ["github_info", 32],
      ["github_login", 10]
    ]
  }
}
```



