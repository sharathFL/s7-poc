FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 CONFIG_PATH=/config/devices.yaml DASHBOARD_DIR=/dashboard
WORKDIR /app
COPY python/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY python/app ./app
COPY python/tools ./tools
COPY dashboard /dashboard
EXPOSE 8001
USER nobody
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
