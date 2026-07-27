FROM python:3.13-alpine

COPY libation_sort.py /app/libation_sort.py

# Run as a non-root user by default; override with `user:` in compose.
USER 1000:1000

ENTRYPOINT ["python", "-u", "/app/libation_sort.py"]
