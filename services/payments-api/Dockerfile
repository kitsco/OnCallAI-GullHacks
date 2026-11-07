# Build context is ./services (so the shared common/ folder can be copied in).
FROM python:3.12-slim
ARG RELEASE=v1
ENV RELEASE=${RELEASE} \
    SERVICE_NAME=payments-api \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY payments-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common/ ./common/
COPY payments-api/app.py .
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
