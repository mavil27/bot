FROM ghcr.io/astral-sh/uv:debian

WORKDIR /app
COPY . /app

RUN uv pip install -r requirements.txt
CMD ["uv", "run", "bot.py"]
