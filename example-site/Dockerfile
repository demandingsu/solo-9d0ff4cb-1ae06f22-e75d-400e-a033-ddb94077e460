FROM python:3.11-slim

WORKDIR /srv/example
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./

EXPOSE 5001
CMD ["python", "app.py"]
