# docker build -t open-doc-review .
# docker run --rm -v "$PWD/my-config:/config" -e ANTHROPIC_API_KEY open-doc-review -c /config/docreview.yaml run
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.10.4 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --all-extras && useradd --create-home docreview
USER docreview
ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT ["docreview"]
CMD ["--help"]
