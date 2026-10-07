FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY app ./app
COPY prompts ./prompts
COPY skills ./skills
COPY data ./data
COPY web ./web

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ".[infra]"

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

