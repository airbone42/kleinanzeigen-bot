"""OpenRouter API client using LangChain ChatOpenAI."""
import logging
from typing import Optional

from langchain_openai import ChatOpenAI

from config.settings import settings
from config.models import OPENROUTER_MODELS

logger = logging.getLogger(__name__)


def get_llm(task: str, temperature: float = 0.7) -> ChatOpenAI:
    """Create a ChatOpenAI instance configured for OpenRouter.

    Args:
        task: Task key from OPENROUTER_MODELS dict
        temperature: Sampling temperature (0.0-1.0)

    Returns:
        Configured ChatOpenAI instance
    """
    model_name = OPENROUTER_MODELS.get(task, OPENROUTER_MODELS["text_generation"])
    logger.debug(f"Creating LLM for task '{task}' using model '{model_name}'")

    return ChatOpenAI(
        model=model_name,
        openai_api_key=settings.openrouter_api_key,  # type: ignore[arg-type]
        openai_api_base=settings.openrouter_base_url,
        temperature=temperature,
        default_headers={
            "HTTP-Referer": "https://github.com/kleinanzeigen-telegram-bot",
            "X-Title": "Kleinanzeigen Telegram Bot",
        },
    )


def get_vision_llm(temperature: float = 0.3) -> ChatOpenAI:
    """Get LLM configured for vision/image analysis tasks."""
    return get_llm("image_analysis", temperature)


def get_text_llm(temperature: float = 0.7) -> ChatOpenAI:
    """Get LLM configured for text generation tasks."""
    return get_llm("text_generation", temperature)


def get_optimization_llm(temperature: float = 0.5) -> ChatOpenAI:
    """Get LLM configured for listing optimization tasks."""
    return get_llm("optimization", temperature)


def get_feedback_llm(temperature: float = 0.6) -> ChatOpenAI:
    """Get LLM configured for feedback processing tasks."""
    return get_llm("feedback", temperature)
