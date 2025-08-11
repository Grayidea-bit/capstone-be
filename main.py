#================================#
# 導入模組和配置日誌
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import os
import httpx
import urllib.parse
import google.generativeai as genai
from google.api_core.exceptions import GoogleAPIError, ResourceExhausted
from typing import Dict, List, Set 
import logging
import time
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, RetryError
import re
import json
import base64 # For decoding file content if not using raw

# 配置日誌
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# 允許 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 載入環境變數
load_dotenv()
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
REDIRECT_URI = "http://localhost:3000" 

# 驗證環境變數
if not all([GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, GEMINI_API_KEY]):
    raise ValueError("缺少必要的環境變數 (GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, GEMINI_API_KEY)，請檢查 .env 文件")

genai.configure(api_key=GEMINI_API_KEY)
logger.info(f"GEMINI_API_KEY loaded during startup: {GEMINI_API_KEY[:5]}...")

conversation_history: Dict[str, List[Dict[str, str]]] = {} 
commit_number_cache: Dict[str, Dict[str, int]] = {}      
commit_data_cache: Dict[str, List[Dict]] = {}           

# 常數定義
MAX_FILES_FOR_PREVIOUS_CONTENT = 7 # 最多獲取多少個 n-1 commit 的檔案內容
MAX_CHARS_PER_PREV_FILE = 4000     # 每個 n-1 commit 檔案內容的最大字符數
MAX_TOTAL_CHARS_PREV_FILES = 25000 # n-1 commit 所有檔案內容總和的最大字符數
MAX_CHARS_CURRENT_DIFF = 35000     # n commit diff 的最大字符數
MAX_CHARS_README = 10000           # README 的最大字符數

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
                    violation['metric'] = metric_match.group(1)
                id_match = re.search(r'quota_id:\s*"([^"]+)"', part)
                if id_match:
                    violation['id'] = id_match.group(1)
                dimensions = {}
                dim_parts = part.split("quota_dimensions {")
                for dim_part in dim_parts[1:]:
                    key_match = re.search(r'key:\s*"([^"]+)"', dim_part)
                    value_match = re.search(r'value:\s*"([^"]+)"', dim_part)
                    if key_match and value_match:
                        dimensions[key_match.group(1)] = value_match.group(1)
                violation['dimensions'] = dimensions
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
        logger.info(f"配額違規詳情: {json.dumps(violations, indent=2, ensure_ascii=False)}")
    violation_types = []
    for violation in violations:
        if "GenerateRequestsPerMinutePerProjectPerModel" in violation.get('id', ''):
            violation_types.append(f"每分鐘請求數")
        if "GenerateRequestsPerDayPerProjectPerModel" in violation.get('id', ''):
            violation_types.append(f"每日請求數")
        if "GenerateContentInputTokensPerModelPerMinute" in violation.get('id', ''):
            violation_types.append(f"輸入 token 數量")
    if not violation_types: 
        return f"Gemini API 使用量已達限制，請 {minutes} 分鐘後再試。", retry_delay
    if len(violation_types) == 1:
        return f"Gemini API {violation_types[0]}已達限制，請 {minutes} 分鐘後再試。", retry_delay
    else:
        violations_str = "、".join(list(set(violation_types))) 
        if "每日請求數" in violations_str: 
            return f"Gemini API 已達到多項限制（{violations_str}），其中包含每日請求數限制，請明天再試。", 86400 
        else:
            return f"Gemini API 已達到多項限制（{violations_str}），請 {minutes} 分鐘後再試。", retry_delay

def retry_on_quota_exceeded(max_retries=3, initial_wait=1, max_wait=10):
    def decorator(func):
        @retry(
            stop=stop_after_attempt(max_retries),
            wait=wait_exponential(multiplier=initial_wait, max=max_wait), 
            retry=retry_if_exception_type(ResourceExhausted),
            before_sleep=lambda retry_state: logger.info(f"Gemini API 配額暫時耗盡。等待 {retry_state.next_action.sleep:.2f} 秒後重試 (嘗試 {retry_state.attempt_number}/{max_retries})...")
        )
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except ResourceExhausted as e:
                logger.error(f"Gemini API 配額重試失敗後依然耗盡: {str(e)}")
                raise 
        return wrapper
    return decorator

def get_available_model() -> str:
    try:
        logger.info("正在嘗試列出 Gemini 模型...")
        all_listed_models = genai.list_models()
        usable_model_names: Set[str] = {
            m.name.split('/')[-1] for m in all_listed_models if 'generateContent' in m.supported_generation_methods
        }
        logger.info(f"找到支援 'generateContent' 的模型: {usable_model_names}")
        if not usable_model_names:
            logger.error("未找到任何支援 'generateContent' 的 Gemini 模型。")
            raise HTTPException(status_code=500, detail="No Gemini models found that support 'generateContent'.")
        preferred_models_ordered = ['gemini-1.5-flash', 'gemini-1.0-pro', 'gemini-1.5-pro']
        for preferred_name in preferred_models_ordered:
            if preferred_name in usable_model_names:
                logger.info(f"選擇優先模型: {preferred_name}")
                return preferred_name
        fallback_model_name = sorted(list(usable_model_names))[0] 
        logger.info(f"無偏好模型可用，選擇後備模型: {fallback_model_name}")
        return fallback_model_name
    except GoogleAPIError as e:
        logger.error(f"列出 Gemini 模型時發生 GoogleAPIError: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to list Gemini models due to API error: {str(e)}")
    except Exception as e:
        logger.error(f"列出 Gemini 模型時發生意外錯誤: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred while listing models: {str(e)}")

