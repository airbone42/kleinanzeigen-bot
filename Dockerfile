FROM python:3.12-slim-bookworm

# git is required for the kleinanzeigen-bot pip dependency (installed from GitHub).
# Chromium is the Debian package (bookworm, security updates via debian-security / LTS,
# e.g. DLA-4878-1), not Playwright's bundled build, which stays frozen at the Playwright
# release (Vikunja #352). Rebuild the image to pick up Chromium updates.
RUN apt-get update && apt-get install -y --no-install-recommends git xvfb chromium fonts-dejavu-core && \
    rm -rf /var/lib/apt/lists/*
ENV CHROMIUM_PATH=/usr/bin/chromium

WORKDIR /app

# Copy project metadata first so pip install is cached separately from source changes
COPY pyproject.toml .
COPY README.md .
COPY LICENSE .
COPY bot/ bot/
COPY agents/ agents/
COPY config/ config/
COPY db/ db/
COPY models/ models/
COPY services/ services/
COPY tests/ tests/
COPY scripts/ scripts/

# Install Python dependencies incl. test tooling for in-container verification.
# fix_nodriver.py is upstream's PDM post_install hook, which plain pip installs skip.
# Without it every XPath lookup fails with CDP -32601 and publishing breaks.
RUN pip install --no-cache-dir -e .[test] && python scripts/fix_nodriver.py

# DISPLAY must be an ENV (not just exported in entrypoint) so docker exec sessions inherit it
ENV DISPLAY=:99

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

CMD ["/docker-entrypoint.sh"]
