FROM python:3.11-slim

WORKDIR /app

# Зависимости копируются отдельно для кэширования слоёв
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV DEBUG=false
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
