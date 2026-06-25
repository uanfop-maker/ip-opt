FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# Install flyctl for SSH access to Fly.io
RUN curl -L https://fly.io/install.sh | sh
ENV PATH="/root/.fly/bin:$PATH"

COPY . .
RUN mkdir -p /app/data

CMD ["python", "bot.py"]
