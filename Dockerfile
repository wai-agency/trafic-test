FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY traffic_monitor ./traffic_monitor

RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1
CMD ["traffic-monitor", "watch", "--interval", "300"]
