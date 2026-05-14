FROM python:3.11-slim
WORKDIR /workspace/project
COPY mock_agent.py /usr/local/bin/mock-agent
RUN chmod +x /usr/local/bin/mock-agent
ENTRYPOINT ["mock-agent"]
