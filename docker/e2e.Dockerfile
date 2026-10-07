FROM mcr.microsoft.com/playwright:v1.58.2-noble
WORKDIR /tests
COPY e2e/package*.json ./
RUN npm ci
COPY e2e/ ./
CMD ["npx", "playwright", "test"]
