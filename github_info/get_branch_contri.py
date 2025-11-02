from fastapi import APIRouter, HTTPException, Query
import httpx
from collections import defaultdict

contri_router = APIRouter()

# GitHub API 的基礎 URL
GITHUB_API_URL = "https://api.github.com"

@contri_router.get("/repos/{owner}/{repo}/contributions")
async def get_all_branch_contributions(
    owner: str,
    repo: str,
    access_token: str = Query(...)
):
    """
    獲取一個倉庫中所有分支的貢獻者提交次數。
    為了避免重複計算合併到多個分支的同一個 commit，我們會記錄處理過的 commit SHA。
    """
    headers = {
        "Authorization": f"token {access_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    
    contributions = defaultdict(int)
    processed_commits = set() # 用於儲存已經處理過的 commit SHA

    async with httpx.AsyncClient() as client:
        try:
            # 1. 獲取所有分支
            branches_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/branches"
            branches_response = await client.get(branches_url, headers=headers)
            branches_response.raise_for_status()
            branches = branches_response.json()

            # 2. 遍歷每個分支以獲取 commits
            for branch in branches:
                branch_name = branch['name']
                page = 1
                while True:
                    # 分頁獲取 commit
                    commits_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/commits?sha={branch_name}&per_page=100&page={page}"
                    commits_response = await client.get(commits_url, headers=headers)
                    commits_response.raise_for_status()
                    commits_data = commits_response.json()

                    if not commits_data:
                        break # 如果這一頁沒有數據，說明這個分支的 commits 已經獲取完畢

                    for commit in commits_data:
                        commit_sha = commit['sha']
                        # 如果這個 commit 已經處理過，就跳過
                        if commit_sha in processed_commits:
                            continue
                        
                        processed_commits.add(commit_sha)
                        
                        # 作者可能為 null，需要檢查
                        if commit.get('author') and commit['author'].get('login'):
                            author_login = commit['author']['login']
                            # 過濾掉 GitHub 官方的 no-reply 用戶
                            if 'users.noreply.github.com' not in author_login:
                                contributions[author_login] += 1
                    
                    page += 1

        except httpx.HTTPStatusError as e:
            # 更詳細的錯誤日誌
            error_details = e.response.json().get("message", e.response.text)
            raise HTTPException(
                status_code=e.response.status_code,
                detail=f"與 GitHub API 通訊時發生錯誤: {error_details}"
            )
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"分析貢獻時發生內部錯誤: {str(e)}"
            )

    if not contributions:
        return {"detail": "找不到任何貢獻紀錄或無法分析。"}

    # 按照貢獻次數降序排序
    sorted_contributions = dict(sorted(contributions.items(), key=lambda item: item[1], reverse=True))
    
    return sorted_contributions