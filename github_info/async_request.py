import asyncio
import httpx
import re
import logging
from fastapi import HTTPException


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def request_github(client, page, url, headers,branch):
    if not branch:
        par={"per_page": 100, "page": page}
    else:
        par={"per_page": 100, "page": page,"sha":branch }
    res = await client.get(url, headers=headers, params=par)
    res.raise_for_status()
    response = dict()
    response[page] = res.json()
    return response


async def async_multiple_request(url, headers, branch=""):
    async with httpx.AsyncClient() as client:
        try:
            if not branch:
                par={"per_page": 100, "page": 1}
            else:
                par={"per_page": 100, "page": 1,"sha":branch }
            res = await client.get(
                url=url,
                headers=headers,
                params=par,
            )
            res.raise_for_status()
            print(res.json())

            link_header = res.headers.get("Link", "")
            if not branch:
                match = re.search(r'page=(\d+)>; rel="last"', link_header)

            else:
                match = re.search(r'page=(\d+)&sha=main>; rel="last"', link_header)

            total_pages = int(match.group(1)) if match else 1

            print(f" 共 {total_pages} 頁，開始抓取...")

            tasks = [
                request_github(client, page, url, headers,branch)
                for page in range(1, total_pages + 1)
            ]
            results = await asyncio.gather(*tasks)

            results_dict = dict()
            for i in results:
                results_dict.update(i)
                
            """  need to sort and get 
            for page in range(1, len(results_dict) + 1):
                for context in results_dict[page]:
                    print(context.get("commit").get("message"))
            """
            
            return results_dict


        except httpx.HTTPStatusError as e:
            logger.error(
                f"GitHub API 返回錯誤: {e.response.status_code} - {e.response.text}"
            )
            raise HTTPException(
                status_code=e.response.status_code,
                detail=f"GitHub API Error: {e.response.text}",
            )
        except Exception as e:
            logger.error(f"發生意外錯誤: {e}")
            raise HTTPException(status_code=500, detail="內部伺服器錯誤")

