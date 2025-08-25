from fastapi import HTTPException
from typing import Dict, List, Set
from google.api_core.exceptions import GoogleAPIError, ResourceExhausted
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    RetryError,
)
from .chat import chat_router
from .diff import diff_router
from .overview import overview_router
import httpx
import logging
import google.generativeai as genai
import re
import json


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Global State and Constants ---
conversation_history: Dict[str, List[Dict[str, str]]] = {}
commit_number_cache: Dict[str, Dict[str, int]] = {}
commit_data_cache: Dict[str, List[Dict]] = {}

# Constants from main.py
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
    return list(set(paths))  #


def extract_retry_delay(error_message: str) -> int:
    try:
        if "retry_delay" in error_message:
            match = re.search(r"retry_delay\s*{\s*seconds:\s*(\d+)\s*}", error_message)
            if match:
                return int(match.group(1))
    except Exception as e:
        logger.error(f"解析重試延遲時間時出錯: {str(e)}")
    return 60


def parse_quota_violations(error_message: str) -> List[Dict]:
    violations = []
    try:
        if "violations {" in error_message:
            parts = error_message.split("violations {")
            for part in parts[1:]:
                violation = {}
                metric_match = re.search(r'quota_metric:\s*"([^"]+)"', part)
                if metric_match:
                    violation["metric"] = metric_match.group(1)
                id_match = re.search(r'quota_id:\s*"([^"]+)"', part)
                if id_match:
                    violation["id"] = id_match.group(1)
                dimensions = {}
                dim_parts = part.split("quota_dimensions {")
                for dim_part in dim_parts[1:]:
                    key_match = re.search(r'key:\s*"([^"]+)"', dim_part)
                    value_match = re.search(r'value:\s*"([^"]+)"', dim_part)
                    if key_match and value_match:
                        dimensions[key_match.group(1)] = value_match.group(1)
                violation["dimensions"] = dimensions
                violations.append(violation)
    except Exception as e:
        logger.error(f"解析配額違規信息時出錯: {str(e)}")
    return violations


