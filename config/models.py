"""OpenRouter model configuration mapping."""

OPENROUTER_MODELS: dict[str, str] = {
    # Unified model choice for all tasks (cost-optimized baseline)
    "image_analysis": "google/gemini-2.5-flash-lite",
    "text_generation": "google/gemini-2.5-flash-lite",
    "price_estimation": "google/gemini-2.5-flash-lite",
    "optimization": "google/gemini-2.5-flash-lite",
    "feedback": "google/gemini-2.5-flash-lite",
}