@retry_on_quota_exceeded()
async def generate_gemini_content(model_instance: genai.GenerativeModel, prompt_text: str) -> str:
    try:
        model_name_used = model_instance.model_name
        logger.info(f"正在使用模型 '{model_name_used}' 生成內容。提示詞長度 (估算 tokens): {len(prompt_text)//4}...")
        response = model_instance.generate_content(prompt_text)
        if not response.text: 
            logger.error(f"Gemini API ({model_name_used}) 返回了空的回應或無效的回應結構。")
            if response.candidates:
                for candidate in response.candidates:
                    if candidate.finish_reason != 1: 
                        logger.error(f"候選內容完成原因: {candidate.finish_reason}. 安全評級: {candidate.safety_ratings}")
                        detail_message = f"Gemini API response was blocked or incomplete. Finish Reason: {candidate.finish_reason}."
                        if candidate.safety_ratings:
                            detail_message += f" Safety Ratings: {[(sr.category, sr.probability) for sr in candidate.safety_ratings]}"
                        raise HTTPException(status_code=400, detail=detail_message) 
            raise HTTPException(status_code=500, detail="Gemini API returned an empty or invalid response.")
        return response.text
    except ResourceExhausted as e: 
        logger.error(f"generate_gemini_content 中 ResourceExhausted (模型: {model_instance.model_name}): {str(e)}")
        raise 
    except GoogleAPIError as e:
        logger.error(f"Gemini API 錯誤 (模型: {model_instance.model_name}, 非配額): {str(e)}")
        if "quota" in str(e).lower() or "rate limit" in str(e).lower() or "429" in str(e):
            error_message, retry_delay = format_rate_limit_error(e)
            raise HTTPException(status_code=429, detail=error_message, headers={"Retry-After": str(retry_delay)})
        raise HTTPException(status_code=500, detail=f"Gemini API failed (non-quota): {str(e)}")
    except RetryError as e: 
        original_error = e.last_attempt.exception()
        logger.error(f"Gemini API 所有重試均失敗。原始錯誤 (模型: {model_instance.model_name}): {str(original_error)}")
        if isinstance(original_error, ResourceExhausted):
            error_message, retry_delay = format_rate_limit_error(original_error)
            raise HTTPException(status_code=429, detail=error_message, headers={"Retry-After": str(retry_delay)})
        else: 
            raise HTTPException(status_code=500, detail=f"Gemini API retries failed: {str(original_error)}")
    except Exception as e:
        logger.error(f"生成 Gemini 內容時發生意外錯誤 (模型: {model_instance.model_name}): {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Unexpected error during Gemini content generation: {str(e)}")

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error(f"全域異常處理器捕獲到未處理的異常: {str(exc)}", exc_info=True)
    status_code = 500
    detail = f"伺服器內部錯誤: {str(exc)}"
    headers = {"Access-Control-Allow-Origin": "http://localhost:3000"} 
    if isinstance(exc, HTTPException):
        status_code = exc.status_code
        detail = exc.detail
        if exc.headers: 
            headers.update(exc.headers)
    elif isinstance(exc, ResourceExhausted): 
        status_code = 429 
        error_message, retry_delay = format_rate_limit_error(exc)
        detail = error_message
        headers["Retry-After"] = str(retry_delay) 
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail},
        headers=headers,
    )

@app.get("/")
async def root():
    return {"message": "歡迎使用 GitHub LLM 分析 API"}

async def get_commit_number_and_list(owner: str, repo: str, access_token: str) -> tuple[Dict[str, int], List[Dict]]:
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
                    logger.error(f"從 GitHub API 獲取 commits 時發生 HTTP 錯誤: {str(e)}, URL: {e.request.url}")
                    detail = f"無法從 GitHub 獲取 commits: {e.response.status_code} - {e.response.text}"
                    if e.response.status_code == 401:
                         detail = "GitHub token 可能無效或已過期 (獲取 commits 時)。"
                    elif e.response.status_code == 404:
                        detail = f"倉庫 {owner}/{repo} 未找到或沒有 commits。"
                    raise HTTPException(status_code=e.response.status_code, detail=detail)
        if not all_commits_fetched:
            logger.info(f"倉庫 {owner}/{repo} 中沒有找到任何 commits。")
            commit_data_cache[cache_key] = []
            commit_number_cache[cache_key] = {}
            return {}, []
        commit_data_cache[cache_key] = all_commits_fetched 
        for i, commit in enumerate(reversed(all_commits_fetched), 1):
            commit_number_cache[cache_key][commit["sha"]] = i
        logger.info(f"為 {cache_key} 成功獲取並快取了 {len(all_commits_fetched)} 個 commits。")
    else:
        logger.info(f"快取命中: {cache_key} (共 {len(commit_data_cache[cache_key])} commits)")
    return commit_number_cache[cache_key], commit_data_cache[cache_key]

@app.get("/auth/github/login")
async def github_login():
    params = {
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": "repo user", 
    }
    github_auth_url = f"https://github.com/login/oauth/authorize?{urllib.parse.urlencode(params)}"
    return RedirectResponse(github_auth_url)

