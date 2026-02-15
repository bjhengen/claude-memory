"""Database helper functions."""

from openai import AsyncOpenAI


async def get_embedding(openai: AsyncOpenAI, text: str) -> list[float]:
    """Generate embedding for text using OpenAI ada-002."""
    response = await openai.embeddings.create(
        model="text-embedding-ada-002",
        input=text
    )
    return response.data[0].embedding


def format_embedding(embedding: list[float]) -> str:
    """Format embedding as PostgreSQL vector string."""
    return f"[{','.join(str(x) for x in embedding)}]"
