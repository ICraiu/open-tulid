FROM python:3.11-slim
WORKDIR /workspace/project
COPY scripted_worker.py /usr/local/bin/scripted-worker
RUN chmod +x /usr/local/bin/scripted-worker
ENTRYPOINT ["scripted-worker"]
