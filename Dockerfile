FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_ENDPOINT=https://hf-mirror.com

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple --retries 5 --timeout 60 -r requirements.txt

COPY model-cache/ /model/
COPY app ./app

ENV DA_DATA_DIR=/data \
    DA_EMBEDDING_LOCAL_PATH=/model
VOLUME /data
EXPOSE 8080

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
