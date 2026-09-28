"""Non-secret official defaults used when no local .env is present."""

OFFICIAL_NEWAPI_BASE_URL = "https://relayclaw.cdnfg.com/v1"

DEFAULT_COGNEE_LLM_PROVIDER = "newapi"
DEFAULT_COGNEE_LLM_MODEL = "DC-cognee-LLM"
DEFAULT_COGNEE_EMBEDDING_PROVIDER = "newapi"
DEFAULT_COGNEE_EMBEDDING_MODEL = "DC-cognee-embedding"
DEFAULT_COGNEE_EMBEDDING_DIM = "1024"
DEFAULT_EMBEDDING_BATCH_SIZE = "10"

DEFAULT_FREEZONE_TRANSLATION_MODEL = "DC-freezone-translator-LLM"
DEFAULT_FREEZONE_TEXT_WRITER_MODEL = "DC-freezone-text-writer-LLM"
DEFAULT_FREEZONE_STORY_SCRIPT_MODEL = "DC-freezone-story-script-writer-LLM"
DEFAULT_FREEZONE_VISION_MODEL = "DC-freezone-vision-LLM"
DEFAULT_VIDEO_PROMPT_OPTIMIZER_MODEL = "DC-video-prompt-optimizer-LLM"
