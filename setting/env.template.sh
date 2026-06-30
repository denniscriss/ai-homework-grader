#!/bin/bash

# Copy this file to setting/api_env.sh, then fill in your private values.
# Do not commit setting/api_env.sh.

# Required for AI grading.
# Example: export AI_GRADER_API_KEY="your_api_key"
export AI_GRADER_API_KEY=""

# Required when using a school/local OpenAI-compatible endpoint.
# Example: export AI_GRADER_BASE_URL="https://your-provider.example/api/v1"
export AI_GRADER_BASE_URL=""

# Required unless model is set in setting/run_config.json.
# Example: export AI_GRADER_MODEL="qwen3.5-27b"
export AI_GRADER_MODEL=""

# Optional. Set to 0 to disable SDK retries and avoid long stalls on failing API calls.
export AI_GRADER_API_MAX_RETRIES="0"

# Optional. Set to false for most local/school endpoints unless you need system proxies.
export AI_GRADER_TRUST_ENV="false"

# Required only for Canvas mode.
# Example: export CANVAS_API_TOKEN="your_canvas_access_token"
export CANVAS_API_TOKEN=""
