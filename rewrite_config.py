import os
import re

ENV_TO_CONFIG = {
    "REDIS_URL": "redis_url",
    "DATABASE_URL": "database_url",
    "CLICKHOUSE_HOST": "clickhouse_url", # clickhouse is weird but let's see
    "CLICKHOUSE_PORT": "clickhouse_url",
    "CLICKHOUSE_USER": "clickhouse_url",
    "CLICKHOUSE_PASSWORD": "clickhouse_url",
    "CLICKHOUSE_DB": "clickhouse_url",
    "MINIO_ENDPOINT": "minio_endpoint",
    "REDIS_BLOCK_MS": "redis_block_ms",
    "STAGE1_BATCH_SIZE": "stage1_batch_size",
    "STAGE1_MAX_RETRIES": "stage1_max_retries",
    "STAGE1_OCR_SENTIMENT": "stage1_ocr_sentiment",
    "SENTIMENT_MODEL_CONFIG_KEY": "sentiment_model_config_key",
    "LLM_BACKEND_KEY": "llm_backend_key",
    "MODEL_STUB_MODE": "model_stub_mode",
    "ASSEMBLER_BATCH_SIZE": "assembler_batch_size",
    "ROUTER_BATCH_SIZE": "router_batch_size",
    "ROUTER_MAX_RETRIES": "router_max_retries",
    "STAGE2_BATCH_SIZE": "stage2_batch_size",
    "STAGE2_MAX_RETRIES": "stage2_max_retries",
    "ASSEMBLER_MAX_RETRIES": "assembler_max_retries",
    "ASSEMBLER_FLUSH_INTERVAL": "assembler_flush_interval",
    "INGESTION_MAX_RETRIES": "ingestion_max_retries",
    "NEAR_DUP_DEDUP": "near_dup_dedup",
    "NEAR_DUP_THRESHOLD": "near_dup_threshold",
    "ROUTER_CONFIDENCE_THRESHOLD": "router_confidence_threshold",
    "ROUTER_POST_TYPE_CONFIDENCE_THRESHOLD": "router_post_type_confidence_threshold",
    "ROUTER_TOXICITY_THRESHOLD": "router_toxicity_threshold",
    "ROUTER_LONG_TEXT_CHARS": "router_long_text_chars",
    "BUS_BACKEND": "bus_backend",
    "OTEL_ENABLED": "otel_enabled",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "otel_exporter_otlp_endpoint"
}

for root, _, files in os.walk('src/defense'):
    for file in files:
        if file.endswith('.py') and file != 'config.py':
            filepath = os.path.join(root, file)
            with open(filepath, 'r') as f:
                content = f.read()

            if 'os.getenv' not in content:
                continue
                
            needs_config_import = False
            for env_var, config_prop in ENV_TO_CONFIG.items():
                pattern = r'os\.getenv\("' + env_var + r'"(?:,\s*"[^"]*")?\)'
                if re.search(pattern, content):
                    content = re.sub(pattern, f'config.{config_prop}', content)
                    needs_config_import = True
                    
            if needs_config_import and 'get_settings' not in content:
                # Add import after the last __future__ or at top
                lines = content.split('\n')
                insert_idx = 0
                for i, l in enumerate(lines):
                    if l.startswith('from __future__') or l.startswith('import os'):
                        insert_idx = i + 1
                lines.insert(insert_idx, "from defense.libs.common.config import get_settings")
                lines.insert(insert_idx + 1, "config = get_settings()")
                content = '\n'.join(lines)
                
            with open(filepath, 'w') as f:
                f.write(content)
