FROM python:3.12-alpine

# iproute2 provides `ip` (startup diagnostics). iputils provides tracepath
# (TCP traceroute for the tcp_trace test; busybox traceroute is UDP-only)
# and GNU ping. tzdata lets summarize.py --tz work inside the container.
RUN apk add --no-cache iproute2 iputils tzdata
WORKDIR /app
COPY ping_logger.py mc_protocol.py net_tools.py config.py summarize.py /app/

VOLUME /data
ENV LOG_DIR=/data

ENTRYPOINT ["python", "-u", "/app/ping_logger.py"]
