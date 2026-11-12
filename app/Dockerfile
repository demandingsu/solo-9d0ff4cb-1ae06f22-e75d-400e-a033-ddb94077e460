FROM python:3.11-slim

WORKDIR /srv/app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY commuter ./commuter

EXPOSE 5000
CMD ["python", "-m", "flask", "--app", "commuter.app", "run", "--host", "0.0.0.0", "--port", "5000"]
