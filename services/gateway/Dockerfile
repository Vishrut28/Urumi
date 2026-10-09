FROM python:3.12-slim
ARG HELM_VERSION=v3.16.2
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 HOME=/tmp
WORKDIR /app
# helm is used only by the optional store provisioner, which installs the same store chart
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" | tar -xz -C /tmp \
 && mv /tmp/linux-amd64/helm /usr/local/bin/helm && rm -rf /tmp/linux-amd64 \
 && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
COPY services/gateway/pyproject.toml ./
COPY services/gateway/gateway ./gateway
RUN pip install --no-cache-dir . && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY charts/store /charts/store
USER 10001
EXPOSE 8080
CMD ["uvicorn", "--factory", "gateway.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
