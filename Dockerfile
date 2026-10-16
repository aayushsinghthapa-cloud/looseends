FROM python:3.12-slim

# Hugging Face Spaces runs containers as user 1000
RUN useradd -m -u 1000 user
WORKDIR /home/user/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=user . .
USER user

ENV PYTHONUNBUFFERED=1
EXPOSE 7860
# The graph is built from data/keps.json on first start (about 6 seconds).
CMD ["uvicorn", "looseends.web:app", "--host", "0.0.0.0", "--port", "7860"]