def format_rate_limit_error(error: Exception) -> tuple[str, int]:
    error_message_str = str(error)
    retry_delay = extract_retry_delay(error_message_str)
    minutes = max(1, (retry_delay + 59) // 60)
    violations = parse_quota_violations(error_message_str)
    if violations:
        logger.info(
            f"配額違規詳情: {json.dumps(violations, indent=2, ensure_ascii=False)}"
        )
    violation_types = []
    for violation in violations:
        if "GenerateRequestsPerMinutePerProjectPerModel" in violation.get("id", ""):
            violation_types.append(f"每分鐘請求數")
        if "GenerateRequestsPerDayPerProjectPerModel" in violation.get("id", ""):
            violation_types.append(f"每日請求數")
        if "GenerateContentInputTokensPerModelPerMinute" in violation.get("id", ""):
            violation_types.append(f"輸入 token 數量")
    if not violation_types:
        return f"Gemini API 使用量已達限制，請 {minutes} 分鐘後再試。", retry_delay
    if len(violation_types) == 1:
        return (
            f"Gemini API {violation_types[0]}已達限制，請 {minutes} 分鐘後再試。",
            retry_delay,
        )
    else:
        violations_str = "、".join(list(set(violation_types)))
        if "每日請求數" in violations_str:
            return (
                f"Gemini API 已達到多項限制（{violations_str}），其中包含每日請求數限制，請明天再試。",
                86400,
            )
        else:
            return (
                f"Gemini API 已達到多項限制（{violations_str}），請 {minutes} 分鐘後再試。",
                retry_delay,
            )


def retry_on_quota_exceeded(max_retries=3, initial_wait=1, max_wait=10):
    def decorator(func):
        @retry(
            stop=stop_after_attempt(max_retries),
            wait=wait_exponential(multiplier=initial_wait, max=max_wait),
            retry=retry_if_exception_type(ResourceExhausted),
            before_sleep=lambda retry_state: logger.info(
                f"Gemini API 配額暫時耗盡。等待 {retry_state.next_action.sleep:.2f} 秒後重試 (嘗試 {retry_state.attempt_number}/{max_retries})..."
            ),
        )
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except ResourceExhausted as e:
                logger.error(f"Gemini API 配額重試失敗後依然耗盡: {str(e)}")
                raise

        return wrapper

    return decorator


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


def get_available_model() -> str:
    try:
        logger.info("正在嘗試列出 Gemini 模型...")
        all_listed_models = genai.list_models()
        usable_model_names: Set[str] = {
            m.name.split("/")[-1]
            for m in all_listed_models
            if "generateContent" in m.supported_generation_methods
        }
        logger.info(f"找到支援 'generateContent' 的模型: {usable_model_names}")
        if not usable_model_names:
            logger.error("未找到任何支援 'generateContent' 的 Gemini 模型。")
            raise HTTPException(
                status_code=500,
                detail="No Gemini models found that support 'generateContent'.",
            )
        preferred_models_ordered = [
            "gemini-1.5-flash",
            "gemini-1.0-pro",
            "gemini-1.5-pro",
        ]
        for preferred_name in preferred_models_ordered:
            if preferred_name in usable_model_names:
                logger.info(f"選擇優先模型: {preferred_name}")
                return preferred_name
        fallback_model_name = sorted(list(usable_model_names))[0]
        logger.info(f"無偏好模型可用，選擇後備模型: {fallback_model_name}")
        return fallback_model_name
    except GoogleAPIError as e:
        logger.error(f"列出 Gemini 模型時發生 GoogleAPIError: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list Gemini models due to API error: {str(e)}",
        )
    except Exception as e:
        logger.error(f"列出 Gemini 模型時發生意外錯誤: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"An unexpected error occurred while listing models: {str(e)}",
        )


@retry_on_quota_exceeded()
async def generate_gemini_content(
    model_instance: genai.GenerativeModel, prompt_text: str
) -> str:
    try:
        model_name_used = model_instance.model_name
        logger.info(
            f"正在使用模型 '{model_name_used}' 生成內容。提示詞長度 (估算 tokens): {len(prompt_text)//4}..."
        )
        response = model_instance.generate_content(prompt_text)
        if not response.text:
            logger.error(
                f"Gemini API ({model_name_used}) 返回了空的回應或無效的回應結構。"
            )
            if response.candidates:
                for candidate in response.candidates:
                    if candidate.finish_reason != 1:
                        logger.error(
                            f"候選內容完成原因: {candidate.finish_reason}. 安全評級: {candidate.safety_ratings}"
                        )
                        detail_message = f"Gemini API response was blocked or incomplete. Finish Reason: {candidate.finish_reason}."
                        if candidate.safety_ratings:
                            detail_message += f" Safety Ratings: {[(sr.category, sr.probability) for sr in candidate.safety_ratings]}"
                        raise HTTPException(status_code=400, detail=detail_message)
            raise HTTPException(
                status_code=500,
                detail="Gemini API returned an empty or invalid response.",
            )
        return response.text
    except ResourceExhausted as e:
        logger.error(
            f"generate_gemini_content 中 ResourceExhausted (模型: {model_instance.model_name}): {str(e)}"
        )
        raise
    except GoogleAPIError as e:
        logger.error(
            f"Gemini API 錯誤 (模型: {model_instance.model_name}, 非配額): {str(e)}"
        )
        if (
            "quota" in str(e).lower()
            or "rate limit" in str(e).lower()
            or "429" in str(e)
        ):
            error_message, retry_delay = format_rate_limit_error(e)
            raise HTTPException(
                status_code=429,
                detail=error_message,
                headers={"Retry-After": str(retry_delay)},
            )
        raise HTTPException(
            status_code=500, detail=f"Gemini API failed (non-quota): {str(e)}"
        )
    except RetryError as e:
        original_error = e.last_attempt.exception()
        logger.error(
            f"Gemini API 所有重試均失敗。原始錯誤 (模型: {model_instance.model_name}): {str(original_error)}"
        )
        if isinstance(original_error, ResourceExhausted):
            error_message, retry_delay = format_rate_limit_error(original_error)
            raise HTTPException(
                status_code=429,
                detail=error_message,
                headers={"Retry-After": str(retry_delay)},
            )
        else:
            raise HTTPException(
                status_code=500,
                detail=f"Gemini API retries failed: {str(original_error)}",
            )
    except Exception as e:
        logger.error(
            f"生成 Gemini 內容時發生意外錯誤 (模型: {model_instance.model_name}): {str(e)}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error during Gemini content generation: {str(e)}",
        )
