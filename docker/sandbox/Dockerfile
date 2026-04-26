FROM python:3.11-slim

# No pip install — sandbox runs pure Python only
RUN useradd --create-home --shell /bin/false sandbox
USER sandbox
WORKDIR /code

# Default command
CMD ["python", "-c", "print('sandbox ready')"]
