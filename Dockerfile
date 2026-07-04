FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

RUN pip install --no-cache-dir tzdata

ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    RUN_ON_START=true \
    SKOOL_CODE_TIMEZONE=America/New_York \
    SKOOL_CODE_RUN_HOUR=5 \
    SKOOL_CODE_RUN_MINUTE=5 \
    QUIZ_ACCESS_REQUIRED=true \
    QUIZ_ACCESS_CODE_PREFIX=BLOOM \
    QUIZ_ACCESS_ROTATION_HOURS=24 \
    QUIZ_ACCESS_ROTATION_ANCHOR_HOUR=5 \
    QUIZ_ACCESS_GRACE_MINUTES=30 \
    QUIZ_ACCESS_TIMEZONE=America/New_York

COPY backend/quiz_access.py backend/quiz_access.py
COPY scripts/skool_publish_quiz_code.py scripts/skool_publish_quiz_code.py
COPY scripts/skool_quiz_lessons.json scripts/skool_quiz_lessons.json
COPY scripts/skool_code_scheduler.py scripts/skool_code_scheduler.py

EXPOSE 8080

CMD ["python", "scripts/skool_code_scheduler.py"]
