FROM python:3.11-slim

# Prevent Python from writing pyc files and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set work directory to /src so imports (from config, etc.) work correctly
WORKDIR /src

# Install system dependencies
# Added: curl and gnupg2 for MS keys, unixodbc-dev for pyodbc compilation
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    gnupg2 \
    unixodbc-dev \
    && rm -rf /var/lib/apt/lists/*

# Add Microsoft Repo for ODBC Driver 18 (Debian 12/Bookworm)
RUN curl https://packages.microsoft.com/keys/microsoft.asc | apt-key add - \
    && curl https://packages.microsoft.com/config/debian/12/prod.list > /etc/apt/sources.list.d/mssql-release.list

# Install the ODBC driver
RUN apt-get update \
    && ACCEPT_EULA=Y apt-get install -y msodbcsql18 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements from the root directory
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the contents of the local 'src' directory into the container's '/src'
COPY src/ .

# Expose the port
EXPOSE 8000

# FIXED: Changed "main:main" to "main:app" to match your code
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]