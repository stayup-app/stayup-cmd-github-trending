FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY pyproject.toml .
COPY fetch_trending.py .
COPY tests/ tests/

ENTRYPOINT ["python", "fetch_trending.py"]
