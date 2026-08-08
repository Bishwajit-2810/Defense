import os

replacements = {
    "src/defense/libs/auth.py": [
        ('int(os.environ.get("SSE_TICKET_TTL", "60"))', 'config.sse_ticket_ttl'),
        ('import os', 'import os\nfrom defense.libs.common.config import get_settings\n\nconfig = get_settings()')
    ],
    "src/defense/libs/common/logging.py": [
        ('int(os.environ.get("LOG_REDIS_MAX", "3000"))', 'config.log_redis_max'),
        ('int(os.environ.get("LOG_REDIS_TTL", "86400"))', 'config.log_redis_ttl'),
        ('os.environ.get("LOG_TO_REDIS", "true").lower() == "true"', 'config.log_to_redis'),
        ('os.environ.get("REDIS_URL", "redis://localhost:6379/0")', 'config.redis_url'),
        ('os.environ.get("LOG_LEVEL", "INFO").upper()', 'config.log_level.upper()'),
        ('os.environ.get("APP_ENV", "dev")', 'config.app_env'),
        ('import os', 'import os\nfrom defense.libs.common.config import get_settings\n\nconfig = get_settings()')
    ],
    "src/defense/libs/common/config.py": [
        ('os.environ.get("APP_ENV") or "dev"', 'get_settings().app_env'),
        ('os.environ.get("JWT_SECRET") or JWT_DEV_DEFAULT_SECRET', 'get_settings().jwt_secret or JWT_DEV_DEFAULT_SECRET')
    ],
    "src/defense/libs/embeddings.py": [
        ('int(os.environ.get("EMBEDDING_DIM", "768"))', 'config.embedding_dim'),
        ('os.environ.get("MODEL_STUB_MODE", "true").lower() == "true"', 'config.model_stub_mode'),
        ('os.environ.get("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)', 'config.embedding_model'),
        ('import os', 'import os\nfrom defense.libs.common.config import get_settings\n\nconfig = get_settings()')
    ],
    "src/defense/libs/llm/client.py": [
        ('int(os.environ.get("LLM_MAX_CONTINUATIONS", "2"))', 'config.llm_max_continuations'),
        ('os.environ.get("LLM_BACKEND", "local")', 'config.llm_backend'),
        ('os.environ.get(\n            "LOCAL_LLM_BASE_URL", "http://localhost:8000/v1"\n        )', 'config.local_llm_base_url'),
        ('os.environ.get("LOCAL_LLM_API_KEY", "ollama")', 'config.local_llm_api_key'),
        ('os.environ.get("GROQ_API_KEY", "")', 'config.groq_api_key'),
        ('os.environ.get(_ROLE_LOCAL_ENV[role], _ROLE_LOCAL_DEFAULT[role])', 'getattr(config, _ROLE_LOCAL_ENV[role].lower(), _ROLE_LOCAL_DEFAULT[role])'),
        ('os.environ.get(_ROLE_GROQ_ENV[role], _ROLE_GROQ_DEFAULT[role])', 'getattr(config, _ROLE_GROQ_ENV[role].lower(), _ROLE_GROQ_DEFAULT[role])'),
        ('int(os.environ.get("LLM_CB_FAILURE_THRESHOLD", "5"))', 'config.llm_cb_failure_threshold'),
        ('float(os.environ.get("LLM_CB_COOLDOWN_SECONDS", "30"))', 'config.llm_cb_cooldown_seconds'),
        ('import os', 'import os\nfrom defense.libs.common.config import get_settings\n\nconfig = get_settings()')
    ],
    "src/defense/libs/llm/policy.py": [
        ('os.environ.get("LLM_BACKEND", "local")', 'config.llm_backend'),
        ('import os', 'import os\nfrom defense.libs.common.config import get_settings\n\nconfig = get_settings()')
    ],
    "src/defense/libs/llm/usage.py": [
        ('os.environ.get("LLM_USAGE_TRACKING_DISABLED", "").lower() in ("1", "true", "yes")', 'config.llm_usage_tracking_disabled'),
        ('os.environ.get("REDIS_URL", "redis://localhost:6379/0")', 'config.redis_url'),
        ('import os', 'import os\nfrom defense.libs.common.config import get_settings\n\nconfig = get_settings()')
    ],
    "src/defense/mcp_servers/analytics_mcp/server.py": [
        ('int(os.environ.get("PORT", 8100))', 'config.analytics_mcp_port')
    ],
    "src/defense/mcp_servers/ingest_mcp/server.py": [
        ('os.environ.get("REDIS_URL", "redis://localhost:6379/0")', 'config.redis_url'),
        ('int(os.environ.get("PORT", 8102))', 'config.ingest_mcp_port'),
        ('import os', 'import os\nfrom defense.libs.common.config import get_settings\n\nconfig = get_settings()')
    ],
    "src/defense/mcp_servers/retrieval_mcp/server.py": [
        ('os.environ.get(\n    "DATABASE_URL", "postgresql://defense:defense@localhost:5432/defense"\n)', 'config.database_url'),
        ('os.environ.get("RETRIEVAL_MCP_STUB", "false").lower() == "true"', 'config.retrieval_mcp_stub'),
        ('int(os.environ.get("PORT", 8101))', 'config.retrieval_mcp_port'),
        ('import os', 'import os\nfrom defense.libs.common.config import get_settings\n\nconfig = get_settings()')
    ],
    "src/defense/services/api/deps.py": [
        ('os.environ.get(\n    "DATABASE_URL", "postgresql+asyncpg://defense:defense@localhost:5432/defense"\n)', 'config.get_async_database_url()'),
        ('os.environ.get("REDIS_URL", "redis://localhost:6379/0")', 'config.redis_url'),
        ('os.environ.get("LLM_BACKEND", "local")', 'config.llm_backend'),
        ('import os', 'import os\nfrom defense.libs.common.config import get_settings\n\nconfig = get_settings()')
    ],
    "src/defense/services/api/routers/auth.py": [
        ('int(os.environ.get("JWT_EXPIRE_HOURS", "1"))', 'config.jwt_expire_hours'),
        ('os.environ.get(\n        "JWT_SECRET", "change-me-in-production"\n    )', 'config.jwt_secret'),
        ('os.environ.get(\n        "JWT_SECRET", "change-me"\n    )', 'config.jwt_secret'),
        ('import os', 'import os\nfrom defense.libs.common.config import get_settings\n\nconfig = get_settings()')
    ]
}

def fix_file(path, reps):
    full_path = f"/home/bk/code/defense/{path}"
    with open(full_path, "r") as f:
        content = f.read()
    
    for old, new in reps:
        if 'import os\nfrom defense.libs.common.config import get_settings' in new and 'from defense.libs.common.config import get_settings' in content:
            # Skip if already imported
            pass
        else:
            content = content.replace(old, new)
            
    with open(full_path, "w") as f:
        f.write(content)

for path, reps in replacements.items():
    fix_file(path, reps)
