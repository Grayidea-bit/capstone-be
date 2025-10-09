from ..setting import logger, redis_client, CACHE_TTL_SECONDS
from transformers import AutoTokenizer, AutoModel
import torch


tokenizer = AutoTokenizer.from_pretrained("jinaai/jina-embeddings-v2-base-code")
model = AutoModel.from_pretrained(
    "jinaai/jina-embeddings-v2-base-code", trust_remote_code=True
)


def embedding_function(text):

    inputs = tokenizer(
        text,
        return_tensors="pt",
        padding=True,
        truncation=True,
    )
    with torch.no_grad():
        outputs = model(**inputs)
        embedding = outputs.last_hidden_state.mean(dim=1)  # 取平均向量
        embedding = torch.nn.functional.normalize(embedding, p=2, dim=1)  # L2 normalize

    return embedding[0]
