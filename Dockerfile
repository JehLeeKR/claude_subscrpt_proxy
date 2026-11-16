FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml LICENSE ./
COPY ccpool ./ccpool
RUN pip install --no-cache-dir ".[bedrock]" && useradd -u 10001 -m ccpool
USER ccpool
ENTRYPOINT ["ccpool"]
