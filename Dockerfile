# toolsim host. Requires a token when exposed: docker run -e TOOLSIM_TOKEN=... -p 8765:8765 toolsim
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv build --wheel --out-dir /dist

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 toolsim
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl
COPY envs /envs
USER toolsim
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=2)"
# Binding 0.0.0.0 requires TOOLSIM_TOKEN (or an explicit --no-auth).
CMD ["toolsim", "serve", "--host", "0.0.0.0", "--port", "8765", "--env-dir", "/envs"]
