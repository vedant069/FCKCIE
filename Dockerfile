FROM python:3.9-slim

WORKDIR /app

# Install Chrome with necessary dependencies
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    unzip \
    curl \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    xdg-utils \
    libpci3 \
    jq \
    && wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y ./google-chrome-stable_current_amd64.deb \
    && rm google-chrome-stable_current_amd64.deb

# Install matching ChromeDriver
RUN CHROME_VERSION=$(google-chrome --version | awk '{print $3}' | cut -d. -f1) \
    && wget -q -O /tmp/latest_chromedriver_version.txt "https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_${CHROME_VERSION}" \
    && CHROMEDRIVER_VERSION=$(cat /tmp/latest_chromedriver_version.txt) \
    && echo "Chrome version: ${CHROME_VERSION}, ChromeDriver version: ${CHROMEDRIVER_VERSION}" \
    && wget -q "https://storage.googleapis.com/chrome-for-testing-public/${CHROMEDRIVER_VERSION}/linux64/chromedriver-linux64.zip" -O /tmp/chromedriver.zip \
    && unzip /tmp/chromedriver.zip -d /tmp/ \
    && mv /tmp/chromedriver-linux64/chromedriver /usr/bin/chromedriver \
    && chmod +x /usr/bin/chromedriver \
    && rm -rf /tmp/chromedriver.zip /tmp/chromedriver-linux64 /tmp/latest_chromedriver_version.txt

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy the application code
COPY . .

# Make modifications to app.py to handle headless browser better
RUN sed -i 's/chrome_options.add_argument("--headless")/chrome_options.add_argument("--headless=new")/g' app.py

# Expose the Streamlit port
EXPOSE 8501

# Set environment variables placeholder (actual key should be provided at runtime)
ENV GEMINI_API_KEY=""

# Run Streamlit
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0"]