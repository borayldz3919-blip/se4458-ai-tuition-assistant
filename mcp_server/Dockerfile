FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY indexer.py server.py ./
COPY data/University_Tuition_Policies.md ./data/
# Index is built ONCE at image build time (not per question)
RUN python indexer.py
ENV PORT=8001
EXPOSE 8001
CMD ["python", "server.py"]
