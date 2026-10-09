FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml shipping_mcp.py ./
RUN pip install --no-cache-dir . && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001
EXPOSE 8000
CMD ["uvicorn", "shipping_mcp:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
