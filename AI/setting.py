from fastapi import HTTPException
from typing import Dict, List
import httpx
import logging
import re
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 全域狀態和常數 ---
conversation_history: Dict[str, List[Dict[str, str]]] = {}
commit_number_cache: Dict[str, Dict[str, int]] = {}
commit_data_cache: Dict[str, List[Dict]] = {}

# 從主程式引用的常數
MAX_FILES_FOR_PREVIOUS_CONTENT = 7
MAX_CHARS_PER_PREV_FILE = 4000
MAX_TOTAL_CHARS_PREV_FILES = 25000
MAX_CHARS_CURRENT_DIFF = 35000
MAX_CHARS_README = 10000


def parse_diff_for_previous_file_paths(diff_text: str) -> List[str]:
    """
    從 diff 文本中解析出在當前 diff 發生變化之前 (即 'a/' 版本) 的檔案路徑。
    這些路徑代表了在 (n-1) commit 中存在且在 nth commit 中被修改或刪除的檔案。
    """
    paths = []
    # diff --git a/path/to/file.py b/path/to/file.py
    # diff --git a/.dev/null b/new_file.py  (新檔案，a_path 是 .dev/null，忽略)
    # diff --git a/deleted_file.py b/.dev/null (刪除檔案，a_path 是 deleted_file.py)
    for match in re.finditer(
        r"^diff --git a/(?P<path_a>[^\s]+) b/(?P<path_b>[^\s]+)",
        diff_text,
        re.MULTILINE,
    ):
        path_a = match.group("path_a")
        # 我們只關心在 n-1 commit 中實際存在的檔案路徑
        if path_a != ".dev/null" and path_a != "/dev/null":  # 處理兩種 null 路徑表示
            paths.append(path_a)
    return list(set(paths))


async def validate_github_token(access_token: str) -> bool:
    if not access_token:
        logger.warning("嘗試驗證空的 GitHub token。")
        return False
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if response.status_code == 200:
                logger.info(f"GitHub token (前5碼: {access_token[:5]}...) 驗證成功。")
                return True
            elif response.status_code == 401:
                logger.warning(
                    f"GitHub token (前5碼: {access_token[:5]}...) 驗證失敗 (401 Unauthorized): {response.text}"
                )
                return False
            else:
                logger.error(
                    f"GitHub token 驗證時收到意外的狀態碼 {response.status_code}: {response.text}"
                )
                return False
        except httpx.RequestError as e:
            logger.error(
                f"驗證 GitHub token (前5碼: {access_token[:5]}...) 時發生網路錯誤: {str(e)}"
            )
            return False
        except Exception as e:
            logger.error(
                f"驗證 GitHub token (前5碼: {access_token[:5]}...) 時發生未知錯誤: {str(e)}",
                exc_info=True,
            )
            return False


async def get_commit_number_and_list(
    owner: str, repo: str, access_token: str
) -> tuple[Dict[str, int], List[Dict]]:
    cache_key = f"{owner}/{repo}"
    if cache_key not in commit_number_cache or cache_key not in commit_data_cache:
        logger.info(f"快取未命中或不完整，正在為 {cache_key} 獲取 commits...")
        commit_number_cache[cache_key] = {}
        commit_data_cache[cache_key] = []
        all_commits_fetched = []
        page = 1
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    response = await client.get(
                        f"https://api.github.com/repos/{owner}/{repo}/commits",
                        headers={"Authorization": f"Bearer {access_token}"},
                        params={"per_page": 100, "page": page},
                    )
                    response.raise_for_status()
                    page_commits = response.json()
                    if not page_commits:
                        break
                    all_commits_fetched.extend(page_commits)
                    page += 1
                    if len(page_commits) < 100:
                        break
                except httpx.HTTPStatusError as e:
                    logger.error(
                        f"從 GitHub API 獲取 commits 時發生 HTTP 錯誤: {str(e)}, URL: {e.request.url}"
                    )
                    detail = f"無法從 GitHub 獲取 commits: {e.response.status_code} - {e.response.text}"
                    if e.response.status_code == 401:
                        detail = "GitHub token 可能無效或已過期 (獲取 commits 時)。"
                    elif e.response.status_code == 404:
                        detail = f"倉庫 {owner}/{repo} 未找到或沒有 commits。"
                    raise HTTPException(
                        status_code=e.response.status_code, detail=detail
                    )
        if not all_commits_fetched:
            logger.info(f"倉庫 {owner}/{repo} 中沒有找到任何 commits。")
            commit_data_cache[cache_key] = []
            commit_number_cache[cache_key] = {}
            return {}, []
        commit_data_cache[cache_key] = all_commits_fetched
        for i, commit in enumerate(reversed(all_commits_fetched), 1):
            commit_number_cache[cache_key][commit["sha"]] = i
        logger.info(
            f"為 {cache_key} 成功獲取並快取了 {len(all_commits_fetched)} 個 commits。"
        )
    else:
        logger.info(
            f"快取命中: {cache_key} (共 {len(commit_data_cache[cache_key])} commits)"
        )
    return commit_number_cache[cache_key], commit_data_cache[cache_key]


async def generate_ai_content(prompt_text: str) -> str:
    """
    使用 Perplexity Sonar API 生成內容。
    """
    api_key = os.getenv("PERPLEXITY_API_KEY")
    if not api_key:
        logger.error("PERPLEXITY_API_KEY 環境變數未設定。")
        raise HTTPException(status_code=500, detail="AI 服務未配置。")

    url = "https://api.perplexity.ai/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "model": "sonar-pro",
        "messages": [
            {
                "role": "system",
                "content": "You are an AI assistant for a GitHub repository analysis tool. Be precise and helpful.",
            },
            {"role": "user", "content": prompt_text},
        ],
    }

    logger.info(f"正在向 Perplexity API (sonar-deep-research) 發送請求。提示詞長度約: {len(prompt_text)} 字元。")

    async with httpx.AsyncClient(timeout=90.0) as client:
        try:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if not content:
                logger.error(f"Perplexity API 返回了空的回應或無效的結構: {data}")
                raise HTTPException(status_code=500, detail="AI 服務返回了空的回應。")
            
            log_content = content[:150] + "..." if len(content) > 150 else content
            logger.info(f"成功從 Perplexity API 獲取回應: '{log_content}'")
            return content
        except httpx.HTTPStatusError as e:
            logger.error(
                f"Perplexity API 錯誤: {e.response.status_code} - {e.response.text}"
            )
            raise HTTPException(
                status_code=e.response.status_code,
                detail=f"AI 服務錯誤: {e.response.text}",
            )
        except Exception as e:
            logger.error(f"呼叫 Perplexity API 時發生意外錯誤: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=500, detail="與 AI 服務通訊時發生意外錯誤。"
            )
