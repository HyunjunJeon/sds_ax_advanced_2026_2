"""API models only; all agent construction lives in agents/factory.py."""
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from day02.settings import Settings


def make_model(settings: Settings, *, vision: bool = False, timeout: float = 60):
    settings.require_model_key()
    return ChatOpenAI(
        model=settings.vision_model if vision else settings.model,
        api_key=settings.api_key, base_url=settings.api_base,
        temperature=0, max_tokens=5000, timeout=timeout, max_retries=0,
        extra_body={"reasoning": {"effort": "low"}},
    )


def make_embeddings(settings: Settings):
    settings.require_model_key()
    return OpenAIEmbeddings(
        model=settings.embedding_model, api_key=settings.api_key, base_url=settings.api_base,
        check_embedding_ctx_length=False, model_kwargs={"encoding_format": "float"}, max_retries=0,
        request_timeout=60,
    )
