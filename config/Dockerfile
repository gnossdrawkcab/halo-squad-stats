# Use a lightweight official Python 3.11 image as the base
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set the working directory inside the container
WORKDIR /app

# Install system dependencies
# fonts-dejavu-core: TrueType fonts for Pillow-rendered shareable stat cards.
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Copy dependencies file and install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all local files into the container
COPY . .

# Set the PYTHONPATH to include the src directory
ENV PYTHONPATH=/app/src:$PYTHONPATH

# Set the default command (will be overridden by compose)
CMD ["python", "/app/src/entrypoint.py"]
