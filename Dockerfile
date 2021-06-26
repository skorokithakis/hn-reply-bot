FROM python:3.8-slim
ENV PYTHONUNBUFFERED 1

RUN pip install -U --pre pip poetry
ADD poetry.lock /code/
ADD pyproject.toml /code/
RUN poetry config virtualenvs.create false
WORKDIR /code
RUN poetry install --no-dev --no-interaction --no-root

COPY . /code/