@app.get("/auth/github/callback")
async def github_callback(code: str = Query(...)):
    logger.info(f"收到 GitHub OAuth code: {code[:10]}...") 
    async with httpx.AsyncClient() as client:
        try:
            token_response = await client.post(
                "https://github.com/login/oauth/access_token",
                json={
                    "client_id": GITHUB_CLIENT_ID,
                    "client_secret": GITHUB_CLIENT_SECRET,
                    "code": code,
                },
                headers={"Accept": "application/json"}, 
            )
            token_response.raise_for_status()
            token_data = token_response.json()
            logger.info(f"GitHub token 響應 (部分): { {k: (v[:5]+'...' if isinstance(v, str) and len(v)>5 else v) for k,v in token_data.items()} }") 
            access_token = token_data.get("access_token")
            if not access_token:
                error = token_data.get("error", "未知錯誤")
                error_description = token_data.get("error_description", "未提供描述")
                logger.error(f"從 GitHub 獲取 access_token 失敗: {error} - {error_description}")
                raise HTTPException(status_code=400, detail=f"無法獲取 GitHub access token: {error_description}")
            user_response = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            user_response.raise_for_status()
            user_data = user_response.json()
            logger.info(f"成功獲取 GitHub 用戶數據: login='{user_data.get('login')}'")
            return {
                "access_token": access_token,
                "user": {
                    "login": user_data.get("login"),
                    "avatar_url": user_data.get("avatar_url"),
                    "html_url": user_data.get("html_url"),
                },
            }
        except httpx.HTTPStatusError as e:
            logger.error(f"GitHub OAuth 回呼期間發生 HTTP 錯誤: {str(e)}, URL: {e.request.url}, Response: {e.response.text}")
            raise HTTPException(status_code=e.response.status_code, detail=f"GitHub OAuth 回呼失敗: {e.response.text}")
        except Exception as e:
            logger.error(f"GitHub OAuth 回呼期間發生意外錯誤: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"GitHub OAuth 回呼期間發生意外錯誤: {str(e)}")

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
                logger.warning(f"GitHub token (前5碼: {access_token[:5]}...) 驗證失敗 (401 Unauthorized): {response.text}")
                return False
            else: 
                logger.error(f"GitHub token 驗證時收到意外的狀態碼 {response.status_code}: {response.text}")
                return False 
        except httpx.RequestError as e: 
            logger.error(f"驗證 GitHub token (前5碼: {access_token[:5]}...) 時發生網路錯誤: {str(e)}")
            return False 
        except Exception as e: 
            logger.error(f"驗證 GitHub token (前5碼: {access_token[:5]}...) 時發生未知錯誤: {str(e)}", exc_info=True)
            return False

@app.get("/repos")
async def get_repos(access_token: str = Query(None)):
    if not access_token:
        logger.error("獲取倉庫列表請求中未提供 Access token。")
        raise HTTPException(status_code=401, detail="Access token is missing.")
    logger.info(f"收到獲取倉庫列表請求，access_token (前5碼): {access_token[:5]}...")
    if not await validate_github_token(access_token):
        logger.error("無效或過期的 GitHub token (get_repos)。")
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired GitHub token. Please login again.",
            headers={"WWW-Authenticate": "Bearer realm='GitHub OAuth'"},
        )
    async with httpx.AsyncClient() as client:
        try:
            repos_response = await client.get(
                "https://api.github.com/user/repos",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"type": "owner", "sort": "updated", "per_page": 100} 
            )
            repos_response.raise_for_status()
            repos_response.raise_for_status()
            repos_data = repos_response.json()
            logger.info(f"成功獲取 {len(repos_data)} 個倉庫。")
            return repos_data
        except httpx.HTTPStatusError as e:
            logger.error(f"獲取倉庫列表時發生 HTTP 錯誤: {str(e)}, URL: {e.request.url}, Response: {e.response.text}")
            detail = f"無法獲取倉庫列表: {e.response.status_code} - {e.response.text}"
            if e.response.status_code == 401: 
                detail = "GitHub token 可能已在此期間失效。請重新登入。"
            raise HTTPException(
                status_code=e.response.status_code,
                detail=detail,
                headers={"WWW-Authenticate": "Bearer realm='GitHub Repos'"} if e.response.status_code == 401 else None
            )
        except Exception as e:
            logger.error(f"獲取倉庫列表時發生意外錯誤: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"獲取倉庫列表時發生意外錯誤: {str(e)}")

@app.get("/repos/{owner}/{repo}/commits")
async def get_commits_endpoint(owner: str, repo: str, access_token: str = Query(None)):
    if not access_token:
        logger.error(f"獲取 {owner}/{repo} commits 請求中未提供 Access token。")
        raise HTTPException(status_code=401, detail="Access token is missing.")
    logger.info(f"收到獲取 commits 請求: owner={owner}, repo={repo}, token (前5碼)={access_token[:5]}...")
    if not await validate_github_token(access_token):
        logger.error("無效或過期的 GitHub token (get_commits_endpoint)。")
        raise HTTPException(status_code=401, detail="Invalid or expired GitHub token. Please login again.", headers={"WWW-Authenticate": "Bearer realm='GitHub OAuth'"})
    try:
        _, commits_data = await get_commit_number_and_list(owner, repo, access_token)
        if not commits_data:
             logger.info(f"倉庫 {owner}/{repo} 中未找到 commits (可能為空倉庫)。")
             return [] 
        logger.info(f"成功從快取或 API 獲取 {owner}/{repo} 的 {len(commits_data)} 個 commits。")
        return commits_data 
    except HTTPException as e: 
        raise e 
    except Exception as e:
        logger.error(f"獲取 {owner}/{repo} commits 時發生意外錯誤: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"獲取 {owner}/{repo} commits 時發生意外錯誤: {str(e)}")

@app.get("/repos/{owner}/{repo}/overview")
async def get_repo_overview(owner: str, repo: str, access_token: str = Query(None)):
    if not access_token:
        raise HTTPException(status_code=401, detail="Access token is missing.")
    logger.info(f"收到倉庫概覽請求: owner={owner}, repo={repo}, token (前5碼)={access_token[:5]}...")
    if not await validate_github_token(access_token):
        raise HTTPException(status_code=401, detail="Invalid or expired GitHub token.")
    async with httpx.AsyncClient() as client:
        try:
            commit_map, commits_data = await get_commit_number_and_list(owner, repo, access_token)
            if not commits_data:
                logger.info(f"倉庫 {owner}/{repo} 無 commits，無法生成概覽。")
                raise HTTPException(status_code=404, detail="倉庫中沒有 commits，無法生成概覽。")
            first_commit_obj = commits_data[-1]
            first_commit_sha = first_commit_obj["sha"]
            first_commit_number = commit_map.get(first_commit_sha)
            if first_commit_number is None:
                logger.error(f"無法為最早的 commit SHA {first_commit_sha} 找到序號 (overview)。")
                raise HTTPException(status_code=500, detail="無法確定第一次 commit 的序號以生成概覽。")
            diff_response = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/commits/{first_commit_sha}",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github.v3.diff"},
            )
            diff_response.raise_for_status()
            diff_data = diff_response.text
            logger.info(f"第一次 commit (序號: {first_commit_number}, SHA: {first_commit_sha}) 的 diff 已獲取。長度: {len(diff_data)} 字元。")
            if len(diff_data) > 70000: 
                logger.warning(f"第一次 commit diff 過大 ({len(diff_data)} 字元)，將截斷至 70000 字元。")
                diff_data = diff_data[:70000] + "\n... [diff 內容因過長已被截斷]"
            readme_content = ""
            try:
                readme_response = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/readme",
                    headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github.raw"}, 
                )
                if readme_response.status_code == 200:
                    readme_content = readme_response.text
                    logger.info(f"成功獲取 {owner}/{repo} 的 README。長度: {len(readme_content)} 字元。")
                    if len(readme_content) > 15000: 
                         logger.warning(f"README 內容過大 ({len(readme_content)} 字元)，將截斷至 15000 字元。")
                         readme_content = readme_content[:15000] + "\n... [README 內容因過長已被截斷]"
                elif readme_response.status_code == 404:
                    logger.info(f"倉庫 {owner}/{repo} 無 README 文件。")
                else:
                    readme_response.raise_for_status() 
            except httpx.HTTPStatusError as e: 
                if e.response.status_code != 404:
                    logger.warning(f"獲取 README 時發生 HTTP 錯誤 (非 404): {str(e)}")
            selected_model_name = get_available_model()
            logger.info(f"為倉庫概覽選擇的模型: {selected_model_name}")
            model_instance = genai.GenerativeModel(selected_model_name)
            prompt = f"""
你是一位資深的程式碼分析專家。請根據以下 GitHub 倉庫的「第一次 commit 的 diff」(序號: {first_commit_number}, SHA: {first_commit_sha}) 和 README（如果有的話），提供一個簡潔（約 100-200 字）、對非技術人員友好的程式碼功能大綱。
說明這個倉庫的核心功能和主要目的。請明確提及這是基於「第一次 commit」的分析。

**倉庫上下文**:
- 第一次 commit diff (序號: {first_commit_number}, SHA: {first_commit_sha}):
```diff
{diff_data}
```
- README 內容 (若可用):
```
{readme_content if readme_content else "未提供 README。"}
```

**你的任務**:
生成程式碼功能大綱。
"""
            logger.info(f"送往 Gemini 的概覽提示詞 (模型: {selected_model_name}, 提示詞長度約: {len(prompt)} 字元): {prompt[:300]}...")
            overview_text = await generate_gemini_content(model_instance, prompt)
            logger.info(f"Gemini 概覽結果 (模型: {selected_model_name}): {overview_text[:150]}...")
            return {"overview": overview_text}
        except httpx.HTTPStatusError as e:
            logger.error(f"獲取倉庫概覽時發生 GitHub API 錯誤: {str(e)}, URL: {e.request.url}, Response: {e.response.text}")
            detail = f"因 GitHub API 錯誤，無法生成倉庫概覽: {e.response.status_code} - {e.response.text}"
            if e.response.status_code == 401:
                detail = "GitHub token 可能無效或已過期 (生成概覽時)。"
            elif e.response.status_code == 404: 
                 detail = f"為 {owner}/{repo} 生成概覽所需的數據未找到 (例如，commit 未找到)。"
            raise HTTPException(status_code=e.response.status_code, detail=detail)
        except HTTPException as e: 
            logger.error(f"獲取倉庫概覽時發生 HTTPException: {e.detail}")
            raise e 
        except Exception as e:
            logger.error(f"獲取倉庫概覽時發生意外錯誤: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"生成倉庫概覽時發生意外錯誤: {str(e)}")

def parse_diff_for_previous_file_paths(diff_text: str) -> List[str]:
    """
    從 diff 文本中解析出在當前 diff 發生變化之前 (即 'a/' 版本) 的檔案路徑。
    這些路徑代表了在 (n-1) commit 中存在且在 nth commit 中被修改或刪除的檔案。
    """
    paths = []
    # diff --git a/path/to/file.py b/path/to/file.py
    # diff --git a/.dev/null b/new_file.py  (新檔案，a_path 是 .dev/null，忽略)
    # diff --git a/deleted_file.py b/.dev/null (刪除檔案，a_path 是 deleted_file.py)
    for match in re.finditer(r"^diff --git a/(?P<path_a>[^\s]+) b/(?P<path_b>[^\s]+)", diff_text, re.MULTILINE):
        path_a = match.group("path_a")
        # 我們只關心在 n-1 commit 中實際存在的檔案路徑
        if path_a != ".dev/null" and path_a != "/dev/null": # 處理兩種 null 路徑表示
            paths.append(path_a)
    return list(set(paths)) # 去重

@app.post("/repos/{owner}/{repo}/chat")
async def chat_with_repo(
    owner: str,
    repo: str,
    access_token: str = Query(None),
    question: str = Query(None),
    target_sha: str = Query(None) 
):
    if not access_token or not question:
        missing = [p for p, v in [("access_token", access_token), ("question", question)] if not v]
        raise HTTPException(status_code=400, detail=f"缺少必要的查詢參數: {', '.join(missing)}")

    log_question = question[:50] + "..." if len(question) > 50 else question
    logger.info(f"收到對話請求: {owner}/{repo}, token(前5):{access_token[:5]}..., q:'{log_question}', target_sha:{target_sha}")
    
    if not await validate_github_token(access_token):
        raise HTTPException(status_code=401, detail="Invalid or expired GitHub token.")

    async with httpx.AsyncClient() as client:
        try:
            commit_map, commits_data = await get_commit_number_and_list(owner, repo, access_token)
            if not commits_data:
                logger.info(f"倉庫 {owner}/{repo} 無 commits，無法進行對話。")
                return {"answer": "抱歉，這個倉庫目前沒有任何提交記錄，我無法根據程式碼內容回答您的問題。", "history": []}

            current_commit_sha_for_context = None
            current_commit_number_for_context = None
            current_commit_diff_text = ""
            
            previous_commit_sha_for_context = None
            previous_commit_number_for_context = None
            previous_commit_files_content_text = "" # 用於存儲 n-1 commit 的檔案內容
            
            commit_context_description = ""

            if target_sha: 
                logger.info(f"對話將使用特定 commit SHA: {target_sha} 及其前一個 commit 的相關檔案內容作為上下文。")
                target_commit_obj = next((c for c in commits_data if c["sha"] == target_sha), None)
                
                current_commit_sha_for_context = target_sha
                current_commit_number_for_context = commit_map.get(target_sha)
                if current_commit_number_for_context is None:
                     logger.warning(f"無法為目標 SHA {target_sha} 計算序號 (chat context)。")
                
                # 1. 獲取第 n 次 commit (target_sha) 的 diff
                diff_response = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/commits/{target_sha}",
                    headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github.v3.diff"},
                )
                diff_response.raise_for_status() 
                current_commit_diff_text = diff_response.text
                logger.info(f"已獲取目標 commit (序號: {current_commit_number_for_context}, SHA: {target_sha}) 的 diff。長度: {len(current_commit_diff_text)}")

                # 2. 找到前一個 commit (n-1)
                if target_commit_obj: 
                    target_index = commits_data.index(target_commit_obj)
                    if target_index + 1 < len(commits_data): 
                        prev_commit_obj = commits_data[target_index + 1]
                        previous_commit_sha_for_context = prev_commit_obj["sha"]
                        previous_commit_number_for_context = commit_map.get(previous_commit_sha_for_context)
                        logger.info(f"找到前一個 commit (序號: {previous_commit_number_for_context}, SHA: {previous_commit_sha_for_context})。")
                
                # 3. 如果找到了 n-1 commit，獲取其相關檔案內容
                if previous_commit_sha_for_context:
                    affected_files_in_n_minus_1 = parse_diff_for_previous_file_paths(current_commit_diff_text)
                    logger.info(f"在 commit {target_sha} 中被修改/刪除的檔案 (來自 n-1 的路徑): {affected_files_in_n_minus_1[:MAX_FILES_FOR_PREVIOUS_CONTENT]}")
                    
                    temp_files_content = []
                    fetched_files_count = 0
                    total_chars_fetched = 0

                    for file_path in affected_files_in_n_minus_1:
                        if fetched_files_count >= MAX_FILES_FOR_PREVIOUS_CONTENT:
                            logger.info(f"已達到獲取前一個 commit 檔案內容的數量上限 ({MAX_FILES_FOR_PREVIOUS_CONTENT})。")
                            break
                        if total_chars_fetched >= MAX_TOTAL_CHARS_PREV_FILES:
                            logger.info(f"已達到獲取前一個 commit 檔案內容的總字元數上限 ({MAX_TOTAL_CHARS_PREV_FILES})。")
                            break
                        
                        try:
                            logger.debug(f"正在獲取檔案 {file_path} 在 commit {previous_commit_sha_for_context} 的內容...")
                            file_content_response = await client.get(
                                f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}?ref={previous_commit_sha_for_context}",
                                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github.raw"} 
                            )
                            # 有些檔案可能因為權限或類型無法直接 raw 獲取，GitHub 會返回 JSON
                            if file_content_response.status_code == 200:
                                file_content = file_content_response.text
                                if len(file_content) > MAX_CHARS_PER_PREV_FILE:
                                    file_content = file_content[:MAX_CHARS_PER_PREV_FILE] + f"\n... [檔案 {file_path} 內容因過長已被截斷]"
                                
                                if total_chars_fetched + len(file_content) > MAX_TOTAL_CHARS_PREV_FILES:
                                    remaining_chars = MAX_TOTAL_CHARS_PREV_FILES - total_chars_fetched
                                    file_content = file_content[:remaining_chars] + f"\n... [檔案 {file_path} 內容因總長度限制已被截斷]"
                                
                                temp_files_content.append(f"--- 檔案 {file_path} (來自 Commit {previous_commit_sha_for_context[:7]}) 的內容 ---\n{file_content}\n--- 結束 {file_path} 的內容 ---")
                                total_chars_fetched += len(file_content)
                                fetched_files_count += 1
                            elif file_content_response.status_code == 404:
                                logger.warning(f"檔案 {file_path} 在 commit {previous_commit_sha_for_context} 中未找到 (404)。")
                            else:
                                # 如果不是 200 或 404，記錄錯誤但繼續
                                logger.warning(f"獲取檔案 {file_path} (commit {previous_commit_sha_for_context}) 內容失敗: 狀態碼 {file_content_response.status_code}, {file_content_response.text[:100]}")
                        except Exception as e_file:
                            logger.error(f"獲取檔案 {file_path} (commit {previous_commit_sha_for_context}) 內容時發生異常: {str(e_file)}")
                    
                    previous_commit_files_content_text = "\n\n".join(temp_files_content)
                    if not previous_commit_files_content_text:
                         logger.info(f"未能獲取到 commit {previous_commit_sha_for_context} 中的任何相關檔案內容。")
                    else:
                        logger.info(f"成功獲取 {fetched_files_count} 個來自前一個 commit 的檔案內容，總長度約 {len(previous_commit_files_content_text)} 字元。")

                commit_context_description = f"當前 commit (序號: {current_commit_number_for_context}, SHA: {current_commit_sha_for_context})"
                if previous_commit_sha_for_context:
                    commit_context_description += f"，及其前一個 commit (序號: {previous_commit_number_for_context}, SHA: {previous_commit_sha_for_context}) 中相關檔案的內容"
                else:
                    commit_context_description += " (無前序 commit 資訊)"
            
            else: # 未指定 target_sha，使用最新的 commit diff
                logger.info("對話將使用最新的 commit diff 作為上下文。")
                latest_commit_obj = commits_data[0]
                current_commit_sha_for_context = latest_commit_obj["sha"]
                current_commit_number_for_context = commit_map.get(current_commit_sha_for_context)
                if current_commit_number_for_context is None:
                    logger.error(f"無法為最新 commit SHA {current_commit_sha_for_context} 計算序號 (chat context)。")

                diff_response = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/commits/{current_commit_sha_for_context}",
                    headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github.v3.diff"},
                )
                diff_response.raise_for_status()
                current_commit_diff_text = diff_response.text
                logger.info(f"已獲取最新 commit (序號: {current_commit_number_for_context}, SHA: {current_commit_sha_for_context}) 的 diff。")
                commit_context_description = f"最新 commit (序號: {current_commit_number_for_context}, SHA: {current_commit_sha_for_context})"
                # previous_commit_files_content_text 保持為空

            # 截斷 diff 文本
            if len(current_commit_diff_text) > MAX_CHARS_CURRENT_DIFF: 
                logger.warning(f"當前 commit diff ({len(current_commit_diff_text)} 字元) 過長，截斷至 {MAX_CHARS_CURRENT_DIFF}。")
                current_commit_diff_text = current_commit_diff_text[:MAX_CHARS_CURRENT_DIFF] + "\n... [diff 因過長已被截斷]"
            
            # 獲取 README
            readme_content_for_prompt = ""
            try:
                readme_response = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/readme",
                    headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github.raw"},
                )
                if readme_response.status_code == 200:
                    readme_content_for_prompt = readme_response.text
                    if len(readme_content_for_prompt) > MAX_CHARS_README: 
                        readme_content_for_prompt = readme_content_for_prompt[:MAX_CHARS_README] + "\n... [README 因過長已被截斷]"
                    logger.info(f"成功獲取 README 用於對話上下文。")
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 404:
                    logger.warning(f"獲取 README 時發生 HTTP 錯誤 (非 404): {str(e)}")
            
            history_key = f"{owner}/{repo}/{access_token[:10]}" 
            if history_key not in conversation_history:
                conversation_history[history_key] = []
            history_for_prompt_parts = []
            for item in conversation_history[history_key][-3:]: 
                history_for_prompt_parts.append(f"使用者先前問: {item['question']}")
                history_for_prompt_parts.append(f"你先前答: {item['answer']}")
            history_for_prompt = "\n".join(history_for_prompt_parts)

            selected_model_name = get_available_model()
            logger.info(f"為對話選擇的模型: {selected_model_name}")
            model_instance = genai.GenerativeModel(selected_model_name)

            # 更新提示詞結構
            prompt_context_parts = [f"以下是關於「{commit_context_description}」的程式碼變更摘要:\n"]

            if previous_commit_files_content_text:
                prompt_context_parts.append(f"**來自前一個 Commit (序號: {previous_commit_number_for_context or 'N/A'}, SHA: {previous_commit_sha_for_context[:7] if previous_commit_sha_for_context else 'N/A'}) 中，在當前 Commit 被修改/刪除的檔案的內容 (可能已截斷):**\n```text\n{previous_commit_files_content_text}\n```\n")
            else:
                if target_sha and previous_commit_sha_for_context: # 嘗試獲取但失敗或為空
                     prompt_context_parts.append("未能獲取到前一個 commit 的相關檔案內容，或這些檔案在前一個 commit 中不存在。\n")
                elif target_sha: # 沒有前一個 commit (例如是第一個 commit)
                     prompt_context_parts.append("這是倉庫的第一個 commit，或無法確定前一個 commit。\n")

            prompt_context_parts.append(f"**當前 Commit (序號: {current_commit_number_for_context or 'N/A'}, SHA: {current_commit_sha_for_context[:7]}) 的 Diff (可能已截斷):**\n```diff\n{current_commit_diff_text}\n```")
            
            diff_data_for_prompt = "\n".join(prompt_context_parts)

            prompt = f"""
作為一個專注於 GitHub 倉庫的 AI 助手，請根據以下提供的倉庫上下文（包括指定的 commit diff、相關的前一個 commit 中的檔案內容、以及 README）和先前的對話歷史來回答使用者的問題。
你的回答應該：
1. 簡潔、直接且與提供的上下文相關。
2. 如果問題與程式碼變更相關，請參考「{commit_context_description}」提供的程式碼和 diff。
3. 如果問題超出當前上下文，請誠實告知，避免編造。
4. 內容可能已被截斷以符合長度限制。

**倉庫程式碼上下文**:
{diff_data_for_prompt}

**倉庫 README (若可用, 可能已截斷)**:
```
{readme_content_for_prompt if readme_content_for_prompt else "未提供 README。"}
```

**先前對話歷史 (最近的在最後)**:
{history_for_prompt if history_for_prompt else "這是對話的開始。"}

**使用者當前問題**: {question}

請提供你的回答:
"""
            log_prompt = prompt[:400] + "..." if len(prompt) > 400 else prompt # 增加日誌中 prompt 的長度
            logger.info(f"送往 Gemini 的對話提示詞 (模型: {selected_model_name}, 提示詞長度約: {len(prompt)} 字元): {log_prompt}")

            answer_text = await generate_gemini_content(model_instance, prompt)
            log_answer = answer_text[:100] + "..." if len(answer_text) > 100 else answer_text
            logger.info(f"Gemini 對話回答 (模型: {selected_model_name}): '{log_answer}'")

            conversation_history[history_key].append({"question": question, "answer": answer_text})
            if len(conversation_history[history_key]) > 5: 
                conversation_history[history_key] = conversation_history[history_key][-5:]

            return {"answer": answer_text, "history": conversation_history[history_key]}

        except httpx.HTTPStatusError as e:
            logger.error(f"處理對話時發生 GitHub API 錯誤: {str(e)}, URL: {e.request.url}, Response: {e.response.text}")
            detail = f"因 GitHub API 錯誤，無法處理對話: {e.response.status_code} - {e.response.text}"
            if e.response.status_code == 401:
                detail = "GitHub token 可能無效或已過期 (處理對話時)。"
            elif e.response.status_code == 404 and target_sha: 
                detail = f"指定的 commit SHA ({target_sha}) 或相關檔案未在倉庫 {owner}/{repo} 中找到。"
            raise HTTPException(status_code=e.response.status_code, detail=detail)
        except HTTPException as e: 
            logger.error(f"處理對話時發生 HTTPException: {e.detail}")
            raise e
        except Exception as e:
            logger.error(f"處理對話時發生意外錯誤: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"處理對話時發生意外錯誤: {str(e)}")


@app.post("/repos/{owner}/{repo}/commits/{sha}/analyze")
async def analyze_commit_diff(owner: str, repo: str, sha: str, access_token: str = Query(None)):
    if not access_token:
        raise HTTPException(status_code=401, detail="Access token is missing.")
    logger.info(f"收到 commit 分析請求: owner={owner}, repo={repo}, sha={sha}, token (前5碼)={access_token[:5]}...")
    if not await validate_github_token(access_token):
        raise HTTPException(status_code=401, detail="Invalid or expired GitHub token.")

    async with httpx.AsyncClient() as client:
        try:
            commit_map, commits_data = await get_commit_number_and_list(owner, repo, access_token)
            if not commits_data:
                raise HTTPException(status_code=404, detail="倉庫中沒有 commits，無法進行分析。")

            target_commit_obj = next((c for c in commits_data if c["sha"] == sha), None)
            if not target_commit_obj:
                logger.warning(f"目標 commit SHA {sha} 未在快取的 commit 列表中找到。將嘗試直接從 GitHub API 獲取。")
                try:
                    target_commit_res = await client.get(
                        f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}",
                        headers={"Authorization": f"Bearer {access_token}"}
                    )
                    target_commit_res.raise_for_status() 
                    logger.info(f"目標 commit SHA {sha} 在 GitHub 上找到，但不在本地快取中。繼續分析，但可能缺少序號上下文。")
                except httpx.HTTPStatusError:
                    raise HTTPException(status_code=404, detail=f"目標 commit SHA {sha} 未在倉庫 {owner}/{repo} 中找到。")
            
            target_commit_number = commit_map.get(sha) 
            if target_commit_number is None:
                logger.warning(f"無法為 SHA {sha} 計算序號 (analyze)，將不顯示序號。")

            current_diff_response = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github.v3.diff"},
            )
            current_diff_response.raise_for_status() 
            current_diff_text = current_diff_response.text
            logger.info(f"已獲取目標 commit (序號: {target_commit_number or 'N/A'}, SHA: {sha}) 的 diff。長度: {len(current_diff_text)}")

            previous_diff_text = None
            previous_commit_sha = None
            previous_commit_number = None

            if target_commit_obj:
                target_index = commits_data.index(target_commit_obj)
                if target_index + 1 < len(commits_data): 
                    previous_commit_obj = commits_data[target_index + 1]
                    previous_commit_sha = previous_commit_obj["sha"]
                    previous_commit_number = commit_map.get(previous_commit_sha) 
                    if previous_commit_sha:
                        prev_diff_response = await client.get(
                            f"https://api.github.com/repos/{owner}/{repo}/commits/{previous_commit_sha}",
                            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github.v3.diff"},
                        )
                        if prev_diff_response.status_code == 200:
                            previous_diff_text = prev_diff_response.text
                            logger.info(f"已獲取前一個 commit (序號: {previous_commit_number or 'N/A'}, SHA: {previous_commit_sha}) 的 diff。長度: {len(previous_diff_text)}")
                        else:
                            logger.warning(f"獲取前一個 commit {previous_commit_sha} 的 diff 失敗: 狀態碼 {prev_diff_response.status_code}")
            else:
                logger.info(f"目標 commit SHA {sha} 不在快取中，無法自動確定前一個 commit。")

            current_diff_for_prompt = current_diff_text
            if len(current_diff_for_prompt) > 60000: 
                logger.warning(f"當前 commit diff ({len(current_diff_for_prompt)} 字元) 過長，截斷至 60000。")
                current_diff_for_prompt = current_diff_for_prompt[:60000] + "\n... [diff 因過長已被截斷]"

            previous_diff_for_prompt = previous_diff_text
            if previous_diff_for_prompt and len(previous_diff_for_prompt) > 15000: 
                logger.warning(f"前一個 commit diff ({len(previous_diff_for_prompt)} 字元) 過長，截斷至 15000。")
                previous_diff_for_prompt = previous_diff_for_prompt[:15000] + "\n... [前一個 diff 因過長已被截斷]"

            combined_diff_for_gemini = ""
            if previous_diff_for_prompt and previous_commit_number is not None:
                combined_diff_for_gemini += f"**上下文：前一個 Commit (序號: {previous_commit_number}, SHA: {previous_commit_sha}) 的 Diff 摘要:**\n```diff\n{previous_diff_for_prompt}\n```\n\n"
            elif previous_diff_for_prompt: 
                 combined_diff_for_gemini += f"**上下文：前一個 Commit (SHA: {previous_commit_sha}) 的 Diff 摘要:**\n```diff\n{previous_diff_for_prompt}\n```\n\n"
            else:
                 combined_diff_for_gemini += "沒有找到前一個 commit 的 diff，或者這是倉庫中的第一個有效 commit。\n\n"
            combined_diff_for_gemini += f"**主要分析目標：當前 Commit (序號: {target_commit_number or 'N/A'}, SHA: {sha}) 的 Diff:**\n```diff\n{current_diff_for_prompt}\n```"

            selected_model_name = get_available_model()
            logger.info(f"為 commit 分析選擇的模型: {selected_model_name}")
            model_instance = genai.GenerativeModel(selected_model_name)
            prompt = f"""
作為一位經驗豐富的程式碼審查專家，請分析以下 GitHub commit 變更。
主要分析目標是「當前 Commit (序號: {target_commit_number or 'N/A'}, SHA: {sha})」的變更。
如果提供了「前一個 Commit」的 diff，請將其作為比較的上下文，以理解變更的演進。

你的分析應包含：
1.  **變更摘要**: 簡要說明當前 commit 的主要目的是什麼。
2.  **詳細變更**: 描述當前 commit 中引入的關鍵程式碼更改。可以分點說明。
3.  **影響與改進**: 這些變更如何影響或改進了程式碼庫？它們解決了什麼問題（如果有的話）？
4.  **潛在問題或建議 (可選)**: 是否有任何潛在的風險、需要注意的地方或可以進一步改進的建議？
請使用清晰、專業的語言。

**Commit Diff 上下文**:
{combined_diff_for_gemini}

請提供你的分析報告:
"""
            log_prompt = prompt[:300] + "..." if len(prompt) > 300 else prompt
            logger.info(f"送往 Gemini 的分析提示詞 (模型: {selected_model_name}, 提示詞長度約: {len(prompt)} 字元): {log_prompt}")
            analysis_text = await generate_gemini_content(model_instance, prompt)
            log_analysis = analysis_text[:150] + "..." if len(analysis_text) > 150 else analysis_text
            logger.info(f"Gemini 分析結果 (模型: {selected_model_name}): '{log_analysis}'")
            return {
                "sha": sha,
                "diff": current_diff_text, 
                "previous_diff": previous_diff_text, 
                "analysis": analysis_text,
                "commit_number": target_commit_number, 
                "previous_commit_number": previous_commit_number, 
            }
        except httpx.HTTPStatusError as e:
            logger.error(f"分析 commit diff 時發生 GitHub API 錯誤: {str(e)}, URL: {e.request.url}, Response: {e.response.text}")
            detail = f"因 GitHub API 錯誤，無法分析 commit diff: {e.response.status_code} - {e.response.text}"
            if e.response.status_code == 401:
                detail = "GitHub token 可能無效或已過期 (分析 commit diff 時)。"
            elif e.response.status_code == 404: 
                 detail = f"Commit SHA {sha} 或相關數據未在倉庫 {owner}/{repo} 中找到以進行分析。"
            elif e.response.status_code == 422: 
                 detail = f"無法處理 commit SHA {sha} 的 diff (可能是無效的 SHA 或 diff 過大): {e.response.text}"
            raise HTTPException(status_code=e.response.status_code, detail=detail)
        except HTTPException as e: 
            logger.error(f"分析 commit diff 時發生 HTTPException: {e.detail}")
            raise e
        except Exception as e:
            logger.error(f"分析 commit diff 時發生意外錯誤: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"分析 commit diff 時發生意外錯誤: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    logger.info("啟動 FastAPI 應用程式...")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)