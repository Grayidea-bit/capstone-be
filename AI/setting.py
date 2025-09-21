from fastapi import HTTPException
from typing import Dict, List, Any, Tuple
import httpx
import logging
import re
import os
import redis
import json
from pythonjsonlogger import jsonlogger


from dotenv import load_dotenv

load_dotenv()


logger = logging.getLogger(__name__)
logHandler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter(
    "%(asctime)s %(name)s %(levelname)s %(message)s"
)
logHandler.setFormatter(formatter)
if not logger.handlers:
    logger.addHandler(logHandler)
    logger.setLevel(logging.INFO)


try:
    redis_client = redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        db=int(os.getenv("REDIS_DB", 0)),
        decode_responses=True,
    )
    redis_client.ping()
    logger.info("成功連接至 Redis 伺服器。")
except redis.exceptions.ConnectionError as e:
    logger.error(f"無法連接至 Redis 伺服器: {e}，快取功能將無法使用。")
    redis_client = None

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", 3600))

# --- AI 內容生成限制 ---
MAX_FILES_FOR_PREVIOUS_CONTENT = int(os.getenv("MAX_FILES_FOR_PREVIOUS_CONTENT", 7))
MAX_CHARS_PER_PREV_FILE = int(os.getenv("MAX_CHARS_PER_PREV_FILE", 4000))
MAX_TOTAL_CHARS_PREV_FILES = int(os.getenv("MAX_TOTAL_CHARS_PREV_FILES", 25000))
MAX_CHARS_CURRENT_DIFF = int(os.getenv("MAX_CHARS_CURRENT_DIFF", 35000))
MAX_CHARS_README = int(os.getenv("MAX_CHARS_README", 10000))
MAX_CHARS_PR_DIFF = int(os.getenv("MAX_CHARS_PR_DIFF", 80000))


def parse_diff_for_previous_file_paths(diff_text: str) -> List[str]:
    """
    從 diff 文本中解析出在當前 diff 發生變化之前 (即 'a/' 版本) 的檔案路徑。
    這些路徑代表了在 (n-1) commit 中存在且在 nth commit 中被修改或刪除的檔案。
    """
    paths = []
    for match in re.finditer(
        r"^diff --git a/(?P<path_a>[^\s]+) b/(?P<path_b>[^\s]+)",
        diff_text,
        re.MULTILINE,
    ):
        path_a = match.group("path_a")
        if path_a != ".dev/null" and path_a != "/dev/null":
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
                user_info = response.json()
                logger.info(
                    "GitHub token 驗證成功。",
                    extra={"user": user_info.get("login"), "token_prefix": access_token[:5]},
                )
                return True
            else:
                logger.warning(
                    "GitHub token 驗證失敗。",
                    extra={
                        "status_code": response.status_code,
                        "response": response.text,
                        "token_prefix": access_token[:5],
                    },
                )
                return False
        except httpx.RequestError as e:
            logger.error(f"驗證 GitHub token 時發生網路錯誤: {str(e)}")
            return False
        except Exception as e:
            logger.error(f"驗證 GitHub token 時發生未知錯誤: {str(e)}", exc_info=True)
            return False


async def get_commit_number_and_list(
    owner: str, repo: str, access_token: str
) -> Tuple[Dict[str, int], List[Dict]]:
    cache_key_data = f"commit_data:{owner}/{repo}"
    cache_key_map = f"commit_map:{owner}/{repo}"

    if redis_client:
        try:
            cached_data = redis_client.get(cache_key_data)
            cached_map = redis_client.get(cache_key_map)
            if cached_data and cached_map:
                logger.info(f"快取命中: {owner}/{repo}")
                return json.loads(cached_map), json.loads(cached_data)
        except redis.exceptions.RedisError as e:
            logger.error(f"讀取 Redis 快取時發生錯誤: {e}")

    logger.info(f"快取未命中，正在為 {owner}/{repo} 從 API 獲取 commits...")
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
                    f"從 GitHub API 獲取 commits 時發生 HTTP 錯誤: {e}",
                    extra={"url": str(e.request.url)},
                )
                detail = f"無法從 GitHub 獲取 commits: {e.response.status_code} - {e.response.text}"
                if e.response.status_code == 401:
                    detail = "GitHub token 可能無效或已過期。"
                raise HTTPException(status_code=e.response.status_code, detail=detail)

    if not all_commits_fetched:
        logger.info(f"倉庫 {owner}/{repo} 中沒有 commits。")
        return {}, []

    commit_map = {
        commit["sha"]: i
        for i, commit in enumerate(reversed(all_commits_fetched), 1)
    }

    if redis_client:
        try:
            redis_client.set(
                cache_key_data, json.dumps(all_commits_fetched), ex=CACHE_TTL_SECONDS
            )
            redis_client.set(
                cache_key_map, json.dumps(commit_map), ex=CACHE_TTL_SECONDS
            )
            logger.info(f"成功為 {owner}/{repo} 快取了 {len(all_commits_fetched)} 個 commits。")
        except redis.exceptions.RedisError as e:
            logger.error(f"寫入 Redis 快取時發生錯誤: {e}")

    return commit_map, all_commits_fetched


async def generate_ai_content(prompt_text: str) -> str:
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
                "content": "You are an AI assistant for a GitHub repository analysis tool, responding in Traditional Chinese. Be precise and helpful.",
            },
            {"role": "user", "content": prompt_text},
        ],
    }

    logger.info(
        "正在向 Perplexity API 發送請求。",
        extra={"prompt_length": len(prompt_text)},
    )

    async with httpx.AsyncClient(timeout=90.0) as client:
        try:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if not content:
                logger.error("Perplexity API 返回了空的回應。", extra={"response_data": data})
                raise HTTPException(status_code=500, detail="AI 服務返回了空的回應。")
            logger.info(
                "成功從 Perplexity API 獲取回應。",
                extra={"response_length": len(content)},
            )
            return content
        except httpx.HTTPStatusError as e:
            logger.error(
                f"Perplexity API 錯誤: {e.response.status_code} - {e.response.text}"
            )
            raise HTTPException(
                status_code=e.response.status_code, detail=f"AI 服務錯誤: {e.response.text}"
            )
        except httpx.TimeoutException as e:
            logger.error(f"呼叫 Perplexity API 時發生超時錯誤: {str(e)}")
            raise HTTPException(status_code=504, detail="AI 服務請求超時。")
        except Exception as e:
            logger.error(f"呼叫 Perplexity API 時發生意外錯誤: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="與 AI 服務通訊時發生意外錯誤。")