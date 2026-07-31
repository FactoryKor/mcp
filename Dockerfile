# 빌드 컨텍스트 = mcp 리포 루트 하나만으로 충분하다(모노레포 아님).
# 각 진단 도구(pg/aks/adx/eh/agw/svcmap)는 requirements.txt의 git+https 버전 태그로 설치된다.
# 예: az acr build -r $ACR -t diag-mcp:v1 -f Dockerfile --secret-build-arg GH_PAT=<PAT> .
FROM python:3.12-slim
WORKDIR /app
COPY . ./mcp/

# 컴포넌트 리포가 private일 때만 필요. 값이 비어있으면 아래 git config는 조용히 건너뛴다.
# ACR: `az acr build --secret-build-arg GH_PAT=<token>` 사용 시 빌드 로그/이미지에 값이 노출되지 않는다.
# (일반 --build-arg는 이미지 히스토리에 남을 수 있으므로 사용하지 말 것)
ARG GH_PAT=""
# git은 requirements.txt의 git+https 설치에만 필요하므로 설치 후 제거한다(python:3.12-slim에는 없음).
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends git ca-certificates; \
    rm -rf /var/lib/apt/lists/*; \
    if [ -n "$GH_PAT" ]; then \
      git config --global url."https://x-access-token:${GH_PAT}@github.com/".insteadOf "https://github.com/"; \
    fi; \
    pip install --no-cache-dir -r mcp/requirements.txt; \
    rm -f /root/.gitconfig; \
    apt-get purge -y --auto-remove git; \
    rm -rf /var/lib/apt/lists/*

EXPOSE 8000
CMD ["python", "mcp/mcp_server.py"]
