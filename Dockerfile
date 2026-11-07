FROM python:3.12-slim
WORKDIR /app
COPY chess_tourney.py serve.py ./
COPY web ./web
RUN mkdir -p /data/results && useradd -r -u 10001 app && chown -R app /data
USER app
EXPOSE 8000
CMD ["python3", "-I", "serve.py", "--host", "0.0.0.0", "--port", "8000", "--results", "/data/results", "--config", "/data/tourney.json"]
