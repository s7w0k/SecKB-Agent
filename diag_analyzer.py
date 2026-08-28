from app.services.vector_backends.factory import _build_opensearch
from app.core.config import get_settings
b = _build_opensearch(get_settings())
cl = b._client

def probe(label, body):
    try:
        r = cl.transport.perform_request(method="POST", url="/_analyze", body=body)
        toks = [t["token"] for t in r.get("tokens", [])]
        print(label, "->", toks[:30])
    except Exception as e:
        print(label, "ERR", str(e)[:200])

probe("cjk_bigram_filter", {
    "tokenizer": "standard",
    "filter": ["lowercase", "cjk_bigram"],
    "text": "模型安全红队评估平台 testRAG"},
)
probe("ngram", {
    "tokenizer": {"type": "nGram", "min_gram": 2, "max_gram": 2,
                  "token_chars": ["letter", "digit"]},
    "text": "模型安全红队平台test"},
)
probe("default_standard", {"text": "模型安全红队平台 testRAG"})