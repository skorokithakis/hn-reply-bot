FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /uvx /bin/

ADD uv.lock /code/
ADD pyproject.toml /code/
WORKDIR /code
RUN uv sync --frozen --no-dev

COPY . /code/
