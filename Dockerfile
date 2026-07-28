FROM python:3.12-slim-bookworm

# git is required for the kleinanzeigen-bot pip dependency (installed from GitHub)
RUN apt-get update && apt-get install -y --no-install-recommends git xvfb && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy project metadata first so pip install is cached separately from source changes
COPY pyproject.toml .
COPY README.md .
COPY bot/ bot/
COPY agents/ agents/
COPY config/ config/
COPY db/ db/
COPY models/ models/
COPY services/ services/
COPY tests/ tests/

# Install Python dependencies incl. test tooling for in-container verification.
RUN pip install --no-cache-dir -e .[test]

# Install Playwright system dependencies and Chromium binary into /opt/playwright.
# Using a fixed path (instead of /root/.cache) keeps it consistent regardless of which
# user runs the container, and avoids conflicts with volume-mounted directories.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
# DISPLAY must be an ENV (not just exported in entrypoint) so docker exec sessions inherit it
ENV DISPLAY=:99
RUN playwright install-deps chromium && playwright install chromium

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

CMD ["/docker-entrypoint.sh"]
