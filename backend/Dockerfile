# 1. Use a slim Python image for a smaller footprint
FROM python:3.11-slim

# 2. Set the working directory
WORKDIR /app

# 3. Install build-essential for gRPC compilation (if needed)
RUN apt-get update && apt-get install -y build-essential && rm -rf /var/lib/apt/lists/*

# 4. Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copy the entire project structure
COPY . .

# 6. Set PYTHONPATH so Python can find the backend package
ENV PYTHONPATH=/app/backend
