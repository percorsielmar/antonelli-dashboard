FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p data

ENV PASCALE_API_URL=https://pascale-dashboard.onrender.com
ENV STREAMLIT_SERVER_HEADLESS=true
ENV PORT=10000

EXPOSE 10000

CMD streamlit run dashboard.py --server.port=${PORT} --server.headless=true --server.address=0.0.0.0
