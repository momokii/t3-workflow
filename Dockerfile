# t3-workflow runs on the Python standard library, plus the tools the stages call:
# git for release, and the libatomic1 shared object the T3 CLI needs.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates git libatomic1 openssh-client \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY board/ board/
COPY bridge/ bridge/
COPY tickets/ tickets/
COPY scripts/ scripts/
COPY deploy/ deploy/
COPY watcher.py Makefile README.md AGENTS.md ./

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8787
CMD ["python3", "board/server.py", "--host", "0.0.0.0", "--port", "8787"]
