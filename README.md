## 1. Repo Branch API   

GET /branch/{owner}/{repo}

取得指定 GitHub repository 的所有分支名稱。

### Path Parameters

| 參數   | 說明                     |
|--------|------------------------|
| owner  | GitHub repository 擁有者 |
| repo   | GitHub repository 名稱   |

### Query Parameters

| 參數         | 說明                           
|--------------|-------------------------------
| access_token | GitHub Personal Access Token    


### 回傳範例
```json
{
  "branches": [
    "main",
    "develop",
    "feature-branch"
  ]
```

## 2. Repo Commit API

GET /repo_commit/repos/{owner}/{repo}

取得指定 GitHub repository 某個分支的 commit 訊息。

### Path Parameters

| 參數   | 說明                     |
|--------|------------------------|
| owner  | GitHub repository 擁有者 |
| repo   | GitHub repository 名稱   |

### Query Parameters

| 參數         | 說明                                           |
|--------------|----------------------------------------------|
| access_token | GitHub Personal Access Token                  |
| branch_name  | 指定要查詢的分支名稱，如果不填則預設為預設分支 |

### 回傳範例
```json
{
  "commits": [
    {
      "name": "Initial commit",
      "sha": "a1b2c3d4"
    },
    {
      "name": "Add README.md",
      "sha": "e5f6g7h8"
    }
  ]
}

}
```
