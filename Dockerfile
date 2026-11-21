FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --system --uid 10001 worker \
    && install -d -o worker /state
COPY catalog.py worker.py generate.py ./
ENV ICORE_CATALOG=/catalog/catalog.yaml WORKER_STATE=/state/worker.sqlite3
USER worker
CMD ["python", "-u", "worker.py"]
