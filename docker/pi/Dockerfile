FROM node:22.19.0-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @earendil-works/pi-coding-agent@1.4.0 \
    && npm cache clean --force
COPY --chown=node:node bin /opt/sapper/bin
COPY --chown=node:node lib /opt/sapper/lib
COPY --chown=node:node pi /opt/sapper/pi
COPY --chown=node:node templates /opt/sapper/templates
COPY --chown=node:node package.json /opt/sapper/package.json
COPY --chown=node:node docs/pi.md /opt/sapper/docs/pi.md
ENV PATH="/opt/sapper/bin:${PATH}" PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_TELEMETRY=0 PYTHONDONTWRITEBYTECODE=1
RUN mkdir /work && chown node:node /work
WORKDIR /work
USER node
ENTRYPOINT ["sapper"]
CMD ["worker", "serve"]
