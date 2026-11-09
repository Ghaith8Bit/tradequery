# Linux image for everything except the MT5 sync: tests, demo, analysis, later the API.
FROM python:3.12-slim

ARG UID=1000
RUN useradd --create-home --uid ${UID} trader
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY tests ./tests
RUN pip install --no-cache-dir ".[dev,query,llm]"

ENV TQ_DATA_DIR=/data
USER trader
ENTRYPOINT ["tradequery"]
